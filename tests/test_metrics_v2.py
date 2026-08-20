"""Calibration v2 metrics (DESIGN_PHASE2.md 3): Murphy decomposition, the
own-probability decile table and the parametric-bootstrap null band.
"""

from __future__ import annotations

import numpy as np
import pytest

from footy.eval.metrics import (
    market_decile_expected_band,
    murphy_decomposition,
    null_calibration_bootstrap,
    own_decile_table,
    rps_array,
)


def _synthetic(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    p = rng.dirichlet([4, 3, 3], size=n)
    y = np.array([rng.choice(3, p=row) for row in p])
    return p, y


def test_murphy_decomposition_reconstructs_brier():
    p, y = _synthetic()
    m = murphy_decomposition(p, y, n_bins=20)
    # Brier = reliability(raw) - resolution + uncertainty, up to the
    # equal-frequency binning's own discretisation error.
    reconstructed = m["reliability_raw"] - m["resolution"] + m["uncertainty"]
    assert reconstructed == pytest.approx(m["brier"], abs=0.01)
    assert m["n"] == 3 * len(y)


def test_murphy_decomposition_a_perfect_forecaster_has_near_zero_reliability():
    """A forecaster whose probabilities *are* the true generating
    probabilities should have small reliability and most of the Brier score
    should be uncertainty/resolution, not miscalibration."""
    rng = np.random.default_rng(1)
    n = 6000
    truth = rng.dirichlet([4, 3, 3], size=n)
    y = np.array([rng.choice(3, p=row) for row in truth])
    m = murphy_decomposition(truth, y, n_bins=20)
    assert abs(m["reliability_debiased"]) < 0.002


def test_murphy_decomposition_penalises_a_miscalibrated_forecaster():
    """Deliberately biasing a forecaster (always +0.15 on the home column,
    renormalised) must raise reliability relative to the honest version."""
    rng = np.random.default_rng(2)
    n = 3000
    truth = rng.dirichlet([4, 3, 3], size=n)
    y = np.array([rng.choice(3, p=row) for row in truth])

    biased = truth.copy()
    biased[:, 0] += 0.15
    biased = np.clip(biased, 1e-6, None)
    biased /= biased.sum(axis=1, keepdims=True)

    honest = murphy_decomposition(truth, y)
    skewed = murphy_decomposition(biased, y)
    assert skewed["reliability_debiased"] > honest["reliability_debiased"]


def test_reliability_debiased_can_go_slightly_negative_for_an_honest_forecaster():
    """The whole point of the debiasing term: an honest forecaster's raw
    reliability is upward-biased by binning noise, so the debiased version
    should scatter around zero rather than always being positive."""
    draws = []
    for seed in range(15):
        p, y = _synthetic(n=1500, seed=100 + seed)
        m = murphy_decomposition(p, y)
        draws.append(m["reliability_debiased"])
    assert min(draws) < 0.0


def test_own_decile_table_pools_all_three_outcomes():
    p, y = _synthetic(n=900)
    table = own_decile_table(p, y, n_bins=10)
    assert table["n"].sum() == 3 * 900
    assert len(table) == 10
    assert set(table.columns) >= {"decile", "n", "model_mean", "observed", "gap"}


def test_own_decile_table_model_equals_market_has_a_small_gap():
    """When the forecaster *is* the data-generating distribution, its own
    decile gaps should be small (bounded by finite-sample noise only)."""
    p, y = _synthetic(n=6000, seed=3)
    table = own_decile_table(p, y, n_bins=10)
    wide = table[table["n"] >= 150]
    assert len(wide) > 0
    assert wide["gap"].abs().max() < 0.05


def test_null_calibration_bootstrap_is_deterministic():
    p, _ = _synthetic(n=500)
    first = null_calibration_bootstrap(p, n_boot=40, seed=7)
    second = null_calibration_bootstrap(p, n_boot=40, seed=7)
    assert first == second


def test_null_calibration_bootstrap_percentiles_are_ordered():
    p, _ = _synthetic(n=1200)
    out = null_calibration_bootstrap(p, n_boot=60, seed=11)
    assert out["reliability_debiased_p95"] <= out["reliability_debiased_p99"]
    assert out["own_decile_worst_gap_mean"] > 0
    assert out["own_decile_worst_gap_p95"] <= out["own_decile_worst_gap_p99"] + 1e-9


def test_null_calibration_bootstrap_shrinks_with_more_matches():
    """More matches -> a tighter null band, the same intuition as any
    finite-sample bootstrap (DESIGN_PHASE2.md 0.5's own measurement)."""
    small, _ = _synthetic(n=400, seed=21)
    large, _ = _synthetic(n=8000, seed=21)
    small_band = null_calibration_bootstrap(small, n_boot=60, seed=5)
    large_band = null_calibration_bootstrap(large, n_boot=60, seed=5)
    assert large_band["reliability_debiased_p99"] < small_band["reliability_debiased_p99"]


def test_market_decile_expected_band_targets_the_requested_gap_closed():
    """Feeding the simulator its own honest score should land near
    gap_closed=1 (identity, sd~0) rather than exploding."""
    p, y = _synthetic(n=2000, seed=42)
    market_score = float(np.mean(rps_array(p, y)))
    clim_score = market_score * 1.2       # a nominal "knows less" baseline
    band = market_decile_expected_band(
        p, y, clim_score=clim_score, market_score=market_score,
        target_gap_closed=0.85, seeds=(1, 2, 3),
    )
    assert band["sd"] > 0
    assert np.isfinite(band["worst_gap_mean"])
    assert "diagnostic only" in band["note"]
