"""Fold generation, the refit loop, and the frozen verdict table.

The walk-forward loop is where a protocol error would be least visible: it
produces a plausible number whether or not the folds are right. So the
structural facts get asserted -- every match predicted exactly once, all legs
scored on one identical match set, the run reloadable -- and the verdict
table is exercised at each of its five outcomes with hand-built inputs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from footy.config import TUNE_END
from footy.eval.report import VERDICTS, evaluate, verdict_block, write_report
from footy.eval.tune import estimate_pi, load_frozen_params, model_params
from footy.eval.walkforward import (
    load_run,
    make_folds,
    run_walkforward,
    save_run,
)


@pytest.fixture(scope="module")
def run(league_matches):
    return run_walkforward(
        league_matches,
        season_from=2005,
        season_to=2006,
        models=("dc", "clim"),
        refit="week",
    )


def test_folds_cover_every_match_exactly_once(league_matches):
    for refit in ("date", "week"):
        folds = make_folds(league_matches, refit)
        ids = pd.concat([group["match_id"] for _, group in folds])
        assert len(ids) == len(league_matches)
        assert set(ids) == set(league_matches["match_id"])


def test_folds_are_in_chronological_order(league_matches):
    for refit in ("date", "week"):
        keys = [key for key, _ in make_folds(league_matches, refit)]
        assert keys == sorted(keys)


def test_week_refit_is_cheaper_than_date_refit(league_matches):
    by_date = make_folds(league_matches, "date")
    by_week = make_folds(league_matches, "week")
    assert len(by_week) < len(by_date)


def test_unknown_refit_mode_is_rejected(league_matches):
    with pytest.raises(ValueError):
        make_folds(league_matches, "fortnight")


def test_run_predicts_every_test_match_once(run, league_matches):
    expected = league_matches[
        (league_matches["season"] >= 2005) & (league_matches["season"] <= 2006)
    ]
    assert len(run.frame) == len(expected)
    assert set(run.frame["match_id"]) == set(expected["match_id"])
    assert run.frame["match_id"].is_unique


def test_every_leg_is_scored_on_the_same_matches(run):
    for name, matrix in run.probs.items():
        assert matrix.shape == (len(run.frame), 3), name
        assert np.allclose(matrix.sum(axis=1), 1.0), name
        assert np.isfinite(matrix).all(), name


def test_market_columns_are_populated(run):
    shin = run.market("shin")
    mult = run.market("multiplicative")
    assert np.allclose(shin.sum(axis=1), 1.0)
    assert np.allclose(mult.sum(axis=1), 1.0)
    assert run.has_market.all()


def test_blocks_are_matchweeks_not_matches(run):
    blocks = run.blocks
    assert len(blocks) == len(run.frame)
    assert len(set(blocks)) < len(blocks)          # weeks group several matches
    assert len(set(blocks)) == run.frame["week_start"].nunique()


def test_fit_metadata_is_recorded_per_fold(run):
    fits = run.fits
    assert len(fits) == run.frame["week_start"].nunique()
    assert (fits["dc_seconds"] > 0).all()
    assert fits["dc_converged"].all()
    assert (fits["dc_tau_clipped"] == 0).all()
    assert fits["n_train"].is_monotonic_increasing


def test_warm_start_is_used_after_the_first_fold(run):
    assert run.fits["dc_iters"].iloc[1:].mean() < run.fits["dc_iters"].iloc[0]


def test_beats_climatology_on_data_it_generated(run):
    """Sanity floor: the model must at least beat knowing nothing."""
    from footy.eval.metrics import rps_array

    y = run.y
    dc = float(np.mean(rps_array(run.probs["dc"], y)))
    clim = float(np.mean(rps_array(run.probs["clim"], y)))
    market = float(np.mean(rps_array(run.market("shin"), y)))
    assert market < dc < clim


def test_run_round_trips_through_disk(run, tmp_path):
    save_run(run, "unit_test", tmp_path)
    back = load_run("unit_test", tmp_path)
    assert back.frame["match_id"].tolist() == run.frame["match_id"].tolist()
    for name, matrix in run.probs.items():
        assert np.allclose(back.probs[name], matrix)
    assert back.params["refit"] == run.params["refit"]


def test_missing_run_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_run("nope", tmp_path)


def test_empty_season_range_is_rejected(league_matches):
    with pytest.raises(ValueError):
        run_walkforward(league_matches, season_from=2100, season_to=2101)


def test_evaluation_and_report_are_produced(run, tmp_path):
    ev = evaluate(run, primary="dc", run_id="unit_test")
    assert ev.verdict in VERDICTS
    assert ev.n_scored == len(run.frame)
    assert ev.n_market_missing == 0
    assert set(ev.by_season["season"]) == {"2005-06", "2006-07"}
    assert "== 判定 ==" in verdict_block(ev)

    path = write_report(ev, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert ev.verdict in text
    assert (tmp_path / "calibration_unit_test.png").exists()
    assert (tmp_path / "rps_by_season_unit_test.png").exists()


def test_market_missing_rows_are_excluded_and_counted(run):
    holed = run.frame.copy()
    holed.loc[holed.index[:20], ["market_shin_h", "market_shin_d",
                                "market_shin_a"]] = np.nan
    patched = type(run)(
        frame=holed, probs=run.probs, fits=run.fits, params=run.params
    )
    ev = evaluate(patched, primary="dc", run_id="holes")
    assert ev.n_market_missing == 20
    assert ev.n_scored == len(run.frame) - 20


# --- the frozen verdict table ------------------------------------------------
def _synthetic_evaluation(run, *, shift: float, jitter: float = 0.0):
    """A run whose model probabilities are the market's, nudged towards
    climatology by `shift`. Lets each verdict branch be reached on demand."""
    market = run.market("shin")
    clim = run.probs["clim"]
    blended = (1 - shift) * market + shift * clim
    if jitter:
        blended = np.clip(blended + jitter, 1e-6, 1.0)
        blended /= blended.sum(axis=1, keepdims=True)
    probs = dict(run.probs)
    probs["dc"] = blended
    return type(run)(
        frame=run.frame, probs=probs, fits=run.fits, params=run.params
    )


def test_matching_the_market_is_audited_not_celebrated(run):
    """A model that beats Pinnacle by a clear margin is a suspect."""
    market = run.market("shin")
    y = run.y
    cheating = market.copy()
    # Nudge every forecast towards the outcome that actually happened.
    cheating[np.arange(len(y)), y] += 0.25
    cheating /= cheating.sum(axis=1, keepdims=True)
    probs = dict(run.probs)
    probs["dc"] = cheating
    patched = type(run)(
        frame=run.frame, probs=probs, fits=run.fits, params=run.params
    )
    ev = evaluate(patched, primary="dc", run_id="audit")
    assert ev.verdict == "FAIL-AUDIT"
    assert "leak" in " ".join(ev.reasons).lower()


def test_knowing_nothing_fails(run):
    probs = dict(run.probs)
    probs["dc"] = run.probs["clim"]
    patched = type(run)(
        frame=run.frame, probs=probs, fits=run.fits, params=run.params
    )
    ev = evaluate(patched, primary="dc", run_id="floor")
    assert ev.verdict == "FAIL"
    assert ev.checks["gap_closed_rps"] == pytest.approx(0.0, abs=1e-9)


def test_gap_closed_is_one_when_the_model_is_the_market(run):
    ev = evaluate(_synthetic_evaluation(run, shift=0.0), primary="dc",
                  run_id="mirror")
    assert ev.checks["gap_closed_rps"] == pytest.approx(1.0, abs=1e-9)
    assert ev.checks["d_rps"] == pytest.approx(0.0, abs=1e-12)


def test_a_broken_draw_rate_fails_however_good_the_gap(run):
    """Calibration is mandatory: the FAIL row carries no gap_closed clause."""
    market = run.market("shin")
    skewed = market.copy()
    skewed[:, 1] += 0.15
    skewed[:, 0] -= 0.15
    skewed = np.clip(skewed, 1e-6, 1.0)
    skewed /= skewed.sum(axis=1, keepdims=True)
    probs = dict(run.probs)
    probs["dc"] = skewed
    patched = type(run)(
        frame=run.frame, probs=probs, fits=run.fits, params=run.params
    )
    ev = evaluate(patched, primary="dc", run_id="draws")
    assert not ev.checks["draw_ok"]
    assert ev.verdict == "FAIL"


def test_verdict_block_names_every_threshold(run):
    ev = evaluate(run, primary="dc", run_id="block")
    text = verdict_block(ev)
    for token in ("gap_closed RPS", "gap_closed LL", "paired d_RPS",
                  "calibration", "verdict"):
        assert token in text


# --- frozen parameters -------------------------------------------------------
def test_defaults_are_used_when_tuning_has_not_run(tmp_path):
    assert load_frozen_params(tmp_path / "absent.json") is None
    params = model_params(None)
    assert params["half_life_days"] == 365.0
    assert params["sigma"] == 0.35
    assert params["pi"] == (0.0, 0.0)
    assert "tune not run" in params["source"]


def test_frozen_parameters_override_the_defaults(tmp_path):
    import json

    path = tmp_path / "frozen.json"
    path.write_text(
        json.dumps(
            {
                "half_life_days": 180.0,
                "sigma": 0.25,
                "pi": [-0.2, -0.15],
                "window_seasons": 5,
                "tune_from": 2000,
                "tune_through": TUNE_END,
                "created_at": "2026-01-01T00:00:00",
            }
        )
    )
    params = model_params(load_frozen_params(path))
    assert params["half_life_days"] == 180.0
    assert params["pi"] == (-0.2, -0.15)
    assert "frozen_params.json" in params["source"]


def test_pi_estimate_finds_promoted_sides_weaker(league_matches):
    """The synthetic league promotes weaker clubs; pi must notice."""
    pi, table = estimate_pi(
        league_matches,
        season_from=2002,
        season_to=2005,
        half_life_days=float("inf"),
        sigma=0.35,
    )
    assert len(table) > 0
    assert pi[0] < 0.0 and pi[1] < 0.0
