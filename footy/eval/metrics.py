"""Scoring rules, paired confidence intervals and calibration.

Two rules from DESIGN.md are enforced by the shapes of these functions rather
than by discipline:

* **Judge paired differences, never levels.** Season difficulty moves RPS
  between 0.180 and 0.209 on its own, so `block_bootstrap` takes a vector of
  per-match differences and there is no function here that puts a confidence
  interval on a single model's RPS (DESIGN.md 2.5).
* **Resample matchweeks, not matches.** Every match in a week shares one
  fitted theta, so their errors are correlated and match-level resampling
  would understate the interval -- the same reason keiba resamples races
  rather than bets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from footy.config import BOOT_ALPHA, BOOT_N, BOOT_SEED

OUTCOMES = ("H", "D", "A")
OUTCOME_INDEX = {"H": 0, "D": 1, "A": 2}
_EPS = 1e-15


def encode_outcome(ftr) -> np.ndarray:
    """'H'/'D'/'A' -> 0/1/2."""
    series = pd.Series(ftr).astype(str).str.strip().str.upper()
    codes = series.map(OUTCOME_INDEX)
    if codes.isna().any():
        bad = sorted(series[codes.isna()].unique())
        raise ValueError(f"unrecognised full-time results: {bad}")
    return codes.to_numpy(dtype="int64")


def rps(p, y: int) -> float:
    """Ranked probability score for one ordered 3-outcome forecast."""
    p = np.asarray(p, dtype="float64")
    e = np.zeros(3)
    e[int(y)] = 1.0
    cp, ce = np.cumsum(p), np.cumsum(e)
    return 0.5 * ((cp[0] - ce[0]) ** 2 + (cp[1] - ce[1]) ** 2)


def rps_array(probs, y) -> np.ndarray:
    """Row-wise RPS. `probs` is (n, 3) in H, D, A order."""
    p = np.asarray(probs, dtype="float64")
    y = np.asarray(y, dtype="int64")
    e = np.zeros_like(p)
    e[np.arange(len(y)), y] = 1.0
    cp, ce = np.cumsum(p, axis=1), np.cumsum(e, axis=1)
    diff = cp[:, :2] - ce[:, :2]
    return 0.5 * (diff**2).sum(axis=1)


def logloss_array(probs, y) -> np.ndarray:
    """Row-wise multiclass log loss."""
    p = np.clip(np.asarray(probs, dtype="float64"), _EPS, 1.0)
    y = np.asarray(y, dtype="int64")
    return -np.log(p[np.arange(len(y)), y])


def gap_closed(score_clim: float, score_model: float, score_market: float) -> float:
    """0 = no better than knowing nothing, 1 = level with the market.

    The direct transplant of keiba's `blind_gap_closed`. Works for RPS and
    log loss alike because both are losses.
    """
    span = float(score_clim) - float(score_market)
    if not np.isfinite(span) or abs(span) < 1e-12:
        return float("nan")
    return (float(score_clim) - float(score_model)) / span


def block_bootstrap(
    values,
    blocks,
    *,
    n_boot: int = BOOT_N,
    seed: int = BOOT_SEED,
    alpha: float = BOOT_ALPHA,
) -> dict:
    """Percentile CI for the mean of `values`, resampling whole blocks.

    `blocks` is one label per observation (the matchweek). Blocks are drawn
    with replacement and their observations pooled, so a week with a midweek
    round carries its natural weight.
    """
    values = np.asarray(values, dtype="float64")
    labels = np.asarray(blocks)
    mask = np.isfinite(values)
    values, labels = values[mask], labels[mask]

    out = {
        "mean": float(np.mean(values)) if values.size else float("nan"),
        "lo": float("nan"),
        "hi": float("nan"),
        "se": float("nan"),
        "n": int(values.size),
        "n_blocks": 0,
        "draws": np.array([]),
    }
    if values.size == 0:
        return out

    codes, uniques = pd.factorize(pd.Series(labels), sort=True)
    n_blocks = len(uniques)
    out["n_blocks"] = int(n_blocks)
    if n_blocks < 2:
        return out

    sums = np.bincount(codes, weights=values, minlength=n_blocks)
    counts = np.bincount(codes, minlength=n_blocks).astype("float64")

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_blocks, size=(n_boot, n_blocks))
    draws = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)

    out["lo"] = float(np.percentile(draws, 100 * alpha / 2))
    out["hi"] = float(np.percentile(draws, 100 * (1 - alpha / 2)))
    out["se"] = float(np.std(draws, ddof=1))
    out["draws"] = draws
    return out


def calibration_table(
    probs, y, *, edges=None, labels=OUTCOMES
) -> pd.DataFrame:
    """Predicted vs observed frequency per probability bucket, per outcome."""
    edges = edges if edges is not None else np.arange(0.0, 1.05, 0.05)
    p = np.asarray(probs, dtype="float64")
    y = np.asarray(y, dtype="int64")
    rows = []
    for k, label in enumerate(labels):
        frame = pd.DataFrame({"p": p[:, k], "hit": (y == k).astype("float64")})
        frame["bin"] = pd.cut(frame["p"], bins=edges, right=False,
                              include_lowest=True)
        grouped = frame.groupby("bin", observed=True).agg(
            n=("hit", "size"), p_mean=("p", "mean"), observed=("hit", "mean")
        )
        grouped = grouped.reset_index()
        grouped.insert(0, "outcome", label)
        rows.append(grouped)
    table = pd.concat(rows, ignore_index=True)
    table["bin"] = table["bin"].astype(str)
    table["gap"] = table["p_mean"] - table["observed"]
    return table


def market_decile_table(model_probs, market_probs, y, *, n_bins: int = 10):
    """Model vs reality inside deciles of the *market's* probability.

    All three outcomes are pooled into one column of (market p, model p, hit)
    triples, so a decile is "matches the market prices around 30%", not
    "home wins around 30%". Sorting by the sharpest available price is the
    demanding version of the test: it asks whether the model agrees with
    reality on the market's own partition.
    """
    market = np.asarray(market_probs, dtype="float64")
    model = np.asarray(model_probs, dtype="float64")
    y = np.asarray(y, dtype="int64")
    hits = np.zeros_like(market)
    hits[np.arange(len(y)), y] = 1.0

    frame = pd.DataFrame(
        {
            "market_p": market.reshape(-1),
            "model_p": model.reshape(-1),
            "hit": hits.reshape(-1),
        }
    ).dropna()
    if frame.empty:
        return pd.DataFrame(
            columns=["decile", "n", "market_mean", "model_mean", "observed", "gap"]
        )
    frame["decile"] = pd.qcut(
        frame["market_p"], q=n_bins, labels=False, duplicates="drop"
    )
    grouped = (
        frame.groupby("decile", observed=True)
        .agg(
            n=("hit", "size"),
            market_mean=("market_p", "mean"),
            model_mean=("model_p", "mean"),
            observed=("hit", "mean"),
        )
        .reset_index()
    )
    grouped["gap"] = grouped["model_mean"] - grouped["observed"]
    return grouped


def draw_check(probs, y) -> dict:
    """Dixon-Coles' known weak spot gets its own number."""
    p = np.asarray(probs, dtype="float64")
    y = np.asarray(y, dtype="int64")
    predicted = float(np.mean(p[:, 1])) if len(p) else float("nan")
    observed = float(np.mean(y == 1)) if len(y) else float("nan")
    return {
        "mean_p_draw": predicted,
        "observed_draw_rate": observed,
        "gap": predicted - observed,
    }


def summarise_scores(probs, y, blocks=None) -> dict:
    """Mean RPS and log loss, plus the block CI when blocks are supplied."""
    rps_values = rps_array(probs, y)
    ll_values = logloss_array(probs, y)
    out = {
        "n": int(len(y)),
        "rps": float(np.mean(rps_values)),
        "logloss": float(np.mean(ll_values)),
    }
    if blocks is not None:
        out["rps_ci"] = block_bootstrap(rps_values, blocks)
        out["logloss_ci"] = block_bootstrap(ll_values, blocks)
    return out
