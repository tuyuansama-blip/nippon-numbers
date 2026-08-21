"""The Odds API `/scores` provisional-results path (`footy/odds/scores.py`),
the same-night counterpart to `reconcile.py`'s football-data-confirmed one.
All synthetic: hand-built `/scores` payloads, never a real API response
(DESIGN.md 4 -- `fetch_scores` itself, the one networked function here, is
untested, same as `odds_schedule.run_schedule`).
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from footy.odds.scores import (
    apply_provisional_results,
    apply_provisional_to_dir,
    parse_score_event,
)
from footy.pipeline.reconcile import reconcile_file


def _prediction_payload(matches):
    return {
        "schema_version": 1, "league": "jpn1", "season": 2026, "season_label": "2026-27",
        "round_id": "2026-08-21", "generated_at": "2026-08-20T10:00:00+00:00",
        "asof": "2026-08-20T10:00:00+00:00", "model_version": "test-v1",
        "params_hash": "deadbeef", "matches": matches,
    }


def _match(event_id, home_team, away_team, commence_time, result=None):
    return {
        "event_id": event_id, "commence_time": commence_time,
        "home_team": home_team, "away_team": away_team,
        "home_id": f"jpn_1:{home_team.lower().replace(' ', '_').replace('.', '')}",
        "away_id": f"jpn_1:{away_team.lower().replace(' ', '_').replace('.', '')}",
        "p_raw": {"h": 0.5, "d": 0.25, "a": 0.25},
        "p_calibrated": {"h": 0.5, "d": 0.25, "a": 0.25},
        "result": result,
    }


def _score_event(event_id, home_team, away_team, commence_time, home_score, away_score,
                  *, completed=True):
    return {
        "id": event_id, "sport_key": "soccer_japan_j_league", "commence_time": commence_time,
        "completed": completed, "home_team": home_team, "away_team": away_team,
        "scores": None if not completed else [
            {"name": home_team, "score": str(home_score)},
            {"name": away_team, "score": str(away_score)},
        ],
        "last_update": commence_time,
    }


# --- parse_score_event ---------------------------------------------------------
def test_parse_score_event_none_when_not_completed():
    event = _score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                          "2026-08-21T10:00:00Z", 4, 2, completed=False)
    assert parse_score_event(event) is None


def test_parse_score_event_none_when_scores_missing():
    event = _score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                          "2026-08-21T10:00:00Z", 4, 2)
    event["scores"] = None
    assert parse_score_event(event) is None


def test_parse_score_event_none_on_non_numeric_score():
    event = _score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                          "2026-08-21T10:00:00Z", 4, 2)
    event["scores"][0]["score"] = "abandoned"
    assert parse_score_event(event) is None


def test_parse_score_event_extracts_scores():
    event = _score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                          "2026-08-21T10:00:00Z", 4, 2)
    parsed = parse_score_event(event)
    assert parsed == {
        "event_id": "evt1", "commence_time": "2026-08-21T10:00:00Z",
        "home_team": "Kashiwa Reysol", "away_team": "V-Varen Nagasaki",
        "home_score": 4, "away_score": 2,
    }


# --- apply_provisional_results: event-id matching -------------------------------
def test_apply_provisional_results_fills_a_null_result_by_event_id():
    payload = _prediction_payload([
        _match("evt1", "Kashiwa Reysol", "V-Varen Nagasaki", "2026-08-21T10:00:00+00:00"),
    ])
    events = [_score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                            "2026-08-21T10:00:00Z", 4, 2)]
    updated, changed, warnings = apply_provisional_results(
        payload, events, now=pd.Timestamp("2026-08-21T12:00:00Z"),
    )
    assert changed is True
    assert warnings == []
    result = updated["matches"][0]["result"]
    assert result["fthg"] == 4.0 and result["ftag"] == 2.0 and result["ftr"] == "H"
    assert result["source"] == "odds_api_provisional"
    assert result["fetched_at"] == "2026-08-21T12:00:00+00:00"
    assert updated["round_summary"]["n_resolved"] == 1


def test_apply_provisional_results_ignores_a_not_yet_completed_event():
    payload = _prediction_payload([
        _match("evt1", "Kashiwa Reysol", "V-Varen Nagasaki", "2026-08-21T10:00:00+00:00"),
    ])
    events = [_score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                            "2026-08-21T10:00:00Z", 1, 0, completed=False)]
    updated, changed, warnings = apply_provisional_results(payload, events)
    assert changed is False
    assert updated["matches"][0]["result"] is None


def test_apply_provisional_results_never_touches_an_already_confirmed_match():
    """A `result` with no `source` key predates the two-stage split and is
    treated as confirmed -- same discipline as `reconcile._is_confirmed`."""
    payload = _prediction_payload([
        _match("evt1", "Kashiwa Reysol", "V-Varen Nagasaki", "2026-08-21T10:00:00+00:00",
               result={"fthg": 3.0, "ftag": 0.0, "ftr": "H", "y": 0,
                       "rps_raw": 0.1, "rps_cal": 0.1, "ll_raw": 0.5, "ll_cal": 0.5}),
    ])
    events = [_score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                            "2026-08-21T10:00:00Z", 4, 2)]
    updated, changed, warnings = apply_provisional_results(payload, events)
    assert changed is False
    assert updated["matches"][0]["result"]["fthg"] == 3.0


def test_apply_provisional_results_re_scores_an_existing_provisional_result():
    """Provisional results are re-fillable: a half-time snapshot must be
    replaceable by a later, full-time one within the same night."""
    payload = _prediction_payload([
        _match("evt1", "Kashiwa Reysol", "V-Varen Nagasaki", "2026-08-21T10:00:00+00:00",
               result={"fthg": 1.0, "ftag": 0.0, "ftr": "H", "y": 0,
                       "rps_raw": 0.1, "rps_cal": 0.1, "ll_raw": 0.5, "ll_cal": 0.5,
                       "source": "odds_api_provisional", "fetched_at": "2026-08-21T09:00:00+00:00"}),
    ])
    events = [_score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                            "2026-08-21T10:00:00Z", 4, 2)]
    updated, changed, warnings = apply_provisional_results(
        payload, events, now=pd.Timestamp("2026-08-21T12:00:00Z"),
    )
    assert changed is True
    result = updated["matches"][0]["result"]
    assert result["fthg"] == 4.0 and result["ftag"] == 2.0
    assert result["fetched_at"] == "2026-08-21T12:00:00+00:00"


# --- apply_provisional_results: team-name fallback -------------------------------
def test_apply_provisional_results_falls_back_to_team_name_when_event_id_differs():
    payload = _prediction_payload([
        _match("predict-side-id", "FC Tokyo", "Chiba", "2026-08-21T10:30:00+00:00"),
    ])
    # The Odds API's /scores id doesn't match the /odds snapshot id the
    # prediction file carries -- the fallback resolves "JEF United
    # Ichihara-Chiba" (a scores-only spelling) through ODDS_API_ALIASES and
    # matches on canonical name + date window instead.
    events = [_score_event("scores-side-id", "FC Tokyo", "JEF United Ichihara-Chiba",
                            "2026-08-21T10:30:00Z", 2, 0)]
    updated, changed, warnings = apply_provisional_results(payload, events)
    assert changed is True
    assert warnings == []
    result = updated["matches"][0]["result"]
    assert result["fthg"] == 2.0 and result["ftag"] == 0.0 and result["ftr"] == "H"


def test_apply_provisional_results_warns_and_skips_an_unresolved_team_name():
    payload = _prediction_payload([
        _match("evt1", "FC Tokyo", "Chiba", "2026-08-21T10:30:00+00:00"),
    ])
    events = [_score_event("not-the-matching-id", "FC Tokyo", "Some Brand New Spelling FC",
                            "2026-08-21T10:30:00Z", 2, 0)]
    updated, changed, warnings = apply_provisional_results(payload, events)
    assert changed is False
    assert updated["matches"][0]["result"] is None
    assert any("Some Brand New Spelling FC" in w for w in warnings)


# --- apply_provisional_to_dir ---------------------------------------------------
def test_apply_provisional_to_dir_writes_only_changed_files(tmp_path):
    payload_a = _prediction_payload([
        _match("evt1", "Kashiwa Reysol", "V-Varen Nagasaki", "2026-08-21T10:00:00+00:00"),
    ])
    payload_b = _prediction_payload([
        _match("evt2", "FC Tokyo", "Chiba", "2026-08-21T10:30:00+00:00"),
    ])
    payload_b["round_id"] = "2026-08-28"
    (tmp_path / "j1_2026_2026-08-21.json").write_text(json.dumps(payload_a), encoding="utf-8")
    (tmp_path / "j1_2026_2026-08-28.json").write_text(json.dumps(payload_b), encoding="utf-8")

    events = [_score_event("evt1", "Kashiwa Reysol", "V-Varen Nagasaki",
                            "2026-08-21T10:00:00Z", 4, 2)]
    result = apply_provisional_to_dir(events, predictions_dir=tmp_path)
    assert result["updated_files"] == ["j1_2026_2026-08-21.json"]

    written = json.loads((tmp_path / "j1_2026_2026-08-21.json").read_text(encoding="utf-8"))
    assert written["matches"][0]["result"]["source"] == "odds_api_provisional"
    untouched = json.loads((tmp_path / "j1_2026_2026-08-28.json").read_text(encoding="utf-8"))
    assert untouched["matches"][0]["result"] is None


def test_apply_provisional_to_dir_on_a_missing_directory_is_a_noop(tmp_path):
    result = apply_provisional_to_dir([], predictions_dir=tmp_path / "does_not_exist")
    assert result == {"updated_files": [], "warnings": []}


# --- interaction with the confirming (football-data) side ----------------------
def test_reconcile_file_confirms_a_matching_provisional_result_without_discrepancy(tmp_path):
    payload = _prediction_payload([
        _match("evt1", "Home FC", "Away FC", "2026-08-21T10:00:00+00:00",
               result={"fthg": 4.0, "ftag": 2.0, "ftr": "H", "y": 0,
                       "rps_raw": 0.1, "rps_cal": 0.1, "ll_raw": 0.5, "ll_cal": 0.5,
                       "source": "odds_api_provisional", "fetched_at": "2026-08-21T12:00:00+00:00"}),
    ])
    payload["matches"][0]["home_id"] = "h1"
    payload["matches"][0]["away_id"] = "a1"
    path = tmp_path / "j1_2026_2026-08-21.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actual = pd.DataFrame([
        {"date": pd.Timestamp("2026-08-21"), "home_id": "h1", "away_id": "a1",
         "fthg": 4.0, "ftag": 2.0, "ftr": "H"},
    ])
    updated, changed = reconcile_file(path, actual)
    assert changed is True
    result = updated["matches"][0]["result"]
    assert result["source"] == "football_data"
    assert "discrepancy" not in result


def test_reconcile_file_records_a_discrepancy_instead_of_raising(tmp_path):
    payload = _prediction_payload([
        _match("evt1", "Home FC", "Away FC", "2026-08-21T10:00:00+00:00",
               result={"fthg": 4.0, "ftag": 2.0, "ftr": "H", "y": 0,
                       "rps_raw": 0.1, "rps_cal": 0.1, "ll_raw": 0.5, "ll_cal": 0.5,
                       "source": "odds_api_provisional", "fetched_at": "2026-08-21T12:00:00+00:00"}),
    ])
    payload["matches"][0]["home_id"] = "h1"
    payload["matches"][0]["away_id"] = "a1"
    path = tmp_path / "j1_2026_2026-08-21.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # football-data disagrees with the earlier odds-api provisional guess.
    actual = pd.DataFrame([
        {"date": pd.Timestamp("2026-08-21"), "home_id": "h1", "away_id": "a1",
         "fthg": 3.0, "ftag": 2.0, "ftr": "H"},
    ])
    updated, changed = reconcile_file(path, actual)
    assert changed is True
    result = updated["matches"][0]["result"]
    assert result["source"] == "football_data"
    assert result["fthg"] == 3.0    # the confirmed score wins
    assert result["discrepancy"] == {
        "odds_api_provisional": {"fthg": 4.0, "ftag": 2.0, "ftr": "H"},
        "football_data": {"fthg": 3.0, "ftag": 2.0, "ftr": "H"},
    }
