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

from footy.config import BOOT_ALPHA, BOOT_N, BOOT_SEED, MURPHY_BINS, NULL_BOOT_N, NULL_BOOT_SEED

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


# ==============================================================================
# Phase 2 (DESIGN_PHASE2.md 3): the acceptance conditions move from the
# market-decile table (a resolution measure, not a calibration one -- 2.1) to
# an unbiased Murphy decomposition and an own-probability decile table. The
# market-decile table itself is *not* deleted: it stays in the report as a
# diagnostic, alongside the expected band a perfectly-calibrated forecaster of
# the same resolution would show there (3.2).
# ==============================================================================


def _equal_freq_bin_stats(p_flat: np.ndarray, h_flat: np.ndarray, n_bins: int):
    """Sort-based equal-frequency binning: `n_bins` groups of (nearly) equal
    size over `p_flat`, each with its count, mean forecast and mean hit rate.

    Used instead of `pd.qcut` in every hot path (Murphy decomposition and,
    above all, the 200-draw null bootstrap) because a plain `np.argsort` plus
    a slice loop is an order of magnitude faster than repeated pandas binning
    at the sizes OOS-LEAGUES produces (n ~ 60,000 x3 pooled rows x200 draws).
    """
    n = len(p_flat)
    if n == 0:
        empty = np.array([], dtype="float64")
        return empty, empty, empty
    order = np.argsort(p_flat, kind="mergesort")
    p_sorted = p_flat[order]
    h_sorted = h_flat[order]
    edges = np.linspace(0, n, n_bins + 1).astype("int64")
    counts, p_means, h_means = [], [], []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        counts.append(hi - lo)
        p_means.append(float(p_sorted[lo:hi].mean()))
        h_means.append(float(h_sorted[lo:hi].mean()))
    return (
        np.array(counts, dtype="float64"),
        np.array(p_means, dtype="float64"),
        np.array(h_means, dtype="float64"),
    )


def _pool(probs, y) -> tuple[np.ndarray, np.ndarray]:
    """(p, y) -> one flat (p_flat, hit_flat) pair, 3 rows per match (H/D/A),
    the same pooling `market_decile_table` uses (DESIGN_PHASE2.md 3.1's
    "3アウトカム プール")."""
    p = np.asarray(probs, dtype="float64")
    y = np.asarray(y, dtype="int64")
    hits = np.zeros_like(p)
    hits[np.arange(len(y)), y] = 1.0
    mask = np.isfinite(p).all(axis=1)
    return p[mask].reshape(-1), hits[mask].reshape(-1)


def murphy_decomposition(probs, y, *, n_bins: int = MURPHY_BINS) -> dict:
    """Brier = reliability - resolution + uncertainty, 3-outcome pooled,
    equal-frequency bins (DESIGN_PHASE2.md 0.1, 2.2).

    `reliability_debiased` subtracts the bin-count bias
    `sum(o_b * (1 - o_b)) / N` from the raw reliability, per the definition
    fixed in DESIGN_PHASE2.md 3.1 -- the raw number is dominated by how many
    bins were used and how large n is, so CAL-1 is judged on the debiased
    figure only.
    """
    p_flat, h_flat = _pool(probs, y)
    n = len(p_flat)
    if n == 0:
        return {
            "n": 0, "brier": float("nan"), "reliability_raw": float("nan"),
            "reliability_debiased": float("nan"), "resolution": float("nan"),
            "uncertainty": float("nan"), "n_bins": 0,
        }
    brier = float(np.mean((p_flat - h_flat) ** 2))
    obar = float(np.mean(h_flat))
    uncertainty = obar * (1.0 - obar)

    counts, p_means, h_means = _equal_freq_bin_stats(p_flat, h_flat, n_bins)
    reliability_raw = float(np.sum(counts * (p_means - h_means) ** 2) / n)
    resolution = float(np.sum(counts * (h_means - obar) ** 2) / n)
    bias = float(np.sum(h_means * (1.0 - h_means)) / n)
    reliability_debiased = reliability_raw - bias

    return {
        "n": int(n),
        "brier": brier,
        "reliability_raw": reliability_raw,
        "reliability_debiased": reliability_debiased,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "n_bins": int(len(counts)),
    }


def own_decile_table(model_probs, y, *, n_bins: int = 10) -> pd.DataFrame:
    """CAL-2: deciles of the model's *own* probability, not the market's
    (DESIGN_PHASE2.md 3.1). Same pooling as `market_decile_table`, sorted by
    a different column -- the point of the replacement is exactly that the
    partition the model is judged on is its own scale, not a scale it may
    legitimately carry less information than.
    """
    p = np.asarray(model_probs, dtype="float64")
    y = np.asarray(y, dtype="int64")
    hits = np.zeros_like(p)
    hits[np.arange(len(y)), y] = 1.0
    frame = pd.DataFrame(
        {"model_p": p.reshape(-1), "hit": hits.reshape(-1)}
    ).dropna()
    if frame.empty:
        return pd.DataFrame(columns=["decile", "n", "model_mean", "observed", "gap"])
    frame["decile"] = pd.qcut(frame["model_p"], q=n_bins, labels=False,
                               duplicates="drop")
    grouped = (
        frame.groupby("decile", observed=True)
        .agg(n=("hit", "size"), model_mean=("model_p", "mean"),
             observed=("hit", "mean"))
        .reset_index()
    )
    grouped["gap"] = grouped["model_mean"] - grouped["observed"]
    return grouped


def null_calibration_bootstrap(
    probs, *, n_boot: int = NULL_BOOT_N, seed: int = NULL_BOOT_SEED,
    n_bins: int = MURPHY_BINS, decile_bins: int = 10, min_bin_n: int = 150,
) -> dict:
    """Parametric bootstrap null (DESIGN_PHASE2.md 0.5, 0.6, 3.1): resample
    outcomes *from the model's own predicted probabilities* -- i.e. simulate
    a forecaster that is calibrated by construction -- and see how large
    `reliability_debiased` and the own-decile worst `|gap|` come out purely
    from finite-sample noise. CAL-1's threshold is `max(0.02*span, this p99)`
    and CAL-2 is read against this band as a sanity floor, not a threshold.

    Vectorised outcome sampling (inverse-CDF on a `(n_boot, n)` uniform
    draw) plus the sort-based binning above is what keeps 200 draws over
    OOS-LEAGUES' ~60,000 matches tractable.
    """
    p = np.asarray(probs, dtype="float64")
    mask = np.isfinite(p).all(axis=1)
    p = p[mask]
    n, k = p.shape
    if n == 0:
        nan = float("nan")
        return {
            "n_boot": 0, "seed": seed,
            "reliability_debiased_mean": nan, "reliability_debiased_sd": nan,
            "reliability_debiased_p95": nan, "reliability_debiased_p99": nan,
            "own_decile_worst_gap_mean": nan, "own_decile_worst_gap_p95": nan,
            "own_decile_worst_gap_p99": nan,
        }

    cum = np.cumsum(p, axis=1)
    rng = np.random.default_rng(seed)
    u = rng.random((n_boot, n))
    sims = (u[:, :, None] > cum[None, :, :]).sum(axis=2)
    sims = np.clip(sims, 0, k - 1)

    p_flat = p.reshape(-1)
    rel_draws = np.empty(n_boot)
    decile_draws = np.empty(n_boot)
    for b in range(n_boot):
        hits = np.zeros_like(p)
        hits[np.arange(n), sims[b]] = 1.0
        h_flat = hits.reshape(-1)

        counts, p_means, h_means = _equal_freq_bin_stats(p_flat, h_flat, n_bins)
        rel_raw = float(np.sum(counts * (p_means - h_means) ** 2) / len(p_flat))
        bias = float(np.sum(h_means * (1.0 - h_means)) / len(p_flat))
        rel_draws[b] = rel_raw - bias

        counts2, p2, h2 = _equal_freq_bin_stats(p_flat, h_flat, decile_bins)
        wide = counts2 >= min_bin_n
        gaps = np.abs(p2 - h2)
        decile_draws[b] = float(np.max(gaps[wide])) if wide.any() else np.nan

    def pct(arr, q):
        finite = arr[np.isfinite(arr)]
        return float(np.percentile(finite, q)) if finite.size else float("nan")

    finite_decile = decile_draws[np.isfinite(decile_draws)]
    decile_mean = float(finite_decile.mean()) if finite_decile.size else float("nan")

    return {
        "n_boot": int(n_boot),
        "seed": int(seed),
        "reliability_debiased_mean": float(np.mean(rel_draws)),
        "reliability_debiased_sd": float(np.std(rel_draws, ddof=1)),
        "reliability_debiased_p95": pct(rel_draws, 95),
        "reliability_debiased_p99": pct(rel_draws, 99),
        "own_decile_worst_gap_mean": decile_mean,
        "own_decile_worst_gap_p95": pct(decile_draws, 95),
        "own_decile_worst_gap_p99": pct(decile_draws, 99),
    }


def _temperature_fit(log_p_noisy: np.ndarray, y: np.ndarray) -> float:
    """The single scalar `T` minimising NLL of `softmax(log_p_noisy / T)`."""
    from scipy.optimize import minimize_scalar

    def nll(temp: float) -> float:
        z = log_p_noisy / temp
        z = z - z.max(axis=1, keepdims=True)
        q = np.exp(z)
        q /= q.sum(axis=1, keepdims=True)
        return float(np.mean(-np.log(np.clip(q[np.arange(len(y)), y], 1e-12, 1.0))))

    result = minimize_scalar(nll, bounds=(0.05, 8.0), method="bounded")
    return float(result.x)


def _simulate_calibrated_forecaster(
    market_probs: np.ndarray, y: np.ndarray, sd: float, seed: int
) -> np.ndarray:
    """A forecaster with resolution controlled by `sd`, recalibrated to be
    honest on its own scale (DESIGN_PHASE2.md 2.1's construction).

    Simplification, disclosed: DESIGN_PHASE2.md 2.1 recalibrates with
    isotonic regression; this recalibrates with a single fitted temperature.
    Both remove first-order miscalibration from the noise injection: the
    point of this function is only to control *resolution* while staying
    approximately honest, for a diagnostic band that is never part of the
    verdict (DESIGN_PHASE2.md 3.2).
    """
    rng = np.random.default_rng(seed)
    log_p = np.log(np.clip(market_probs, 1e-12, 1.0))
    noisy = log_p + rng.normal(0.0, sd, log_p.shape)
    temp = _temperature_fit(noisy, y)
    z = noisy / temp
    z -= z.max(axis=1, keepdims=True)
    q = np.exp(z)
    return q / q.sum(axis=1, keepdims=True)


def market_decile_expected_band(
    market_probs, y, *, clim_score: float, market_score: float,
    target_gap_closed: float, seeds=(1, 2, 3, 4, 5, 6, 7, 8),
    n_bins: int = 10, min_bin_n: int = 150,
) -> dict:
    """The market-decile band a perfectly-honest forecaster of *this run's*
    resolution would show (DESIGN_PHASE2.md 3.2, 2.1). Diagnostic only --
    the market-decile table itself is never judged (DESIGN_PHASE2.md 3.2).
    """
    from footy.eval.metrics import gap_closed as _gap_closed  # local, explicit

    market_probs = np.asarray(market_probs, dtype="float64")
    y = np.asarray(y, dtype="int64")

    def gap_at(sd: float, probe_seeds) -> float:
        vals = []
        for s in probe_seeds:
            p = _simulate_calibrated_forecaster(market_probs, y, sd, s)
            score = float(np.mean(rps_array(p, y)))
            vals.append(_gap_closed(clim_score, score, market_score))
        return float(np.mean(vals))

    lo, hi = 1e-3, 1.5
    probe = seeds[:3]
    g_lo, g_hi = gap_at(lo, probe), gap_at(hi, probe)
    sd = hi
    if g_lo >= target_gap_closed >= g_hi:
        for _ in range(12):
            mid = 0.5 * (lo + hi)
            if gap_at(mid, probe) > target_gap_closed:
                lo = mid
            else:
                hi = mid
        sd = 0.5 * (lo + hi)

    worst, tops, bottoms = [], [], []
    for s in seeds:
        p = _simulate_calibrated_forecaster(market_probs, y, sd, s)
        dec = market_decile_table(p, market_probs, y, n_bins=n_bins)
        wide = dec[dec["n"] >= min_bin_n]
        if len(wide):
            worst.append(float(wide["gap"].abs().max()))
        if len(dec):
            tops.append(float(dec.iloc[-1]["gap"]))
            bottoms.append(float(dec.iloc[0]["gap"]))

    return {
        "sd": float(sd),
        "target_gap_closed": float(target_gap_closed),
        "n_seeds": len(seeds),
        "worst_gap_mean": float(np.mean(worst)) if worst else float("nan"),
        "worst_gap_sd": float(np.std(worst, ddof=1)) if len(worst) > 1 else float("nan"),
        "top_decile_gap_mean": float(np.mean(tops)) if tops else float("nan"),
        "bottom_decile_gap_mean": float(np.mean(bottoms)) if bottoms else float("nan"),
        "note": (
            "simplified stand-in for DESIGN_PHASE2.md 2.1: single fitted "
            "temperature, not full isotonic recalibration -- diagnostic "
            "only, never part of the verdict"
        ),
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
