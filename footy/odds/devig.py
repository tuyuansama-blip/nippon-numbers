"""Turning bookmaker prices into probabilities.

Two methods, and the report always shows both. Shin's method is the primary
one because it has a story -- the margin is what the book charges to protect
itself from insiders, so it falls harder on longshots -- but on Pinnacle's
1X2 the measured difference from plain normalisation is RPS 0.0001
(DESIGN.md 2.4). Printing both every time is how we keep proving that the
choice of de-vig is not what any conclusion rests on.

Odds are a benchmark only. Nothing here ever reaches the model's inputs
(DESIGN.md 2.3-3).
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import brentq

from footy.config import SHIN_Z_BOUNDS

_EPS = 1e-12


def implied(odds) -> np.ndarray:
    """Raw inverse odds. Rows sum to the overround, not to 1."""
    values = np.asarray(odds, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(values > 0, 1.0 / values, np.nan)
    return out


def overround(odds) -> np.ndarray:
    return np.nansum(implied(odds), axis=-1)


def multiplicative(odds) -> np.ndarray:
    """Plain normalisation: p_i = pi_i / sum(pi)."""
    pi = implied(odds)
    total = pi.sum(axis=-1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        return pi / total


def _shin_probs(pi: np.ndarray, s: float, z: float) -> np.ndarray:
    """p_i(z) from DESIGN.md 2.4."""
    inner = z * z + 4.0 * (1.0 - z) * pi * pi / s
    return (np.sqrt(inner) - z) / (2.0 * (1.0 - z))


def shin_one(odds) -> tuple[np.ndarray, float]:
    """Shin probabilities for a single market. Returns (p, z).

    `z` is the insider-trading fraction. It is solved on (0, 0.4); a market
    with no margin (sum of inverse odds <= 1) gives z = 0 and the method
    reduces to the identity.
    """
    pi = implied(odds)
    if not np.all(np.isfinite(pi)) or np.any(pi <= 0):
        return np.full(np.shape(pi), np.nan), float("nan")
    s = float(pi.sum())
    if s <= 1.0 + _EPS:
        # No margin to remove. p_i(0) = pi_i / sqrt(s), which is the identity
        # at s == 1 and is renormalised here for the s < 1 (arbitrage) case.
        return pi / s, 0.0

    lo, hi = SHIN_Z_BOUNDS

    def gap(z: float) -> float:
        return float(_shin_probs(pi, s, z).sum() - 1.0)

    lo_value = gap(lo)
    hi_value = gap(hi - 1e-12)
    if lo_value <= 0.0:                       # pragma: no cover - s > 1 forbids it
        return pi / s, 0.0
    if hi_value > 0.0:
        # A margin too fat for z < 0.4. Not reachable with Pinnacle 1X2
        # (measured overround 1.024); clamp rather than raise so one odd row
        # cannot abort a backtest.
        z = hi - 1e-12
        p = _shin_probs(pi, s, z)
        return p / p.sum(), float(z)

    z = float(brentq(gap, lo, hi - 1e-12, xtol=1e-12, rtol=1e-14, maxiter=200))
    p = _shin_probs(pi, s, z)
    return p / p.sum(), z


def shin(odds) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise Shin. `odds` is (n, k) or (k,). Returns (probs, z)."""
    values = np.asarray(odds, dtype="float64")
    if values.ndim == 1:
        p, z = shin_one(values)
        return p, np.asarray(z)
    probs = np.full(values.shape, np.nan)
    zs = np.full(values.shape[0], np.nan)
    for i in range(values.shape[0]):
        row = values[i]
        if not np.all(np.isfinite(row)) or np.any(row <= 0):
            continue
        probs[i], zs[i] = shin_one(row)
    return probs, zs


def devig(odds, method: str = "shin") -> np.ndarray:
    """`method` is 'shin' or 'multiplicative'."""
    if method == "shin":
        return shin(odds)[0]
    if method in ("multiplicative", "normalise", "normalize"):
        return multiplicative(odds)
    raise ValueError(f"unknown de-vig method: {method!r}")


def market_probs(matches, method: str = "shin") -> np.ndarray:
    """(n, 3) H/D/A probabilities from the PSCH/PSCD/PSCA columns.

    Rows without a complete Pinnacle close come back as NaN; the caller drops
    them from head-to-head comparisons and reports the count.
    """
    odds = matches[["psch", "pscd", "psca"]].to_numpy(dtype="float64")
    return devig(odds, method)
