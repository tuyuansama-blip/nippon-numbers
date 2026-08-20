"""Dixon-Coles with time decay and ridge shrinkage (DESIGN.md 1).

    log lam_k = mu0 + gamma + a_h(k) - d_a(k)
    log mu_k  = mu0         + a_a(k) - d_h(k)

Three implementation decisions carry the whole thing:

**Identification by ridge, not by a hard constraint.** `mu0` plus a Gaussian
prior on `a` and `d` breaks the additive degeneracy without `sum(a) == 0`, so
the fit stays an unconstrained L-BFGS-B problem and promoted sides are handled
by the same machinery that identifies the model (DESIGN.md 1.1, 1.4).

**Analytic gradient.** With ~2x35+3 parameters a numerical gradient costs ~74
function evaluations per step, which turns the 3,400-fit tuning grid into
something nobody runs. Every derivative below is closed form, and
`tests/test_dc.py` checks it against `scipy.optimize.check_grad` -- that test
is the one that must never be allowed to go red (DESIGN.md 1.2).

**Warm start.** One matchweek of new data barely moves theta, so the previous
solution is the next initial point, keyed by team so a changing squad of teams
does not scramble the vector.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from footy.config import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_PI,
    DEFAULT_SIGMA,
    MAX_ITER,
    RHO_BOUNDS,
    SCORE_K,
    TAU_FLOOR,
    WINDOW_SEASONS,
)
from footy.model.base import BaseModel
from footy.model.scoreline import probs_1x2


def decay_rate(half_life_days: float) -> float:
    """xi = ln2 / HL. An infinite half-life means no decay at all."""
    if half_life_days is None or not np.isfinite(half_life_days):
        return 0.0
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive (or inf for no decay)")
    return math.log(2.0) / float(half_life_days)


def season_of(asof) -> int:
    """Season start year containing `asof` (Aug 1 boundary).

    2020-07-20 -> 2019 (the COVID-extended 2019/20), 2020-09-12 -> 2020.
    """
    stamp = pd.Timestamp(asof)
    return int(stamp.year if stamp.month >= 8 else stamp.year - 1)


@dataclass
class FitMeta:
    n_matches: int = 0
    n_teams: int = 0
    n_promoted: int = 0
    iterations: int = 0
    func_evals: int = 0
    objective: float = float("nan")
    converged: bool = False
    message: str = ""
    tau_clipped: int = 0
    rho_at_bound: bool = False
    seconds: float = 0.0
    warm_started: bool = False
    window_from_season: int | None = None
    extras: dict = field(default_factory=dict)


def _unpack(theta: np.ndarray, n_teams: int):
    return (
        theta[0],
        theta[1],
        theta[2 : 2 + n_teams],
        theta[2 + n_teams : 2 + 2 * n_teams],
        theta[-1],
    )


def objective_and_grad(
    theta: np.ndarray,
    home_idx: np.ndarray,
    away_idx: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    w: np.ndarray,
    prior_a: np.ndarray,
    prior_d: np.ndarray,
    sigma: float,
    n_teams: int,
    counter: dict | None = None,
) -> tuple[float, np.ndarray]:
    """Negative penalised weighted log-likelihood and its exact gradient.

    Returns `(-l, -dl/dtheta)` so it can be handed straight to a minimiser.
    """
    mu0, gamma, a, d, rho = _unpack(theta, n_teams)

    log_lam = mu0 + gamma + a[home_idx] - d[away_idx]
    log_mu = mu0 + a[away_idx] - d[home_idx]
    lam = np.exp(log_lam)
    mu = np.exp(log_mu)

    # --- Dixon-Coles low-score correction ------------------------------------
    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)

    tau_raw = np.ones_like(lam)
    tau_raw = np.where(m00, 1.0 - lam * mu * rho, tau_raw)
    tau_raw = np.where(m01, 1.0 + lam * rho, tau_raw)
    tau_raw = np.where(m10, 1.0 + mu * rho, tau_raw)
    tau_raw = np.where(m11, 1.0 - rho, tau_raw)
    tau = np.maximum(tau_raw, TAU_FLOOR)
    # Where the floor bites, tau is constant in theta, so its derivative is 0.
    active = (tau_raw >= TAU_FLOOR).astype("float64")
    if counter is not None:
        counter["tau_clipped"] = int((tau_raw < TAU_FLOOR).sum())

    log_lik = float(
        np.sum(w * (np.log(tau) + x * log_lam - lam + y * log_mu - mu))
    )
    resid_a = a - prior_a
    resid_d = d - prior_d
    inv_var = 1.0 / (sigma * sigma)
    penalty = 0.5 * inv_var * float(np.sum(resid_a**2) + np.sum(resid_d**2))

    # --- gradient ------------------------------------------------------------
    inv_tau = active / tau

    dtau_dlam = np.where(m00, -mu * rho, 0.0) + np.where(m01, rho, 0.0)
    dtau_dmu = np.where(m00, -lam * rho, 0.0) + np.where(m10, rho, 0.0)
    dtau_drho = (
        np.where(m00, -lam * mu, 0.0)
        + np.where(m01, lam, 0.0)
        + np.where(m10, mu, 0.0)
        + np.where(m11, -1.0, 0.0)
    )

    # dl/d(log lam) and dl/d(log mu), per match. d lam / d log lam == lam.
    g_lam = w * ((x - lam) + lam * inv_tau * dtau_dlam)
    g_mu = w * ((y - mu) + mu * inv_tau * dtau_dmu)

    grad = np.zeros_like(theta)
    grad[0] = np.sum(g_lam) + np.sum(g_mu)          # mu0 enters both means
    grad[1] = np.sum(g_lam)                          # gamma enters lam only

    grad_a = np.zeros(n_teams)
    grad_d = np.zeros(n_teams)
    np.add.at(grad_a, home_idx, g_lam)               # a_h raises lam
    np.add.at(grad_a, away_idx, g_mu)                # a_a raises mu
    np.add.at(grad_d, away_idx, -g_lam)              # d_a lowers lam
    np.add.at(grad_d, home_idx, -g_mu)               # d_h lowers mu

    grad_a -= inv_var * resid_a
    grad_d -= inv_var * resid_d
    grad[2 : 2 + n_teams] = grad_a
    grad[2 + n_teams : 2 + 2 * n_teams] = grad_d
    grad[-1] = np.sum(w * inv_tau * dtau_drho)

    if counter is not None:
        counter["func_evals"] = counter.get("func_evals", 0) + 1
    return -(log_lik - penalty), -grad


class DixonColes(BaseModel):
    """Time-decayed, ridge-shrunk Dixon-Coles."""

    name = "dc"

    def __init__(
        self,
        *,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        sigma: float = DEFAULT_SIGMA,
        pi: tuple[float, float] = DEFAULT_PI,
        rho_bounds: tuple[float, float] = RHO_BOUNDS,
        window_seasons: int = WINDOW_SEASONS,
        k_max: int = SCORE_K,
        max_iter: int = MAX_ITER,
        fit_rho: bool = True,
    ):
        self.half_life_days = float(half_life_days)
        self.xi = decay_rate(half_life_days)
        self.sigma = float(sigma)
        self.pi = (float(pi[0]), float(pi[1]))
        self.rho_bounds = tuple(rho_bounds)
        self.window_seasons = int(window_seasons)
        self.k_max = int(k_max)
        self.max_iter = int(max_iter)
        self.fit_rho = bool(fit_rho)

        self.teams: list[str] = []
        self.index: dict[str, int] = {}
        self.mu0 = 0.0
        self.gamma = 0.0
        self.attack = np.zeros(0)
        self.defence = np.zeros(0)
        self.rho = 0.0
        self.promoted: set[str] = set()
        self.asof: pd.Timestamp | None = None
        self.meta = FitMeta()

    # -- promoted sides -------------------------------------------------------
    def promoted_teams(self, history: pd.DataFrame, season: int) -> set[str]:
        """Sides in `season` that did not play in the division the year before.

        Uses only matches already in the training frame, so it is computable
        at `asof` with no knowledge of the future. A club returning after ten
        years away counts as promoted: what the prior encodes is "just came
        up from the Championship", not "never existed" (DESIGN.md 1.4).
        """
        previous = history[history["season"] == season - 1]
        if previous.empty:
            return set()
        seen = set(previous["home_id"]) | set(previous["away_id"])
        current = history[history["season"] == season]
        now = set(current["home_id"]) | set(current["away_id"])
        return now - seen

    def _priors(self, teams: list[str]) -> tuple[np.ndarray, np.ndarray]:
        prior_a = np.zeros(len(teams))
        prior_d = np.zeros(len(teams))
        for i, team in enumerate(teams):
            if team in self.promoted:
                prior_a[i] = self.pi[0]
                prior_d[i] = self.pi[1]
        return prior_a, prior_d

    # -- fitting --------------------------------------------------------------
    def _initial_theta(
        self,
        teams: list[str],
        prior_a: np.ndarray,
        prior_d: np.ndarray,
        x: np.ndarray,
        y: np.ndarray,
        warm_start: "DixonColes | None",
    ) -> tuple[np.ndarray, bool]:
        n = len(teams)
        theta = np.zeros(2 + 2 * n + 1)
        home_mean = max(float(np.mean(x)), 1e-3)
        away_mean = max(float(np.mean(y)), 1e-3)
        theta[0] = math.log(away_mean)
        theta[1] = math.log(home_mean) - math.log(away_mean)
        theta[2 : 2 + n] = prior_a
        theta[2 + n : 2 + 2 * n] = prior_d
        theta[-1] = 0.0

        if warm_start is None or not warm_start.teams:
            return theta, False
        theta[0] = warm_start.mu0
        theta[1] = warm_start.gamma
        for i, team in enumerate(teams):
            j = warm_start.index.get(team)
            if j is not None:
                theta[2 + i] = warm_start.attack[j]
                theta[2 + n + i] = warm_start.defence[j]
        theta[-1] = float(
            np.clip(warm_start.rho, self.rho_bounds[0], self.rho_bounds[1])
        )
        return theta, True

    def fit(
        self,
        matches: pd.DataFrame,
        asof,
        *,
        warm_start: "DixonColes | None" = None,
        season: int | None = None,
    ) -> "DixonColes":
        started = time.perf_counter()
        self.asof = pd.Timestamp(asof)
        current_season = int(season) if season is not None else season_of(self.asof)

        history = self.training_frame(matches, self.asof)
        # Expanding window truncated at the last `window_seasons` seasons.
        # Beyond ~4 half-lives the weights are under 6% and only cost time.
        first_season = current_season - self.window_seasons + 1
        window = history[history["season"] >= first_season]
        if window.empty:
            raise ValueError(f"no training matches before {self.asof.date()}")

        self.promoted = self.promoted_teams(history, current_season)

        teams = sorted(set(window["home_id"]) | set(window["away_id"]))
        index = {team: i for i, team in enumerate(teams)}
        n = len(teams)

        home_idx = window["home_id"].map(index).to_numpy(dtype="int64")
        away_idx = window["away_id"].map(index).to_numpy(dtype="int64")
        x = window["fthg"].to_numpy(dtype="float64")
        y = window["ftag"].to_numpy(dtype="float64")
        age = (self.asof - window["date"]).dt.days.to_numpy(dtype="float64")
        w = np.exp(-self.xi * age)

        prior_a, prior_d = self._priors(teams)
        theta0, warm = self._initial_theta(teams, prior_a, prior_d, x, y, warm_start)

        bounds = [(None, None)] * (2 + 2 * n)
        bounds.append(self.rho_bounds if self.fit_rho else (0.0, 0.0))
        if not self.fit_rho:
            theta0[-1] = 0.0

        counter: dict = {}
        result = minimize(
            objective_and_grad,
            theta0,
            args=(home_idx, away_idx, x, y, w, prior_a, prior_d, self.sigma, n,
                  counter),
            method="L-BFGS-B",
            jac=True,
            bounds=bounds,
            options={"maxiter": self.max_iter, "maxfun": 10 * self.max_iter},
        )

        mu0, gamma, a, d, rho = _unpack(result.x, n)
        self.teams, self.index = teams, index
        self.mu0, self.gamma = float(mu0), float(gamma)
        self.attack, self.defence = np.array(a), np.array(d)
        self.rho = float(rho)

        self.meta = FitMeta(
            n_matches=int(len(window)),
            n_teams=n,
            n_promoted=len(self.promoted & set(teams)),
            iterations=int(getattr(result, "nit", 0)),
            func_evals=int(counter.get("func_evals", 0)),
            objective=float(result.fun),
            converged=bool(result.success),
            message=str(result.message),
            tau_clipped=int(counter.get("tau_clipped", 0)),
            rho_at_bound=bool(
                self.fit_rho
                and min(
                    abs(self.rho - self.rho_bounds[0]),
                    abs(self.rho - self.rho_bounds[1]),
                )
                < 1e-6
            ),
            seconds=time.perf_counter() - started,
            warm_started=warm,
            window_from_season=first_season,
        )
        return self

    # -- prediction -----------------------------------------------------------
    def team_params(self, team_id: str) -> tuple[float, float]:
        """(a, d) for a team, falling back to the prior centre.

        A side with no matches in the window has zero pull from the
        likelihood, so its fitted parameters would be exactly the prior
        centre. Returning that centre directly is the same number, and it
        means a fixture list never has to be known at fit time.
        """
        j = self.index.get(team_id)
        if j is not None:
            return float(self.attack[j]), float(self.defence[j])
        # Absent from a six-season window means the side is about to play its
        # first match in the division, i.e. it is a promoted club.
        return self.pi

    def rates(self, fixtures: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        home = fixtures["home_id"].to_numpy()
        away = fixtures["away_id"].to_numpy()
        a_home = np.array([self.team_params(t)[0] for t in home])
        d_home = np.array([self.team_params(t)[1] for t in home])
        a_away = np.array([self.team_params(t)[0] for t in away])
        d_away = np.array([self.team_params(t)[1] for t in away])
        lam = np.exp(self.mu0 + self.gamma + a_home - d_away)
        mu = np.exp(self.mu0 + a_away - d_home)
        return lam, mu

    def predict_1x2(self, fixtures: pd.DataFrame) -> np.ndarray:
        if len(fixtures) == 0:
            return np.zeros((0, 3))
        lam, mu = self.rates(fixtures)
        return probs_1x2(lam, mu, self.rho, self.k_max)

    def strengths(self) -> pd.DataFrame:
        """Fitted a/d per team -- used to estimate pi over the TUNE period."""
        return pd.DataFrame(
            {
                "team_id": self.teams,
                "attack": self.attack,
                "defence": self.defence,
                "promoted": [t in self.promoted for t in self.teams],
            }
        )
