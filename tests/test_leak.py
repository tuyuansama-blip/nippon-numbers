"""Leak tests -- the ones that decide whether any of the rest means anything.

A backtest built on leaked information produces an excellent RPS and no
knowledge. Each test below attacks a different route by which a fact from
after kickoff could reach a forecast made before it, and the assertions are
bit-exact on purpose: "close enough" would hide exactly the small,
systematic contamination that matters.

Direct descendants of keiba's `tests/test_features_leak.py` (DESIGN.md 2.3).
"""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from footy.eval.walkforward import (
    check_fold_boundaries,
    make_folds,
    run_walkforward,
)
from footy.model.base import BaseModel
from footy.model.baseline import Climatology
from footy.model.dixon_coles import DixonColes

CUT = pd.Timestamp("2005-02-05")


def _fit(matches, asof, **kwargs):
    return DixonColes(**kwargs).fit(matches, asof)


def test_truncating_the_future_changes_nothing(league_matches):
    """Delete every match from D onward, refit at D: identical to the bit.

    The general leak test. Any dependence on a later result -- a global mean,
    a rolling window computed the wrong way round, a sort that reaches across
    the boundary -- shows up here as a mismatch.
    """
    full = _fit(league_matches, CUT)
    truncated = _fit(league_matches[league_matches["date"] < CUT], CUT)

    assert np.array_equal(full.attack, truncated.attack)
    assert np.array_equal(full.defence, truncated.defence)
    assert full.mu0 == truncated.mu0
    assert full.gamma == truncated.gamma
    assert full.rho == truncated.rho

    fixtures = league_matches[league_matches["date"] == CUT]
    assert len(fixtures) > 0
    assert np.array_equal(
        full.predict_1x2(fixtures), truncated.predict_1x2(fixtures)
    )


def test_rewriting_the_days_own_results_changes_no_forecast(league_matches):
    """Scramble the scores of day D itself; D's forecast must not move.

    The complement of truncation: that test proves nothing *after* the day
    leaks, this one proves nothing *from* the day leaks. `Time` only exists
    from 2019/20, so same-day matches are excluded outright rather than
    ordered within the day (DESIGN.md 2.3-1).
    """
    fixtures = league_matches[league_matches["date"] == CUT]
    assert len(fixtures) > 0
    before = _fit(league_matches, CUT).predict_1x2(fixtures)

    mutated = league_matches.copy()
    same_day = mutated["date"] == CUT
    mutated.loc[same_day, "fthg"] = 7
    mutated.loc[same_day, "ftag"] = 0
    mutated.loc[same_day, "ftr"] = "H"
    after = _fit(mutated, CUT).predict_1x2(mutated[mutated["date"] == CUT])

    assert np.array_equal(before, after)


def test_climatology_is_asof_scoped_too(league_matches):
    """The benchmark has to be honest or `gap_closed` is meaningless."""
    full = Climatology().fit(league_matches, CUT).rates
    truncated = (
        Climatology().fit(league_matches[league_matches["date"] < CUT], CUT).rates
    )
    assert np.array_equal(full, truncated)

    mutated = league_matches.copy()
    mutated.loc[mutated["date"] >= CUT, "ftr"] = "A"
    assert np.array_equal(Climatology().fit(mutated, CUT).rates, full)


def test_training_frame_excludes_the_asof_day(league_matches):
    frame = BaseModel.training_frame(league_matches, CUT)
    assert frame["date"].max() < CUT
    assert (frame["date"] == CUT).sum() == 0
    assert frame["fthg"].notna().all()


def test_fit_never_reaches_the_asof_day(league_matches):
    model = _fit(league_matches, CUT)
    window = league_matches[
        (league_matches["date"] < CUT)
        & (league_matches["season"] >= model.meta.window_from_season)
    ]
    assert model.meta.n_matches == len(window)


def test_folds_never_split_a_match_day(league_matches):
    for refit in ("date", "week"):
        folds = make_folds(league_matches, refit)
        check_fold_boundaries(folds)
        days = {}
        for key, group in folds:
            for day in group["date"].dt.date.unique():
                assert day not in days, f"{day} appears in two {refit} folds"
                days[day] = key
        assert len(days) == league_matches["date"].nunique()


def test_check_fold_boundaries_catches_a_split_day(league_matches):
    """The guard must actually fire -- a passing invariant proves nothing if
    the check cannot fail."""
    counts = league_matches["date"].value_counts()
    day = counts[counts >= 2].index[0]
    same_day = league_matches[league_matches["date"] == day]
    bad = [
        (pd.Timestamp("2000-01-01"), same_day.iloc[:1]),
        (pd.Timestamp("2000-01-02"), same_day.iloc[1:]),
    ]
    with pytest.raises(AssertionError):
        check_fold_boundaries(bad)


def test_every_fold_trains_strictly_before_its_first_kickoff(league_matches):
    for refit in ("date", "week"):
        for key, fold in make_folds(league_matches, refit):
            asof = fold["date"].min()
            train = league_matches[league_matches["date"] < asof]
            if train.empty:
                continue
            assert train["date"].max() < asof
            assert train["date"].max() < fold["date"].min()


def test_walkforward_predictions_are_invariant_to_deleting_later_seasons(
    league_matches,
):
    """Same fold, same forecast, whether or not later seasons exist at all."""
    full = run_walkforward(
        league_matches,
        season_from=2004,
        season_to=2004,
        models=("dc", "clim"),
        refit="week",
    )
    truncated = run_walkforward(
        league_matches[league_matches["season"] <= 2004],
        season_from=2004,
        season_to=2004,
        models=("dc", "clim"),
        refit="week",
    )
    assert full.frame["match_id"].tolist() == truncated.frame["match_id"].tolist()
    for name in ("dc", "clim"):
        assert np.array_equal(full.probs[name], truncated.probs[name])


def test_walkforward_ignores_a_folds_own_results_but_not_earlier_ones(
    league_matches,
):
    """Both directions, the way keiba's intraday test does it.

    Rewriting the scores *inside* one fold must leave that fold's forecasts
    untouched -- it is being predicted, not learned from. The same rewrite
    must move *later* folds, because they are entitled to train on it. A
    model whose forecasts never moved at all would pass the first half on its
    own while being broken.
    """
    base = run_walkforward(
        league_matches, season_from=2004, season_to=2004,
        models=("dc",), refit="week",
    )
    first_fold = base.frame["fold"].iloc[0]
    targets = set(base.frame.loc[base.frame["fold"] == first_fold, "match_id"])
    assert len(targets) > 1

    mutated = league_matches.copy()
    hit = mutated["match_id"].isin(targets)
    mutated.loc[hit, "fthg"] = 6
    mutated.loc[hit, "ftag"] = 0
    mutated.loc[hit, "ftr"] = "H"

    scrambled = run_walkforward(
        mutated, season_from=2004, season_to=2004, models=("dc",), refit="week",
    )
    inside = (base.frame["fold"] == first_fold).to_numpy()
    assert np.array_equal(base.probs["dc"][inside], scrambled.probs["dc"][inside])
    assert not np.array_equal(
        base.probs["dc"][~inside], scrambled.probs["dc"][~inside]
    )


def test_predict_never_accepts_market_probabilities():
    """DESIGN.md 5: a market blend must stay structurally impossible."""
    for cls in (DixonColes, Climatology):
        params = inspect.signature(cls.predict_1x2).parameters
        assert list(params) == ["self", "fixtures"], cls.__name__


def test_odds_columns_are_absent_from_the_model_path(league_matches):
    """Strip every price and the model must still fit and predict."""
    blind = league_matches.drop(columns=["psch", "pscd", "psca"])
    model = _fit(blind, CUT)
    fixtures = blind[blind["date"] == CUT]
    assert np.isfinite(model.predict_1x2(fixtures)).all()


# --- the parse itself is a leak surface (DESIGN.md 6.1) ----------------------
# "Getting this wrong silently breaks the date order, and the leak walks in."
# So the loader's three known landmines get regression tests here rather than
# being trusted to stay fixed.
_HEADER = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA"


def _write_csv(path, rows, header=_HEADER, encoding="utf-8"):
    path.write_text("\n".join([header, *rows]) + "\n", encoding=encoding)
    return path


def test_two_and_four_digit_years_parse_to_the_same_day(tmp_path):
    """The mixed-format parse, day-first. '02/03/04' is 2 March 2004."""
    from footy.data.load import read_raw_csv, normalise

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        [
            "E0,02/03/04,Arsenal,Chelsea,1,0,H,2.0,3.5,4.0",
            "E0,15/03/2004,Everton,Liverpool,0,0,D,2.6,3.2,3.0",
        ],
    )
    frame = normalise(read_raw_csv(path), 2003)
    assert list(frame["date"].dt.strftime("%Y-%m-%d")) == ["2004-03-02",
                                                           "2004-03-15"]
    assert frame["date"].is_monotonic_increasing


def test_ragged_rows_with_blank_surplus_fields_are_truncated(tmp_path):
    """2003/04 and 2004/05 carry rows wider than their own header."""
    from footy.data.load import read_raw_csv

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        [
            "E0,02/03/04,Arsenal,Chelsea,1,0,H,2.0,3.5,4.0,,,,,",
            "E0,03/03/04,Everton,Liverpool,0,0,D,2.6,3.2,3.0",
        ],
    )
    frame = read_raw_csv(path)
    assert len(frame) == 2
    assert list(frame.columns) == _HEADER.split(",")


def test_a_ragged_row_with_real_surplus_data_is_an_error(tmp_path):
    """Truncating is only safe because the surplus is blank -- prove it."""
    from footy.data.load import read_raw_csv

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        ["E0,02/03/04,Arsenal,Chelsea,1,0,H,2.0,3.5,4.0,SOMETHING"],
    )
    with pytest.raises(ValueError, match="surplus is not blank"):
        read_raw_csv(path)


def test_trailing_blank_lines_are_dropped(tmp_path):
    """1995/96, 1996/97 and 1999/00 end with ~170 empty rows."""
    from footy.data.load import read_raw_csv

    path = _write_csv(
        tmp_path / "E0_9596.csv",
        ["E0,19/08/95,Arsenal,Chelsea,1,0,H,,,", ",,,,,,,,,", ",,,,,,,,,"],
    )
    assert len(read_raw_csv(path)) == 1


def test_a_byte_order_mark_does_not_rename_the_first_column(tmp_path):
    """The 2025/26 file is UTF-8 with a BOM, which turns 'Div' into 'ï»¿Div'."""
    from footy.data.load import read_raw_csv

    path = tmp_path / "E0_2526.csv"
    path.write_bytes(
        b"\xef\xbb\xbf" + (_HEADER + "\nE0,15/08/25,Arsenal,Chelsea,1,0,H,2,3,4\n")
        .encode("utf-8")
    )
    assert list(read_raw_csv(path).columns)[0] == "Div"


def test_check_fails_on_an_unknown_team_spelling(tmp_path):
    from footy.data.check import run_checks
    from footy.data.load import normalise, read_raw_csv

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        ["E0,02/03/04,Arsenal,Notarealclub,1,0,H,2.0,3.5,4.0"],
    )
    result = run_checks(normalise(read_raw_csv(path), 2003))
    assert not result.ok
    assert any("unknown team" in p for p in result.problems)


def test_check_fails_when_ftr_disagrees_with_the_score(tmp_path):
    from footy.data.check import run_checks
    from footy.data.load import normalise, read_raw_csv

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        ["E0,02/03/04,Arsenal,Chelsea,1,0,A,2.0,3.5,4.0"],
    )
    result = run_checks(normalise(read_raw_csv(path), 2003))
    assert not result.ok
    assert any("FTR disagrees" in p for p in result.problems)


def test_check_fails_when_the_overround_is_impossible(tmp_path):
    """A column mix-up shows up as an overround outside [1.00, 1.10]."""
    from footy.data.check import run_checks
    from footy.data.load import normalise, read_raw_csv

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        ["E0,02/03/04,Arsenal,Chelsea,1,0,H,1.5,2.0,2.5"],
    )
    result = run_checks(normalise(read_raw_csv(path), 2003))
    assert not result.ok
    assert any("overround" in p for p in result.problems)


def test_check_fails_on_a_duplicated_fixture(tmp_path):
    from footy.data.check import run_checks
    from footy.data.load import normalise, read_raw_csv

    row = "E0,02/03/04,Arsenal,Chelsea,1,0,H,2.0,3.5,4.0"
    path = _write_csv(tmp_path / "E0_0304.csv", [row, row])
    result = run_checks(normalise(read_raw_csv(path), 2003))
    assert not result.ok
    assert any("duplicate" in p for p in result.problems)


def test_check_fails_when_a_date_lands_outside_its_season(tmp_path):
    """A day/month swap puts a fixture outside the Aug..Jul window."""
    from footy.data.check import run_checks
    from footy.data.load import normalise, read_raw_csv

    path = _write_csv(
        tmp_path / "E0_0304.csv",
        ["E0,02/03/99,Arsenal,Chelsea,1,0,H,2.0,3.5,4.0"],
    )
    result = run_checks(normalise(read_raw_csv(path), 2003))
    assert not result.ok
    assert any("season/date mismatch" in p for p in result.problems)
