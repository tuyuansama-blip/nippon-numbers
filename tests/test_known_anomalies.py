"""The known-source-anomaly exception list (footy/data/known_anomalies.py).

The contract under test: a listed (div, date, home, away) row loses exactly
the listed odds columns, every application is reported with its mandatory
reason, the operation is idempotent, and `run_checks_j1` judges the
overround bands only *after* the exclusion -- without ever widening the
bands themselves.
"""

import io

import numpy as np
import pandas as pd
import pytest

from footy.data.check import format_result, run_checks_j1
from footy.data.known_anomalies import (
    KNOWN_ANOMALIES,
    KnownAnomaly,
    apply_known_anomalies,
)
from footy.data.source_fd_new import build_j1_matches, normalise_jpn

_HEADER = (
    "Country,League,Season,Date,Time,Home,Away,HG,AG,Res,"
    "PSCH,PSCD,PSCA,BFECH,BFECD,BFECA"
)


def _row(season, date, home, away, hg, ag, res, psc=(1.9, 3.4, 4.1), bfec=("", "", "")):
    return (
        f"Japan, J1 League,{season},{date},19:00,{home},{away},{hg},{ag},{res},"
        f"{psc[0]},{psc[1]},{psc[2]},{bfec[0]},{bfec[1]},{bfec[2]}"
    )


def _frame(rows):
    return pd.read_csv(io.StringIO("\n".join([_HEADER, *rows]) + "\n"))


# The two real anomalous rows, reproduced with their real source values.
_SENDAI_ROW = _row(2013, "13/07/2013", "Vegalta Sendai", "Iwata", 1, 1, "D",
                   psc=(2.0, 3.52, 3.09))
_URAWA_ROW = _row(2024, "22/11/2024", "Urawa Reds", "Kawasaki Frontale", 1, 1, "D",
                  psc=(2.61, 3.22, 2.81), bfec=(2.34, 3.6, 2.8))
_CLEAN_ROW = _row(2013, "06/07/2013", "Kashima Antlers", "Kyoto", 2, 0, "H")


def test_every_registered_anomaly_has_a_reason_and_columns():
    assert KNOWN_ANOMALIES  # the list is non-empty by construction
    for entry in KNOWN_ANOMALIES:
        assert entry.reason.strip()
        assert entry.columns


def test_reason_is_mandatory():
    with pytest.raises(ValueError, match="reason"):
        KnownAnomaly(
            div="JPN1", date="2013-07-13", home_team="A", away_team="B",
            columns=("psch",), reason="   ",
        )


def test_columns_are_mandatory():
    with pytest.raises(ValueError, match="columns"):
        KnownAnomaly(
            div="JPN1", date="2013-07-13", home_team="A", away_team="B",
            columns=(), reason="some reason",
        )


def test_apply_nans_only_listed_columns_on_matching_row():
    matches = normalise_jpn(_frame([_CLEAN_ROW, _SENDAI_ROW]))
    out, applied = apply_known_anomalies(matches)

    sendai = out[out["home_team"] == "Vegalta Sendai"].iloc[0]
    assert np.isnan(sendai["psch"]) and np.isnan(sendai["pscd"]) and np.isnan(sendai["psca"])
    # the row itself survives: result and non-listed columns untouched
    assert sendai["fthg"] == 1 and sendai["ftag"] == 1 and sendai["ftr"] == "D"
    clean = out[out["home_team"] == "Kashima Antlers"].iloc[0]
    assert clean["psch"] == 1.9

    assert len(applied) == 1
    entry = applied[0]
    assert entry["n_rows"] == 1 and entry["n_cleared"] == 1
    assert entry["columns"] == ("psch", "pscd", "psca")
    assert "PSCH=2.00" in entry["reason"]
    # the input frame is not mutated
    assert matches.loc[matches["home_team"] == "Vegalta Sendai", "psch"].iloc[0] == 2.0


def test_apply_is_idempotent():
    matches = normalise_jpn(_frame([_SENDAI_ROW, _URAWA_ROW]))
    once, applied_once = apply_known_anomalies(matches)
    twice, applied_twice = apply_known_anomalies(once)

    pd.testing.assert_frame_equal(once, twice)
    assert [e["n_cleared"] for e in applied_once] == [1, 1]
    # second pass still reports the matches, but nothing left to clear
    assert [e["n_cleared"] for e in applied_twice] == [0, 0]


def test_apply_is_a_noop_without_a_matching_row():
    matches = normalise_jpn(_frame([_CLEAN_ROW]))
    out, applied = apply_known_anomalies(matches)
    assert applied == []
    pd.testing.assert_frame_equal(out, matches)


def test_run_checks_j1_goes_green_on_the_anomalous_rows_and_reports_them():
    """Without the exception list both rows breach their overround band
    (PSC 1.1077 > 1.10, BFEC 1.0623 > 1.05). With it, the check is judged on
    the post-exclusion data and each exclusion is disclosed as a note."""
    matches = normalise_jpn(_frame([_CLEAN_ROW, _SENDAI_ROW, _URAWA_ROW]))

    result = run_checks_j1(matches)
    assert not any("overround" in p for p in result.problems)
    assert result.stats["known_anomalies_applied"] == 2
    assert sum("known data anomaly excluded" in n for n in result.notes) == 2
    assert any("Vegalta Sendai" in n for n in result.notes)
    assert any("Urawa Reds" in n for n in result.notes)

    text = format_result(result)
    assert "known anomalies 2 excluded" in text


def test_run_checks_j1_still_fails_on_an_unlisted_overround_breach():
    """The band itself is untouched: an anomalous row *not* on the list still
    turns the check red -- the exception list must never blunt regression
    detection."""
    bad_unlisted = _row(2013, "20/07/2013", "Kashima Antlers", "Kyoto", 0, 0, "D",
                        psc=(2.0, 3.0, 3.0))   # overround ~1.167
    matches = normalise_jpn(_frame([_CLEAN_ROW, bad_unlisted]))

    result = run_checks_j1(matches)
    assert not result.ok
    assert any("PSC overround" in p for p in result.problems)


def test_build_j1_matches_applies_the_exclusion_at_load_time(tmp_path):
    """The loader path (`footy build --league jpn1`) writes the parquet with
    the exclusion already applied -- the raw CSV itself is never edited."""
    (tmp_path / "JPN_new.csv").write_text(
        "\n".join([_HEADER, _CLEAN_ROW, _SENDAI_ROW]) + "\n", encoding="utf-8"
    )
    frame = build_j1_matches(tmp_path)

    sendai = frame[frame["home_team"] == "Vegalta Sendai"]
    assert len(sendai) == 1
    assert sendai[["psch", "pscd", "psca"]].isna().all().all()
    clean = frame[frame["home_team"] == "Kashima Antlers"].iloc[0]
    assert clean["psch"] == 1.9
