"""Publish (git commit), per-round stamp, and the `weekly` orchestrator
(DESIGN_PHASE2.md 9, DESIGN_SITE.md 3.2). Publishing is exercised against a
throwaway local git repository under `tmp_path` -- never the project's own
repository -- and stamping never makes a network call (DESIGN.md 4).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from footy.pipeline.predict import immutable_view
from footy.pipeline.publish import publish, round_tag
from footy.pipeline.stamp import ots_stamp_round, ots_upgrade, stamp_records, stamp_round
from footy.pipeline.weekly import (
    WEEKDAY_STEPS,
    run_weekly,
    stamp_paths,
    step_predict,
    step_publish,
    step_reconcile,
    step_scores,
    step_stamp,
)
from tests.j1_world import build_world, write_snapshot


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


# --- publish -------------------------------------------------------------------
def test_publish_dry_run_touches_nothing(tmp_path):
    _init_repo(tmp_path)
    target = tmp_path / "predictions" / "j1_2024_2024-05-03.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    result = publish([target], repo_root=tmp_path, tag="j1-2024-r2024-05-03", dry_run=True)
    assert result == {
        "ok": True, "dry_run": True, "committed": False,
        "would_add": ["predictions/j1_2024_2024-05-03.json"],
        "would_tag": "j1-2024-r2024-05-03",
    }
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                             capture_output=True, text=True, check=True)
    assert "predictions" in status.stdout   # untracked, never added


def test_publish_commits_and_tags(tmp_path):
    _init_repo(tmp_path)
    target = tmp_path / "predictions" / "j1_2024_2024-05-03.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"round_id": "2024-05-03"}', encoding="utf-8")

    result = publish([target], repo_root=tmp_path, tag="j1-2024-r2024-05-03", dry_run=False)
    assert result["ok"] is True
    assert result["committed"] is True
    assert result["tag"] == "j1-2024-r2024-05-03"

    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                          capture_output=True, text=True, check=True)
    assert "publish" in log.stdout
    tags = subprocess.run(["git", "tag"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "j1-2024-r2024-05-03" in tags.stdout.split()


def test_publish_is_idempotent_when_nothing_changed(tmp_path):
    _init_repo(tmp_path)
    target = tmp_path / "predictions" / "j1_2024_2024-05-03.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"round_id": "2024-05-03"}', encoding="utf-8")
    first = publish([target], repo_root=tmp_path, dry_run=False)
    assert first["committed"] is True

    second = publish([target], repo_root=tmp_path, dry_run=False)
    assert second["ok"] is True
    assert second["committed"] is False
    assert second["note"] == "nothing to commit"


def test_round_tag_format():
    assert round_tag({"season": 2026, "round_id": "2026-08-21"}) == "j1-2026-r2026-08-21"


# --- stamp -----------------------------------------------------------------
def test_stamp_round_records_the_digest_and_intent(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    path = predictions_dir / "j1_2024_2024-05-03.json"
    path.write_text('{"round_id": "2024-05-03"}', encoding="utf-8")

    payload = stamp_round(path)
    assert payload["stamped"] is False
    assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    sidecar = predictions_dir / "j1_2024_2024-05-03.ots.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["sha256"] == payload["sha256"]


def test_stamp_round_digest_changes_if_the_file_changes(tmp_path):
    path = tmp_path / "round.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    first = stamp_round(path)
    path.write_text('{"a": 2}', encoding="utf-8")
    second = stamp_round(path)
    assert first["sha256"] != second["sha256"]


# --- stamp: the networked half (docs/DESIGN_ACTIONS.md 5) ---------------------
class _FakeCompleted:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _runner_that_writes(ots_bytes=b"\x00ots"):
    """Stands in for `ots stamp`: writes the `.ots` the real client would.
    No network, no installed binary (DESIGN.md 4)."""
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        target = Path(args[-1])
        target.with_name(target.name + ".ots").write_bytes(ots_bytes)
        return _FakeCompleted()

    run.calls = calls
    return run


def test_ots_stamp_round_records_a_real_stamp(tmp_path):
    path = tmp_path / "j1_2026_2026-08-21.json"
    path.write_text('{"round_id": "2026-08-21"}', encoding="utf-8")

    runner = _runner_that_writes()
    payload = ots_stamp_round(path, runner=runner, ots_bin="/opt/ots/bin/ots")

    assert runner.calls == [["/opt/ots/bin/ots", "stamp", str(path)]]
    assert payload["stamped"] is True
    assert payload["ots_path"] == "j1_2026_2026-08-21.json.ots"
    assert payload["upgraded"] is False
    assert (tmp_path / "j1_2026_2026-08-21.json.ots").exists()
    assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_ots_stamp_round_downgrades_to_a_warning_when_the_client_is_missing(tmp_path):
    """DESIGN_PHASE2.md 9 makes L2 warning-only: a calendar being down, or
    `ots` not being installed, must never raise out of the publish path."""
    path = tmp_path / "j1_2026_2026-08-21.json"
    path.write_text("{}", encoding="utf-8")

    def missing(args, **kwargs):
        raise OSError("No such file or directory: 'ots'")

    payload = ots_stamp_round(path, runner=missing)
    assert payload["stamped"] is False
    assert payload["ots_path"] is None
    assert "warning-only" in payload["note"]
    assert Path(payload["path"]).exists()          # the record is still written


def test_ots_stamp_round_does_not_claim_a_stamp_on_a_nonzero_exit(tmp_path):
    path = tmp_path / "round.json"
    path.write_text("{}", encoding="utf-8")
    payload = ots_stamp_round(
        path, runner=lambda args, **kw: _FakeCompleted(1, "calendar unreachable")
    )
    assert payload["stamped"] is False
    assert "calendar unreachable" in payload["note"]


def test_ots_upgrade_reports_whether_the_attestation_grew(tmp_path):
    ots_path = tmp_path / "round.json.ots"
    ots_path.write_bytes(b"\x00pending")

    def upgrade(args, **kwargs):
        Path(args[-1]).write_bytes(b"\x00pending+bitcoin")
        return _FakeCompleted()

    grew = ots_upgrade(ots_path, runner=upgrade)
    assert grew == {"ok": True, "file": "round.json.ots", "changed": True, "note": None}

    unchanged = ots_upgrade(ots_path, runner=lambda args, **kw: _FakeCompleted())
    assert unchanged["changed"] is False


def test_stamp_records_only_lists_stamps_with_a_surviving_ots(tmp_path):
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    ots_stamp_round(tmp_path / "a.json", runner=_runner_that_writes())
    # A stub record (stamped=false) and a record whose .ots was never written
    # are both invisible to the upgrade workflow.
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    stamp_round(tmp_path / "b.json")

    found = stamp_records(tmp_path)
    assert [record["file"] for _, record, _ in found] == ["a.json"]


# --- weekly orchestration ----------------------------------------------------
@pytest.fixture(scope="module")
def world():
    return build_world()


def test_weekday_table_stamps_before_it_publishes():
    """DESIGN_SITE.md 3.2-L2 requires the `.ots` in the *same* commit as the
    prediction it attests, which a stamp taken after `publish` can never be
    (docs/DESIGN_ACTIONS.md 5)."""
    assert WEEKDAY_STEPS[3] == ("predict", "stamp", "publish")


def test_step_predict_then_stamp_then_publish_dry_run(world, tmp_path):
    snapshot_dir = tmp_path / "odds_snapshots"
    snapshot_dir.mkdir()
    ts = world["asof_utc"].strftime("%Y%m%dT%H%M%SZ")
    write_snapshot(world["events"], snapshot_dir / f"j1_h2h_eu_{ts}.json")
    predictions_dir = tmp_path / "predictions"

    predict_result = step_predict(
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=snapshot_dir, predictions_dir=predictions_dir,
        asof_utc=world["asof_utc"],
    )
    assert predict_result["ok"] is True
    assert (predictions_dir / f"j1_2014_{world['next_round_commence0'].strftime('%Y-%m-%d')}.json").exists()

    stamp_result = step_stamp(predict_result=predict_result)
    assert stamp_result["ok"] is True
    assert stamp_result["stamped"] is False

    publish_result = step_publish(
        predict_result=predict_result, stamp_result=stamp_result,
        dry_run=True, repo_root=tmp_path,
    )
    assert publish_result["ok"] is True
    assert publish_result["dry_run"] is True
    # The stamp record is staged alongside the prediction, not left behind.
    assert any(name.endswith(".ots.json") for name in publish_result["would_add"])


def test_stamp_paths_adds_the_binary_ots_only_when_one_exists(tmp_path):
    record = tmp_path / "j1_2026_2026-08-21.ots.json"
    assert stamp_paths(None) == []
    assert stamp_paths({"path": str(record), "stamped": False, "ots_path": None}) == [str(record)]
    real = stamp_paths({
        "path": str(record), "stamped": True,
        "ots_path": "j1_2026_2026-08-21.json.ots",
    })
    assert real == [str(record), str(tmp_path / "j1_2026_2026-08-21.json.ots")]


def test_step_reconcile_is_a_noop_on_an_empty_predictions_dir(world, tmp_path):
    result = step_reconcile(matches_history=world["matches_history"], predictions_dir=tmp_path / "predictions")
    assert result["ok"] is True
    assert result["updated_files"] == []


def test_run_weekly_thursday_flow_with_injected_data(world, tmp_path):
    snapshot_dir = tmp_path / "odds_snapshots"
    snapshot_dir.mkdir()
    ts = world["asof_utc"].strftime("%Y%m%dT%H%M%SZ")
    write_snapshot(world["events"], snapshot_dir / f"j1_h2h_eu_{ts}.json")
    predictions_dir = tmp_path / "predictions"

    report = run_weekly(
        steps=list(WEEKDAY_STEPS[3]),
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=snapshot_dir, predictions_dir=predictions_dir,
        repo_root=tmp_path, dry_run_publish=True, asof_utc=world["asof_utc"],
    )
    assert report["predict"]["ok"] is True
    assert report["publish"]["ok"] is True
    assert report["stamp"]["ok"] is True
    assert report["steps_run"] == ["predict", "stamp", "publish"]
    # `run_weekly` threads the stamp into the commit, same as the manual flow.
    assert any(name.endswith(".ots.json") for name in report["publish"]["would_add"])


def _write_pending_prediction(path, *, event_id="evt1", home="Kashiwa Reysol",
                               away="V-Varen Nagasaki", commence="2026-08-21T10:00:00+00:00"):
    payload = {
        "schema_version": 1, "league": "jpn1", "season": 2026, "season_label": "2026-27",
        "round_id": "2026-08-21", "generated_at": "2026-08-20T10:00:00+00:00",
        "asof": "2026-08-20T10:00:00+00:00", "model_version": "test-v1",
        "params_hash": "deadbeef",
        "matches": [{
            "event_id": event_id, "commence_time": commence,
            "home_team": home, "away_team": away,
            "home_id": "jpn_1:x", "away_id": "jpn_1:y",
            "p_raw": {"h": 0.5, "d": 0.25, "a": 0.25},
            "p_calibrated": {"h": 0.5, "d": 0.25, "a": 0.25},
            "result": None,
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _kashiwa_score_events():
    return [{
        "id": "evt1", "sport_key": "soccer_japan_j_league",
        "commence_time": "2026-08-21T10:00:00Z", "completed": True,
        "home_team": "Kashiwa Reysol", "away_team": "V-Varen Nagasaki",
        "scores": [
            {"name": "Kashiwa Reysol", "score": "4"},
            {"name": "V-Varen Nagasaki", "score": "2"},
        ],
    }]


# --- scores (Friday/Saturday/Sunday night, DESIGN_PHASE2.md 9 + the odds-api
# provisional-results extension) ------------------------------------------------
def test_weekday_table_puts_scores_on_friday_saturday_sunday_nights():
    assert WEEKDAY_STEPS[4] == ("scores",)
    assert WEEKDAY_STEPS[5] == ("scores",)
    assert WEEKDAY_STEPS[6] == ("scores",)


def test_step_scores_applies_injected_score_events_without_touching_the_network(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    path = predictions_dir / "j1_2026_2026-08-21.json"
    _write_pending_prediction(path)

    result = step_scores(score_events=_kashiwa_score_events(), predictions_dir=predictions_dir)
    assert result["ok"] is True
    assert result["updated_files"] == [path.name]

    written = json.loads(path.read_text(encoding="utf-8"))
    match_result = written["matches"][0]["result"]
    assert match_result["fthg"] == 4.0 and match_result["ftag"] == 2.0
    assert match_result["source"] == "odds_api_provisional"


def test_step_scores_is_a_soft_noop_without_an_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    result = step_scores(predictions_dir=tmp_path / "predictions")
    assert result["ok"] is True
    assert result["updated_files"] == []
    assert "ODDS_API_KEY" in result["note"]


def test_run_weekly_scores_step_with_injected_score_events(tmp_path):
    predictions_dir = tmp_path / "predictions"
    predictions_dir.mkdir()
    path = predictions_dir / "j1_2026_2026-08-21.json"
    _write_pending_prediction(path)

    report = run_weekly(
        steps=["scores"], predictions_dir=predictions_dir, score_events=_kashiwa_score_events(),
    )
    assert report["scores"]["ok"] is True
    assert report["scores"]["updated_files"] == [path.name]
    assert report["steps_run"] == ["scores"]


def test_run_weekly_stops_after_a_red_fetch(tmp_path, monkeypatch):
    import footy.pipeline.weekly as weekly_mod

    def fake_fetch(**kwargs):
        return {"ok": False, "problems": ["synthetic failure for the test"]}

    monkeypatch.setattr(weekly_mod, "step_fetch", fake_fetch)
    report = run_weekly(steps=["fetch", "reconcile", "calibrate"])
    assert report["fetch"]["ok"] is False
    assert report["stopped_after"] == "fetch"
    assert "reconcile" not in report
    assert report["steps_run"] == ["fetch"]


# --- immutability of a published round (DESIGN_SITE.md 3.3) -------------------
def _predict_once(world, tmp_path):
    snapshot_dir = tmp_path / "odds_snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    ts = world["asof_utc"].strftime("%Y%m%dT%H%M%SZ")
    write_snapshot(world["events"], snapshot_dir / f"j1_h2h_eu_{ts}.json")
    return lambda predictions_dir, **kw: step_predict(
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=snapshot_dir, predictions_dir=predictions_dir,
        asof_utc=world["asof_utc"], **kw,
    )


def test_step_predict_is_a_no_op_when_the_round_is_already_published(world, tmp_path):
    """A retried or manually re-dispatched Thursday run must not rewrite a
    prediction that is already committed (docs/DESIGN_ACTIONS.md 6)."""
    predict = _predict_once(world, tmp_path)
    predictions_dir = tmp_path / "predictions"

    first = predict(predictions_dir)
    assert first["written"] is True
    before = (Path(first["json_path"])).read_bytes()

    second = predict(predictions_dir)
    assert second["ok"] is True
    assert second["written"] is False
    assert "already published" in second["note"]
    assert Path(first["json_path"]).read_bytes() == before


def test_step_predict_refuses_to_overwrite_a_changed_round(world, tmp_path):
    predict = _predict_once(world, tmp_path)
    predictions_dir = tmp_path / "predictions"
    first = predict(predictions_dir)

    published = json.loads(Path(first["json_path"]).read_text(encoding="utf-8"))
    published["matches"][0]["p_calibrated"]["h"] += 0.05
    Path(first["json_path"]).write_text(json.dumps(published, indent=2), encoding="utf-8")

    blocked = predict(predictions_dir)
    assert blocked["ok"] is False
    assert "fixture probabilities" in blocked["reason"]


def test_immutable_view_ignores_the_two_always_moving_timestamps():
    """`asof`/`generated_at` are protected by never rewriting the file, not
    by the comparison -- see the note beside IMMUTABLE_TOP_FIELDS."""
    base = {
        "round_id": "2026-08-21", "season": 2026, "model_version": "v1",
        "params_hash": "abc", "matches": [],
    }
    assert immutable_view({**base, "asof": "A", "generated_at": "A"}) == immutable_view(
        {**base, "asof": "B", "generated_at": "B"}
    )
    assert immutable_view(base) != immutable_view({**base, "params_hash": "def"})
