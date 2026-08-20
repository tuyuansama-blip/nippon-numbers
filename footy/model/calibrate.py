"""Online walk-forward recalibration layer `Tb` (DESIGN_PHASE2.md 4).

Not a performance device: the honest TUNE-period measurement is a swing of
RPS 0.00013, about 2% of the market gap (DESIGN_PHASE2.md 0.9). It earns its
place for three reasons that have nothing to do with squeezing more RPS out
of the model -- J1's base rates drift more year to year than EPL's (4.1-1),
the product's published calibration curve should stay straight on its own
(4.1-2), and J1 has no TUNE period to freeze a constant against, so a layer
that re-derives itself online is the only kind that can run there at all
(4.1-3, 6.1).

    Tb:  z = log p ;  g(z; phi) = z / exp(phi0) + (phi1, phi2, 0)
    q = softmax(g(log p; phi))

`phi` is never frozen -- what *is* frozen is the functional form, `lambda`,
the warm-up threshold and the refit cadence (4.2). `OnlineTb` refits on the
sum (not the mean) of NLL over its own history plus an L2 pull back to
`phi_identity = 0`, exactly as specified, so `lambda` behaves as an
effective prior sample count that decays naturally as history accumulates
rather than a hyperparameter whose strength silently depends on n.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from footy.config import (
    CALIBRATE_LAMBDA,
    CALIBRATE_REFIT_DAYS,
    CALIBRATE_WARMUP_MATCHES,
)

PHI_IDENTITY = np.zeros(3)


def apply_tb(probs, phi) -> np.ndarray:
    """`softmax(log(p) / exp(phi0) + (phi1, phi2, 0))`."""
    p = np.clip(np.asarray(probs, dtype="float64"), 1e-12, 1.0)
    phi = np.asarray(phi, dtype="float64")
    temp = float(np.exp(phi[0]))
    offset = np.array([phi[1], phi[2], 0.0])
    g = np.log(p) / temp + offset
    g = g - g.max(axis=1, keepdims=True)
    q = np.exp(g)
    return q / q.sum(axis=1, keepdims=True)


def _nll_sum_and_grad(phi: np.ndarray, log_p: np.ndarray, y: np.ndarray):
    """Sum (not mean) NLL of `softmax(log_p/exp(phi0) + (phi1,phi2,0))`,
    and its exact gradient -- the closed form matters here for the same
    reason it did for Dixon-Coles (DESIGN.md 1.2): `phi` is refit at every
    predict in the online loop, so a slow or numerically-differentiated fit
    would make the whole walk-forward run an order of magnitude slower.
    """
    temp = float(np.exp(phi[0]))
    offset = np.array([phi[1], phi[2], 0.0])
    g = log_p / temp + offset
    g = g - g.max(axis=1, keepdims=True)
    expg = np.exp(g)
    z = expg.sum(axis=1, keepdims=True)
    logq = g - np.log(z)
    n = len(y)
    idx = np.arange(n)
    nll = float(-np.sum(logq[idx, y]))

    q = expg / z
    onehot = np.zeros_like(q)
    onehot[idx, y] = 1.0
    dl_dg = q - onehot                      # (n, 3): dNLL/dg
    dg_dphi0 = -log_p / temp                # d(log_p/exp(phi0))/dphi0

    grad = np.array([
        float(np.sum(dl_dg * dg_dphi0)),
        float(np.sum(dl_dg[:, 0])),
        float(np.sum(dl_dg[:, 1])),
    ])
    return nll, grad


def fit_tb(
    probs, y, *, lam: float = CALIBRATE_LAMBDA, phi0=None, max_iter: int = 200
) -> np.ndarray:
    """`argmin_phi sum_k NLL(g(log p_k; phi), y_k) + lam/2 * ||phi||^2`."""
    p = np.clip(np.asarray(probs, dtype="float64"), 1e-12, 1.0)
    log_p = np.log(p)
    y = np.asarray(y, dtype="int64")
    init = PHI_IDENTITY.copy() if phi0 is None else np.asarray(phi0, dtype="float64")

    def objective(phi):
        nll, grad = _nll_sum_and_grad(phi, log_p, y)
        reg = 0.5 * lam * float(np.sum(phi**2))
        return nll + reg, grad + lam * phi

    result = minimize(
        objective, init, method="L-BFGS-B", jac=True,
        options={"maxiter": max_iter},
    )
    return np.asarray(result.x, dtype="float64")


class OnlineTb:
    """The online loop itself: predict with the current `phi`, then learn
    from the outcome once it is known -- strictly after, so the prediction
    for match k never sees k's own result (the same asof discipline as the
    rest of the project, DESIGN.md 2.3).

    `history` accumulates raw (pre-calibration) model probabilities and
    outcomes for every match this instance has been asked to predict. Below
    `warmup` matches of history, `phi` stays at `phi_identity` (4.3's
    "ウォームアップ未達なら φ = identity"). Above it, `phi` is refit at most
    once per `refit_days` of asof advance (4.3's "リフィット頻度 = 月次"),
    which keeps the online loop's cost roughly constant per fold instead of
    growing with history length.
    """

    def __init__(
        self,
        *,
        warmup: int = CALIBRATE_WARMUP_MATCHES,
        lam: float = CALIBRATE_LAMBDA,
        refit_days: int = CALIBRATE_REFIT_DAYS,
    ):
        self.warmup = int(warmup)
        self.lam = float(lam)
        self.refit_days = int(refit_days)
        self.phi = PHI_IDENTITY.copy()
        self._last_refit: pd.Timestamp | None = None
        self._history_p: list[np.ndarray] = []
        self._history_y: list[np.ndarray] = []
        self.n_history = 0
        self.log: list[dict] = []          # one row per `predict` call (CL-3)

    def _refit_if_due(self, asof: pd.Timestamp) -> bool:
        if self.n_history < self.warmup:
            self.phi = PHI_IDENTITY.copy()
            return False
        due = (
            self._last_refit is None
            or (asof - self._last_refit).days >= self.refit_days
        )
        if not due:
            return False
        p = np.concatenate(self._history_p, axis=0)
        y = np.concatenate(self._history_y, axis=0)
        self.phi = fit_tb(p, y, lam=self.lam, phi0=self.phi)
        self._last_refit = asof
        return True

    def predict(self, asof, probs: np.ndarray) -> np.ndarray:
        """Calibrate `probs` (already asof-honest) with the current `phi`,
        refitting first if warm-up has been cleared and a refit is due."""
        asof = pd.Timestamp(asof)
        refitted = self._refit_if_due(asof)
        calibrated = apply_tb(probs, self.phi)
        self.log.append({
            "asof": asof,
            "n_history": self.n_history,
            "warm": self.n_history >= self.warmup,
            "refitted": refitted,
            "temperature": float(np.exp(self.phi[0])),
            "phi_home": float(self.phi[1]),
            "phi_draw": float(self.phi[2]),
        })
        return calibrated

    def observe(self, raw_probs: np.ndarray, y) -> None:
        """Learn from a fold's outcome -- called only once the fold's own
        result is known, i.e. after `predict` for that fold has run."""
        y = np.asarray(y, dtype="int64")
        self._history_p.append(np.asarray(raw_probs, dtype="float64"))
        self._history_y.append(y)
        self.n_history += len(y)

    def phi_frame(self) -> pd.DataFrame:
        """The `phi` time series for CL-3 (4.4): identity vs. systematic
        drift is a model-drift alarm, not a calibration success."""
        if not self.log:
            return pd.DataFrame(
                columns=["asof", "n_history", "warm", "refitted",
                         "temperature", "phi_home", "phi_draw"]
            )
        return pd.DataFrame(self.log)
