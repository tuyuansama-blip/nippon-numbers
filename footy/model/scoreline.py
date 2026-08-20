"""Score matrix -> 1X2 (DESIGN.md 1.5).

The truncation at K=12 and the renormalisation are not cosmetic. `tau` makes
the corrected matrix stop being a probability distribution, and truncating a
Poisson tail removes a little more mass; dividing by the sum absorbs both at
once, which is why the renormalise comes *after* the correction and not
before it.
"""

from __future__ import annotations

import numpy as np
from scipy.special import gammaln

from footy.config import SCORE_K


def poisson_pmf_row(mean, k_max: int = SCORE_K) -> np.ndarray:
    """(n, k_max+1) Poisson pmf for 0..k_max, computed in log space."""
    values = np.atleast_1d(np.asarray(mean, dtype="float64"))
    ks = np.arange(k_max + 1, dtype="float64")
    log_pmf = (
        -values[:, None]
        + ks[None, :] * np.log(values[:, None])
        - gammaln(ks + 1.0)[None, :]
    )
    return np.exp(log_pmf)


def tau_matrix(lam, mu, rho) -> np.ndarray:
    """(n, 2, 2) Dixon-Coles correction for the four low-score cells."""
    lam = np.atleast_1d(np.asarray(lam, dtype="float64"))
    mu = np.atleast_1d(np.asarray(mu, dtype="float64"))
    rho = float(rho)
    out = np.empty((lam.size, 2, 2), dtype="float64")
    out[:, 0, 0] = 1.0 - lam * mu * rho
    out[:, 0, 1] = 1.0 + lam * rho
    out[:, 1, 0] = 1.0 + mu * rho
    out[:, 1, 1] = 1.0 - rho
    return out


def score_matrix(lam, mu, rho, k_max: int = SCORE_K) -> np.ndarray:
    """(n, k_max+1, k_max+1) of P(home=i, away=j), corrected and renormalised."""
    home = poisson_pmf_row(lam, k_max)
    away = poisson_pmf_row(mu, k_max)
    matrix = home[:, :, None] * away[:, None, :]
    matrix[:, :2, :2] *= tau_matrix(lam, mu, rho)
    matrix = np.maximum(matrix, 0.0)
    matrix /= matrix.sum(axis=(1, 2), keepdims=True)
    return matrix


def probs_1x2(lam, mu, rho, k_max: int = SCORE_K) -> np.ndarray:
    """(n, 3) H/D/A. Scalar inputs still return a (1, 3) array."""
    matrix = score_matrix(lam, mu, rho, k_max)
    size = matrix.shape[1]
    rows = np.arange(size)
    home_mask = rows[:, None] > rows[None, :]
    draw_mask = rows[:, None] == rows[None, :]
    away_mask = rows[:, None] < rows[None, :]
    probs = np.stack(
        [
            (matrix * home_mask).sum(axis=(1, 2)),
            (matrix * draw_mask).sum(axis=(1, 2)),
            (matrix * away_mask).sum(axis=(1, 2)),
        ],
        axis=1,
    )
    return probs / probs.sum(axis=1, keepdims=True)
