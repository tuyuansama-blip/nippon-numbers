"""Odds snapshot -> quotes -> pseudo-close pipeline (DESIGN_PHASE2.md 8.2, 8.3).

All synthetic JSON, shaped like a real Odds API response but hand-built, so
the three landmines DESIGN_PHASE2.md measured on a live snapshot (0.18-0.20)
each get a dedicated regression test: outcomes out of order, an unresolved
team spelling, and staleness past the 6h gate.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from footy.data.teams_j1 import odds_api_team_id, resolve_odds_api_team
from footy.odds.close import build_close
from footy.odds.ingest import ingest, parse_snapshot


def _event(event_id, home, away, commence, bookmakers):
    return {
        "id": event_id, "sport_key": "soccer_japan_j_league",
        "commence_time": commence, "home_team": home, "away_team": away,
        "bookmakers": bookmakers,
    }


def _book(key, last_update, home, away, h_price, d_price, a_price, market_key="h2h"):
    return {
        "key": key, "last_update": last_update,
        "markets": [{
            "key": market_key, "last_update": last_update,
            "outcomes": [
                {"name": home, "price": h_price},
                {"name": "Draw", "price": d_price},
                {"name": away, "price": a_price},
            ],
        }],
    }


def _write_snapshot(tmp_path, ts, events):
    path = tmp_path / f"j1_h2h_eu_{ts}.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    return path


# --- team crosswalk ----------------------------------------------------------
def test_resolve_odds_api_team_uses_the_alias_table():
    assert resolve_odds_api_team("Hiroshima Sanfrecce FC") == "Sanfrecce Hiroshima"
    assert resolve_odds_api_team("Kyoto Purple Sanga") == "Kyoto"


def test_resolve_odds_api_team_falls_back_to_exact_j1_spelling():
    assert resolve_odds_api_team("Kashiwa Reysol") == "Kashiwa Reysol"


def test_resolve_odds_api_team_never_fuzzy_matches_the_marinos_pair():
    a = odds_api_team_id("Yokohama F Marinos")
    b = odds_api_team_id("Yokohama FC")
    assert a is not None and a != b


def test_resolve_odds_api_team_unknown_spelling_is_none():
    assert resolve_odds_api_team("Not A Real Club") is None


# --- ingest --------------------------------------------------------------
def test_parse_snapshot_matches_outcomes_by_name_not_index(tmp_path):
    """DESIGN_PHASE2.md 0.19: away can be listed first in `outcomes[]`."""
    events = [_event(
        "e1", "Kashima Antlers", "Urawa Reds", "2026-08-21T10:00:00Z",
        [{
            "key": "pinnacle", "last_update": "2026-08-20T07:00:00Z",
            "markets": [{
                "key": "h2h", "last_update": "2026-08-20T07:00:00Z",
                "outcomes": [
                    {"name": "Urawa Reds", "price": 4.5},   # away listed first
                    {"name": "Kashima Antlers", "price": 1.7},
                    {"name": "Draw", "price": 3.8},
                ],
            }],
        }],
    )]
    path = _write_snapshot(tmp_path, "20260820T070000Z", events)
    rows = parse_snapshot(path)
    by_outcome = {r["outcome"]: r["price"] for r in rows}
    assert by_outcome["H"] == 1.7
    assert by_outcome["A"] == 4.5
    assert by_outcome["D"] == 3.8


def test_parse_snapshot_drops_a_match_with_an_unmatched_outcome_name(tmp_path):
    warnings = []
    events = [_event(
        "e1", "Kashima Antlers", "Urawa Reds", "2026-08-21T10:00:00Z",
        [{
            "key": "pinnacle", "last_update": "2026-08-20T07:00:00Z",
            "markets": [{
                "key": "h2h", "last_update": "2026-08-20T07:00:00Z",
                "outcomes": [
                    {"name": "Kashima Antlers", "price": 1.7},
                    {"name": "Urawa Reds", "price": 4.5},
                    {"name": "Something Else", "price": 3.8},   # not H/A/Draw
                ],
            }],
        }],
    )]
    path = _write_snapshot(tmp_path, "20260820T070000Z", events)
    rows = parse_snapshot(path, warn=warnings.append)
    assert rows == []
    assert warnings and "dropping the whole match" in warnings[0]


def test_parse_snapshot_ignores_h2h_lay_markets(tmp_path):
    events = [_event(
        "e1", "Kashima Antlers", "Urawa Reds", "2026-08-21T10:00:00Z",
        [_book("pinnacle", "2026-08-20T07:00:00Z", "Kashima Antlers", "Urawa Reds",
               1.7, 3.8, 4.5)]
        + [{
            "key": "betfair_ex_eu", "last_update": "2026-08-20T07:00:00Z",
            "markets": [
                {"key": "h2h", "last_update": "2026-08-20T07:00:00Z", "outcomes": [
                    {"name": "Kashima Antlers", "price": 1.75},
                    {"name": "Draw", "price": 3.9},
                    {"name": "Urawa Reds", "price": 4.6},
                ]},
                {"key": "h2h_lay", "last_update": "2026-08-20T07:00:00Z", "outcomes": [
                    {"name": "Kashima Antlers", "price": 1.8},
                    {"name": "Draw", "price": 4.0},
                    {"name": "Urawa Reds", "price": 4.8},
                ]},
            ],
        }],
    )]
    path = _write_snapshot(tmp_path, "20260820T070000Z", events)
    rows = parse_snapshot(path)
    keys = {r["book_key"] for r in rows}
    assert keys == {"pinnacle", "betfair_ex_eu"}
    assert len(rows) == 6         # 2 books x 3 outcomes, h2h only


def test_parse_snapshot_raises_on_an_unresolved_team_spelling(tmp_path):
    events = [_event(
        "e1", "Not A Real Club", "Urawa Reds", "2026-08-21T10:00:00Z",
        [_book("pinnacle", "2026-08-20T07:00:00Z", "Not A Real Club", "Urawa Reds",
               1.7, 3.8, 4.5)],
    )]
    path = _write_snapshot(tmp_path, "20260820T070000Z", events)
    with pytest.raises(ValueError, match="unresolved J1 team spelling"):
        parse_snapshot(path)


def test_ingest_is_idempotent_across_repeated_calls(tmp_path):
    events = [_event(
        "e1", "Kashima Antlers", "Urawa Reds", "2026-08-21T10:00:00Z",
        [_book("pinnacle", "2026-08-20T07:00:00Z", "Kashima Antlers", "Urawa Reds",
               1.7, 3.8, 4.5)],
    )]
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    _write_snapshot(snap_dir, "20260820T070000Z", events)
    out = tmp_path / "quotes.parquet"

    first = ingest(snap_dir, out_path=out, warn=lambda m: None)
    second = ingest(snap_dir, out_path=out, warn=lambda m: None)
    assert len(first) == len(second) == 3


def test_ingest_merges_a_second_snapshot_without_duplicating_the_first(tmp_path):
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    e1 = [_event(
        "e1", "Kashima Antlers", "Urawa Reds", "2026-08-21T10:00:00Z",
        [_book("pinnacle", "2026-08-19T07:00:00Z", "Kashima Antlers", "Urawa Reds",
               1.7, 3.8, 4.5)],
    )]
    e2 = [_event(
        "e1", "Kashima Antlers", "Urawa Reds", "2026-08-21T10:00:00Z",
        [_book("pinnacle", "2026-08-20T07:00:00Z", "Kashima Antlers", "Urawa Reds",
               1.75, 3.7, 4.4)],
    )]
    _write_snapshot(snap_dir, "20260819T070000Z", e1)
    _write_snapshot(snap_dir, "20260820T070000Z", e2)
    out = tmp_path / "quotes.parquet"
    combined = ingest(snap_dir, out_path=out, warn=lambda m: None)
    # Two distinct book_last_update values for the same (event, book) survive
    # -- ingest keeps history, `close.py` decides which one is "the" close.
    assert combined["book_last_update"].nunique() == 2
    assert len(combined) == 6


# --- pseudo-close --------------------------------------------------------
def _quotes_frame(rows):
    frame = pd.DataFrame(rows)
    frame["commence_time"] = pd.to_datetime(frame["commence_time"], utc=True)
    frame["book_last_update"] = pd.to_datetime(frame["book_last_update"], utc=True)
    return frame


def test_build_close_prefers_pinnacle_when_present():
    commence = "2026-08-21T10:00:00Z"
    last_update = "2026-08-21T08:00:00Z"       # 2h before kickoff -> 'ok'
    rows = []
    for outcome, price in (("H", 1.7), ("D", 3.8), ("A", 4.5)):
        rows.append({
            "event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
            "outcome": outcome, "price": price, "book_last_update": last_update,
            "home_id": "jpn_1:kashima_antlers", "away_id": "jpn_1:urawa_reds",
        })
    for outcome, price in (("H", 1.9), ("D", 4.0), ("A", 5.0)):
        rows.append({
            "event_id": "e1", "commence_time": commence, "book_key": "betsson",
            "outcome": outcome, "price": price, "book_last_update": last_update,
            "home_id": "jpn_1:kashima_antlers", "away_id": "jpn_1:urawa_reds",
        })
    close = build_close(_quotes_frame(rows))
    assert len(close) == 1
    assert close.iloc[0]["benchmark_source"] == "pinnacle"
    assert close.iloc[0]["close_quality"] == "ok"
    assert close[["p_h", "p_d", "p_a"]].sum(axis=1).iloc[0] == pytest.approx(1.0)


def test_build_close_falls_back_to_the_median_when_pinnacle_is_absent():
    commence = "2026-08-21T10:00:00Z"
    last_update = "2026-08-21T08:00:00Z"
    books = ["coolbet", "nordicbet", "betsson"]
    rows = []
    for book in books:
        for outcome, price in (("H", 1.8), ("D", 3.9), ("A", 4.6)):
            rows.append({
                "event_id": "e1", "commence_time": commence, "book_key": book,
                "outcome": outcome, "price": price, "book_last_update": last_update,
                "home_id": "jpn_1:kashima_antlers", "away_id": "jpn_1:urawa_reds",
            })
    close = build_close(_quotes_frame(rows))
    assert close.iloc[0]["benchmark_source"] == "median16"
    assert close[["p_h", "p_d", "p_a"]].sum(axis=1).iloc[0] == pytest.approx(1.0)


def test_build_close_flags_stale_quotes_as_poor_quality():
    commence = "2026-08-21T10:00:00Z"
    last_update = "2026-08-19T07:00:00Z"     # ~51h before kickoff
    rows = [
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": o, "price": p, "book_last_update": last_update,
         "home_id": "jpn_1:kashima_antlers", "away_id": "jpn_1:urawa_reds"}
        for o, p in (("H", 1.7), ("D", 3.8), ("A", 4.5))
    ]
    close = build_close(_quotes_frame(rows))
    assert close.iloc[0]["close_quality"] == "poor"
    assert close.iloc[0]["staleness_sec"] > 6 * 3600


def test_build_close_uses_the_most_recent_pre_kickoff_quote_per_book():
    commence = "2026-08-21T10:00:00Z"
    rows = [
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": "H", "price": 2.0, "book_last_update": "2026-08-19T07:00:00Z",
         "home_id": "h", "away_id": "a"},
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": "D", "price": 3.8, "book_last_update": "2026-08-19T07:00:00Z",
         "home_id": "h", "away_id": "a"},
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": "A", "price": 4.5, "book_last_update": "2026-08-19T07:00:00Z",
         "home_id": "h", "away_id": "a"},
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": "H", "price": 1.7, "book_last_update": "2026-08-21T08:00:00Z",
         "home_id": "h", "away_id": "a"},
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": "D", "price": 3.9, "book_last_update": "2026-08-21T08:00:00Z",
         "home_id": "h", "away_id": "a"},
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": "A", "price": 4.6, "book_last_update": "2026-08-21T08:00:00Z",
         "home_id": "h", "away_id": "a"},
    ]
    close = build_close(_quotes_frame(rows))
    assert len(close) == 1
    assert close.iloc[0]["close_quality"] == "ok"
    # p_h should reflect the *later* (closer-to-kickoff) price of 1.7, not 2.0.
    assert close.iloc[0]["p_h"] > close.iloc[0]["p_a"] / 2   # sanity: still a real favourite
    implied_from_1_7 = 1 / 1.7
    implied_from_2_0 = 1 / 2.0
    assert abs(close.iloc[0]["p_h"] - implied_from_1_7) < abs(
        close.iloc[0]["p_h"] - implied_from_2_0
    )


def test_build_close_ignores_quotes_updated_after_kickoff():
    """A quote timestamped after kickoff cannot be a pre-match close."""
    commence = "2026-08-21T10:00:00Z"
    rows = [
        {"event_id": "e1", "commence_time": commence, "book_key": "pinnacle",
         "outcome": o, "price": p, "book_last_update": "2026-08-21T11:00:00Z",
         "home_id": "h", "away_id": "a"}
        for o, p in (("H", 1.7), ("D", 3.8), ("A", 4.5))
    ]
    close = build_close(_quotes_frame(rows))
    assert close.empty
