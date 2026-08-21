"""Result reconciliation and the track record (DESIGN_PHASE2.md 9's Monday
`reconcile` step). All synthetic: a hand-built prediction payload plus a
matches-history frame that resolves some, but not all, of its fixtures.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from footy.eval.metrics import rps
from footy.pipeline.reconcile import build_track_record, reconcile, reconcile_file


def _payload(matches):
    return {
        "schema_version": 1, "league": "jpn1", "season": 2024, "season_label": "2024",
        "round_id": "2024-05-03", "generated_at": "2024-05-01T03:00:00+00:00",
        "asof": "2024-05-01T03:00:00+00:00", "model_version": "test-v1",
        "params_hash": "deadbeef", "matches": matches,
    }


def _match(home_id, away_id, home_team, away_team, commence_time, p_raw, p_cal):
    return {
        "event_id": f"{home_id}-{away_id}", "commence_time": commence_time,
        "home_team": home_team, "away_team": away_team,
        "home_id": home_id, "away_id": away_id,
        "p_raw": {"h": p_raw[0], "d": p_raw[1], "a": p_raw[2]},
        "p_calibrated": {"h": p_cal[0], "d": p_cal[1], "a": p_cal[2]},
        "result": None,
    }


def _matches_history(rows):
    frame = pd.DataFrame(rows)
    return frame


def test_reconcile_file_fills_in_a_resolved_match(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               p_raw=(0.5, 0.3, 0.2), p_cal=(0.55, 0.25, 0.20)),
    ])
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actual = _matches_history([
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h1", "away_id": "a1",
         "fthg": 2.0, "ftag": 0.0, "ftr": "H"},
    ])
    updated, changed = reconcile_file(path, actual)
    assert changed is True
    result = updated["matches"][0]["result"]
    assert result["ftr"] == "H"
    assert result["y"] == 0
    assert result["rps_raw"] == pytest.approx(rps([0.5, 0.3, 0.2], 0))
    assert result["rps_cal"] == pytest.approx(rps([0.55, 0.25, 0.20], 0))
    assert updated["round_summary"]["complete"] is True
    assert updated["round_summary"]["n_resolved"] == 1


def test_reconcile_file_leaves_unplayed_matches_null(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.5, 0.3, 0.2)),
        _match("h2", "a2", "Other FC", "Third FC", "2024-05-03T10:00:00+00:00",
               (0.4, 0.3, 0.3), (0.4, 0.3, 0.3)),
    ])
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actual = _matches_history([
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h1", "away_id": "a1",
         "fthg": 1.0, "ftag": 1.0, "ftr": "D"},
    ])
    updated, changed = reconcile_file(path, actual)
    assert changed is True
    assert updated["matches"][0]["result"] is not None
    assert updated["matches"][1]["result"] is None
    assert updated["round_summary"]["complete"] is False
    assert updated["round_summary"]["n_resolved"] == 1


def test_reconcile_file_never_touches_an_already_resolved_match(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.5, 0.3, 0.2)),
    ])
    payload["matches"][0]["result"] = {
        "fthg": 3.0, "ftag": 0.0, "ftr": "H", "y": 0,
        "rps_raw": 0.09, "rps_cal": 0.09, "ll_raw": 0.5, "ll_cal": 0.5,
    }
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # An "actual" result that disagrees, to prove it is never consulted for
    # an already-resolved match.
    actual = _matches_history([
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h1", "away_id": "a1",
         "fthg": 0.0, "ftag": 5.0, "ftr": "A"},
    ])
    updated, changed = reconcile_file(path, actual)
    assert changed is False
    assert updated["matches"][0]["result"]["ftr"] == "H"


def test_reconcile_file_matches_within_the_date_window_only(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.5, 0.3, 0.2)),
    ])
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # Ten days off -- outside RESULT_MATCH_WINDOW_DAYS -- must not match.
    actual = _matches_history([
        {"date": pd.Timestamp("2024-05-13"), "home_id": "h1", "away_id": "a1",
         "fthg": 2.0, "ftag": 0.0, "ftr": "H"},
    ])
    updated, changed = reconcile_file(path, actual)
    assert changed is False
    assert updated["matches"][0]["result"] is None


def test_reconcile_end_to_end_writes_track_record(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.55, 0.25, 0.20)),
        _match("h2", "a2", "Other FC", "Third FC", "2024-05-03T10:00:00+00:00",
               (0.4, 0.3, 0.3), (0.4, 0.3, 0.3)),
    ])
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    matches_history = _matches_history([
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h1", "away_id": "a1",
         "fthg": 2.0, "ftag": 0.0, "ftr": "H"},
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h2", "away_id": "a2",
         "fthg": 1.0, "ftag": 1.0, "ftr": "D"},
    ])
    result = reconcile(matches_history, predictions_dir=tmp_path)
    assert result["updated_files"] == [path.name]
    assert result["track_record_rows"] == 2

    track = pd.read_csv(result["track_record_path"])
    assert set(track["ftr"]) == {"H", "D"}
    assert (track["rps_cal"] >= 0).all()

    # idempotent: a second run with no new information changes nothing
    again = reconcile(matches_history, predictions_dir=tmp_path)
    assert again["updated_files"] == []
    assert again["track_record_rows"] == 2


def test_build_track_record_skips_unresolved_matches(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.5, 0.3, 0.2)),
    ])
    (tmp_path / "j1_2024_2024-05-03.json").write_text(json.dumps(payload), encoding="utf-8")
    track = build_track_record(tmp_path)
    assert track.empty


def test_reconcile_on_a_missing_predictions_dir_is_a_noop(tmp_path):
    result = reconcile(pd.DataFrame(columns=["date", "home_id", "away_id", "fthg", "ftag", "ftr"]),
                        predictions_dir=tmp_path / "does_not_exist")
    assert result == {"updated_files": [], "track_record_rows": 0, "track_record_path": None}


# --- two-stage results (odds-api provisional -> football-data confirmed) -------
def test_build_track_record_carries_the_source_of_each_result(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.5, 0.3, 0.2)),
    ])
    payload["matches"][0]["result"] = {
        "fthg": 2.0, "ftag": 0.0, "ftr": "H", "y": 0,
        "rps_raw": 0.09, "rps_cal": 0.09, "ll_raw": 0.5, "ll_cal": 0.5,
        "source": "odds_api_provisional", "fetched_at": "2024-05-03T12:00:00+00:00",
    }
    (tmp_path / "j1_2024_2024-05-03.json").write_text(json.dumps(payload), encoding="utf-8")

    track = build_track_record(tmp_path)
    assert list(track["source"]) == ["odds_api_provisional"]


def test_build_track_record_defaults_source_to_football_data_for_legacy_results(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.5, 0.3, 0.2)),
    ])
    payload["matches"][0]["result"] = {
        "fthg": 2.0, "ftag": 0.0, "ftr": "H", "y": 0,
        "rps_raw": 0.09, "rps_cal": 0.09, "ll_raw": 0.5, "ll_cal": 0.5,
    }
    (tmp_path / "j1_2024_2024-05-03.json").write_text(json.dumps(payload), encoding="utf-8")

    track = build_track_record(tmp_path)
    assert list(track["source"]) == ["football_data"]


def test_reconcile_end_to_end_upgrades_a_provisional_result_and_reports_no_discrepancy(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.55, 0.25, 0.20)),
    ])
    payload["matches"][0]["result"] = {
        "fthg": 2.0, "ftag": 0.0, "ftr": "H", "y": 0,
        "rps_raw": 0.09, "rps_cal": 0.09, "ll_raw": 0.5, "ll_cal": 0.5,
        "source": "odds_api_provisional", "fetched_at": "2024-05-03T12:00:00+00:00",
    }
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    matches_history = _matches_history([
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h1", "away_id": "a1",
         "fthg": 2.0, "ftag": 0.0, "ftr": "H"},
    ])
    result = reconcile(matches_history, predictions_dir=tmp_path)
    assert result["updated_files"] == [path.name]
    assert result["discrepancies"] == []

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["matches"][0]["result"]["source"] == "football_data"


def test_reconcile_surfaces_a_discrepancy_between_provisional_and_confirmed_scores(tmp_path):
    payload = _payload([
        _match("h1", "a1", "Home FC", "Away FC", "2024-05-03T10:00:00+00:00",
               (0.5, 0.3, 0.2), (0.55, 0.25, 0.20)),
    ])
    payload["matches"][0]["result"] = {
        "fthg": 2.0, "ftag": 0.0, "ftr": "H", "y": 0,
        "rps_raw": 0.09, "rps_cal": 0.09, "ll_raw": 0.5, "ll_cal": 0.5,
        "source": "odds_api_provisional", "fetched_at": "2024-05-03T12:00:00+00:00",
    }
    path = tmp_path / "j1_2024_2024-05-03.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # football-data disagrees with the earlier provisional guess.
    matches_history = _matches_history([
        {"date": pd.Timestamp("2024-05-03"), "home_id": "h1", "away_id": "a1",
         "fthg": 1.0, "ftag": 0.0, "ftr": "H"},
    ])
    result = reconcile(matches_history, predictions_dir=tmp_path)
    assert len(result["discrepancies"]) == 1
    discrepancy = result["discrepancies"][0]
    assert discrepancy["odds_api_provisional"]["fthg"] == 2.0
    assert discrepancy["football_data"]["fthg"] == 1.0

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["matches"][0]["result"]["fthg"] == 1.0    # confirmed wins
    assert written["matches"][0]["result"]["discrepancy"] is not None
