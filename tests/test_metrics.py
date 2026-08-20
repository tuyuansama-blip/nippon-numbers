"""Scoring rules and the paired confidence interval (DESIGN.md 2.5, 4).

The bootstrap tests are the ones that matter for the verdict: a seed that
does not reproduce makes the pass thresholds unfalsifiable, and a
match-level resample would quietly shrink the interval that decides
PASS versus WARN.
"""

from __future__ import annotations

import numpy as np
import pytest

from footy.eval.metrics import (
    block_bootstrap,
    calibration_table,
    draw_check,
    encode_outcome,
    gap_closed,
    logloss_array,
    market_decile_table,
    rps,
    rps_array,
)


def test_rps_known_values():
    assert rps([1.0, 0.0, 0.0], 0) == pytest.approx(0.0)
    assert rps([0.0, 1.0, 0.0], 1) == pytest.approx(0.0)
    assert rps([0.0, 0.0, 1.0], 2) == pytest.approx(0.0)
    # Certain of a home win, the away side wins: the worst possible score.
    assert rps([1.0, 0.0, 0.0], 2) == pytest.approx(1.0)
    assert rps([0.0, 0.0, 1.0], 0) == pytest.approx(1.0)
    # Uniform forecast, home win: cumulative (1/3, 2/3) vs (1, 1).
    assert rps([1 / 3, 1 / 3, 1 / 3], 0) == pytest.approx(
        0.5 * ((1 / 3 - 1) ** 2 + (2 / 3 - 1) ** 2)
    )


def test_rps_penalises_the_far_outcome_more_than_the_near_one():
    """A draw missed by a home forecast must cost less than an away win."""
    p = [0.6, 0.25, 0.15]
    assert rps(p, 0) < rps(p, 1) < rps(p, 2)


def test_rps_array_matches_the_scalar_definition():
    rng = np.random.default_rng(0)
    probs = rng.dirichlet([3, 2, 2], size=50)
    y = rng.integers(0, 3, size=50)
    expected = np.array([rps(probs[i], y[i]) for i in range(50)])
    assert np.allclose(rps_array(probs, y), expected)


def test_logloss_known_values():
    probs = np.array([[0.5, 0.25, 0.25], [0.1, 0.1, 0.8]])
    y = np.array([0, 2])
    assert np.allclose(logloss_array(probs, y), [-np.log(0.5), -np.log(0.8)])


def test_encode_outcome_rejects_junk():
    assert list(encode_outcome(["H", "D", "A"])) == [0, 1, 2]
    with pytest.raises(ValueError):
        encode_outcome(["H", "X"])


def test_gap_closed_endpoints():
    assert gap_closed(0.233, 0.233, 0.191) == pytest.approx(0.0)
    assert gap_closed(0.233, 0.191, 0.191) == pytest.approx(1.0)
    assert gap_closed(0.233, 0.212, 0.191) == pytest.approx(0.5)
    assert np.isnan(gap_closed(0.2, 0.19, 0.2))


def test_block_bootstrap_is_deterministic():
    rng = np.random.default_rng(1)
    values = rng.normal(0.01, 0.1, 800)
    blocks = np.repeat(np.arange(80), 10)
    first = block_bootstrap(values, blocks)
    second = block_bootstrap(values, blocks)
    assert first["lo"] == second["lo"]
    assert first["hi"] == second["hi"]
    assert first["se"] == second["se"]
    assert first["n_blocks"] == 80


def test_block_bootstrap_interval_brackets_the_mean():
    rng = np.random.default_rng(2)
    values = rng.normal(0.02, 0.15, 1000)
    blocks = np.repeat(np.arange(100), 10)
    out = block_bootstrap(values, blocks)
    assert out["lo"] < out["mean"] < out["hi"]


def test_block_bootstrap_is_wider_when_blocks_are_correlated():
    """Why matchweeks, not matches.

    Every observation inside a block is given the same value here, which is
    the extreme of the correlation the real folds have. Resampling matches
    would report a much tighter interval for the same information.
    """
    rng = np.random.default_rng(3)
    per_block = rng.normal(0.01, 0.1, 100)
    values = np.repeat(per_block, 10)
    correlated = np.repeat(np.arange(100), 10)
    independent = np.arange(1000)

    wide = block_bootstrap(values, correlated)
    narrow = block_bootstrap(values, independent)
    assert wide["se"] > 2.5 * narrow["se"]


def test_block_bootstrap_handles_degenerate_input():
    empty = block_bootstrap(np.array([]), np.array([]))
    assert empty["n"] == 0 and np.isnan(empty["mean"])
    single = block_bootstrap(np.array([0.1, 0.2]), np.array(["a", "a"]))
    assert single["n_blocks"] == 1 and np.isnan(single["lo"])


def test_calibration_table_recovers_a_known_frequency():
    n = 4000
    rng = np.random.default_rng(4)
    probs = np.tile([0.5, 0.25, 0.25], (n, 1))
    y = rng.choice([0, 1, 2], size=n, p=[0.5, 0.25, 0.25])
    table = calibration_table(probs, y)
    home = table[(table["outcome"] == "H") & (table["n"] > 0)]
    assert len(home) == 1
    assert home["observed"].iloc[0] == pytest.approx(0.5, abs=0.03)


def test_market_decile_table_pools_all_three_outcomes():
    rng = np.random.default_rng(5)
    market = rng.dirichlet([4, 3, 3], size=600)
    model = market.copy()
    y = np.array([rng.choice(3, p=row) for row in market])
    table = market_decile_table(model, market, y)
    assert table["n"].sum() == 3 * 600
    assert len(table) == 10
    # Model == market, so the model column must track the market column.
    assert np.allclose(table["model_mean"], table["market_mean"])


def test_draw_check_reports_the_signed_gap():
    probs = np.tile([0.4, 0.3, 0.3], (100, 1))
    y = np.array([1] * 25 + [0] * 75)
    out = draw_check(probs, y)
    assert out["mean_p_draw"] == pytest.approx(0.3)
    assert out["observed_draw_rate"] == pytest.approx(0.25)
    assert out["gap"] == pytest.approx(0.05)
