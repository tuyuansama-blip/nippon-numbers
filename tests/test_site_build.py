"""`footy site build` (DESIGN_SITE.md 2.1): predictions/*.json -> static
HTML. All synthetic, self-contained payloads -- never the project's real
`predictions/` directory -- built directly in the schema
`footy.pipeline.predict.write_prediction` actually writes, not the fuller
schema DESIGN_SITE.md 2.2 sketches (this project's `predict` step
deliberately never puts a market probability on a fixture -- see
`footy/pipeline/reconcile.py`'s module docstring / DESIGN_SITE.md 2.6 -- so
there is nothing to accidentally leak onto the page in the first place).
"""

from __future__ import annotations

import json
import re

import pandas as pd
import pytest

from footy.site.build import (
    build_site,
    file_sha256,
    latest_prediction,
    list_predictions,
    render_index,
    render_methodology,
    render_record,
    round_label,
)

# Design-doc banned tipster/gambling vocabulary (DESIGN_SITE.md 4.3), minus
# "bet"/"betting" -- the design doc's own top-page mockup (1.3) uses "the
# betting market" as a factual noun phrase ("The betting market is still
# better than us"), so that pair is legitimate here and excluded from the
# blanket check below.
BANNED_WORDS = [
    "tip", "pick", "stake", "value bet", "edge", "roi", "bankroll", "lock",
    "banker", "sure thing", "must-win", "deserved", "unlucky", "tactical",
    "formation", "will win",
]


def _match(event_id, home, away, kickoff, p_h, p_d, p_a, result=None):
    return {
        "event_id": event_id,
        "commence_time": kickoff,
        "home_team": home,
        "away_team": away,
        "home_id": f"jpn_1:{home.lower().replace(' ', '_')}",
        "away_id": f"jpn_1:{away.lower().replace(' ', '_')}",
        "p_raw": {"h": p_h, "d": p_d, "a": p_a},
        "p_calibrated": {"h": p_h, "d": p_d, "a": p_a},
        "result": result,
    }


def _payload(round_id="2026-08-21", generated_at="2026-08-20T10:29:36+00:00", n=2):
    matches = [
        _match(
            f"evt{i}", f"Home {i}", f"Away {i}",
            f"2026-08-21T{10 + i:02d}:00:00+00:00", 0.5, 0.25, 0.25,
        )
        for i in range(n)
    ]
    return {
        "schema_version": 1,
        "league": "jpn1",
        "season": 2026,
        "season_label": "2026-27",
        "round_id": round_id,
        "generated_at": generated_at,
        "asof": generated_at,
        "model_version": "footy-ev-phase2-dc-tb-v1",
        "params_hash": "deadbeef" * 8,
        "params_hash_check": {"ok": True, "current": "x", "tag": None, "expected": None, "note": "n/a"},
        "frozen_params_source": "data/frozen_params.json (test)",
        "phi": {
            "temperature": 1.0, "phi_home": 0.0, "phi_draw": 0.0,
            "n_history": 100, "warm": True, "as_of_fold": "2026-08-10 00:00:00",
        },
        "training_window": {"n_matches": 1000, "n_teams": 20, "from_season": 2021},
        "odds_snapshot": {
            "files": ["snap.json"],
            "sha256": {"snap.json": "abc123"},
            "combined_sha256": "combinedabc123",
        },
        "check": {"ok": True, "problems": []},
        "publish_gate": {"ok": True, "reasons": []},
        "matches": matches,
    }


def _write(tmp_path, payload):
    stem = f"j1_{payload['season']}_{payload['round_id']}"
    path = tmp_path / f"{stem}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --- discovering prediction files --------------------------------------------
def test_list_predictions_finds_valid_round_files(tmp_path):
    _write(tmp_path, _payload(round_id="2026-08-21", generated_at="2026-08-20T10:00:00+00:00"))
    _write(tmp_path, _payload(round_id="2026-08-28", generated_at="2026-08-27T10:00:00+00:00"))
    found = list_predictions(tmp_path)
    assert [p["round_id"] for p, _ in found] == ["2026-08-28", "2026-08-21"]


def test_list_predictions_skips_non_round_json(tmp_path):
    """An `.ots.json` stamp stub (`stamp.py`'s `<round>.ots.json`) still
    matches the `j1_*.json` glob but has no `matches` key -- it must be
    skipped, not crash the build."""
    _write(tmp_path, _payload())
    stub = tmp_path / "j1_2026_2026-08-21.ots.json"
    stub.write_text(json.dumps({"file": "x", "sha256": "y", "stamped": False}), encoding="utf-8")
    found = list_predictions(tmp_path)
    assert len(found) == 1


def test_latest_prediction_none_when_empty(tmp_path):
    assert latest_prediction(tmp_path) is None


def test_round_label():
    payload = _payload(round_id="2026-08-21")
    assert round_label(payload) == "J1 2026-27 -- round of 2026-08-21"


# --- index.html --------------------------------------------------------------
def test_render_index_shows_metadata_and_fixtures():
    payload = _payload()
    html = render_index(payload, "somehash123", source_filename="j1_2026_2026-08-21.json")
    assert "Home 0" in html and "Away 0" in html
    assert payload["params_hash"] in html
    assert "somehash123" in html
    assert "j1_2026_2026-08-21.json" in html
    assert "Statistical model output for research and entertainment" in html


def test_render_index_never_shows_market_prices():
    """The schema this project's predict step actually writes carries no
    per-fixture market probability or price (DESIGN_SITE.md 1.1/2.6) -- this
    just pins that the template doesn't invent one. `market_raw`/`market_p`
    are the schema's own field names for it (DESIGN_SITE.md 2.2); "Pinnacle"
    is the specific bookmaker used in the backtest and belongs on the
    Methodology page discussing that backtest, never on a live round page."""
    html = render_index(_payload(), "hash")
    assert "market_raw" not in html
    assert "market_p" not in html
    assert "Pinnacle" not in html


def _contains_word(text: str, word: str) -> bool:
    """Whole-word (or whole-phrase) containment -- a naive substring check
    would flag "information" for containing "formation", or
    "block-bootstrapped" for containing "lock"."""
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def test_render_index_avoids_tipster_language():
    html = render_index(_payload(), "hash").lower()
    for word in BANNED_WORDS:
        assert not _contains_word(html, word), f"banned word {word!r} found in rendered index.html"


def test_render_index_unplayed_fixture_says_not_yet_played():
    html = render_index(_payload(), "hash")
    assert "not yet played" in html


def test_render_index_shows_result_once_reconciled():
    payload = _payload(n=1)
    payload["matches"][0]["result"] = {
        "fthg": 2.0, "ftag": 1.0, "ftr": "H", "y": 0,
        "rps_raw": 0.1, "rps_cal": 0.09, "ll_raw": 0.5, "ll_cal": 0.4,
    }
    payload["round_summary"] = {
        "n_matches": 1, "n_resolved": 1, "complete": True,
        "mean_rps_raw": 0.1, "mean_rps_cal": 0.09, "mean_ll_raw": 0.5, "mean_ll_cal": 0.4,
    }
    html = render_index(payload, "hash")
    assert "2–1 (H)" in html or "2-1 (H)" in html or "2&ndash;1 (H)" in html
    assert "Round result" in html


# --- methodology.html ---------------------------------------------------------
def test_render_methodology_cites_confirmation_run():
    html = render_methodology()
    assert "3,882" in html
    assert "Dixon" in html
    assert "Statistical model output for research and entertainment" in html


def test_render_methodology_avoids_tipster_language():
    html = render_methodology().lower()
    for word in BANNED_WORDS:
        assert not _contains_word(html, word), f"banned word {word!r} found in rendered methodology.html"


# --- record.html ---------------------------------------------------------------
def test_render_record_empty_track_says_no_matches_yet():
    empty = pd.DataFrame(columns=[
        "round_id", "season", "commence_time", "home_team", "away_team",
        "p_raw_h", "p_raw_d", "p_raw_a", "p_cal_h", "p_cal_d", "p_cal_a",
        "y", "ftr", "rps_raw", "rps_cal", "ll_raw", "ll_cal",
        "model_version", "params_hash",
    ])
    html = render_record(empty, first_round_id="2026-08-21")
    assert "0 matches reconciled yet" in html
    assert "Recording began" in html


def test_render_record_below_threshold_says_too_few_matches():
    track = pd.DataFrame([
        {"round_id": "2026-08-21", "rps_raw": 0.2, "rps_cal": 0.19},
    ])
    html = render_record(track, first_round_id="2026-08-21")
    assert "Too few matches to mean anything yet" in html
    assert "n = 1" in html


def test_render_record_never_mixes_backtest_and_live_in_one_number():
    """DESIGN_SITE.md 1.1's second design rule: backtest and live figures
    must never be pooled into a single statistic. This is a coarse but
    cheap structural check -- the live mean-RPS and the backtest's d_RPS
    are visibly distinct numbers, computed by disjoint code paths in the
    template (`_live_record_summary` vs. `facts.J1_CONFIRMATION_RUN`)."""
    track = pd.DataFrame([{"round_id": "2026-08-21", "rps_raw": 0.2, "rps_cal": 0.19}])
    html = render_record(track, first_round_id="2026-08-21")
    assert "0.1900" in html  # live mean RPS (calibrated), from the track frame
    assert "0.0044" in html  # backtest d_RPS (facts.J1_CONFIRMATION_RUN), separately


# --- build_site orchestration --------------------------------------------------
def test_build_site_writes_all_pages(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    _write(predictions_dir, _payload())
    out_dir = tmp_path / "site"

    result = build_site(predictions_dir=predictions_dir, output_dir=out_dir)

    assert result["ok"] is True
    assert result["round_id"] == "2026-08-21"
    assert result["n_fixtures"] == 2
    assert result["track_record_rows"] == 0
    for name in ("index.html", "methodology.html", "record.html", "style.css"):
        assert (out_dir / name).exists(), name

    index_text = (out_dir / "index.html").read_text(encoding="utf-8")
    assert file_sha256(predictions_dir / "j1_2026_2026-08-21.json") in index_text


def test_build_site_no_predictions_is_not_ok(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    out_dir = tmp_path / "site"

    result = build_site(predictions_dir=predictions_dir, output_dir=out_dir)

    assert result["ok"] is False
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_build_site_picks_the_newest_round(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    _write(predictions_dir, _payload(round_id="2026-08-21", generated_at="2026-08-20T10:00:00+00:00"))
    _write(predictions_dir, _payload(round_id="2026-08-28", generated_at="2026-08-27T10:00:00+00:00"))
    out_dir = tmp_path / "site"

    result = build_site(predictions_dir=predictions_dir, output_dir=out_dir)

    assert result["ok"] is True
    assert result["round_id"] == "2026-08-28"


def test_build_site_every_page_has_the_fixed_disclaimer(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    _write(predictions_dir, _payload())
    out_dir = tmp_path / "site"

    build_site(predictions_dir=predictions_dir, output_dir=out_dir)

    disclaimer = "Statistical model output for research and entertainment. Not betting advice. Not affiliated with the J.League or any club."
    for name in ("index.html", "methodology.html", "record.html"):
        text = (out_dir / name).read_text(encoding="utf-8")
        assert disclaimer in text, name
