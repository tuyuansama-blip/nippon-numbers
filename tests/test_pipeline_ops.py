"""Publish (git commit), per-round stamp, and the `weekly` orchestrator
(DESIGN_PHASE2.md 9, DESIGN_SITE.md 3.2). Publishing is exercised against a
throwaway local git repository under `tmp_path` -- never the project's own
repository -- and stamping never makes a network call (DESIGN.md 4).
"""

from __future__ import annotations

import hashlib
import json
import subprocess

import pandas as pd
import pytest

from footy.pipeline.publish import publish, round_tag
from footy.pipeline.stamp import stamp_round
from footy.pipeline.weekly import (
    WEEKDAY_STEPS,
    run_weekly,
    step_predict,
    step_publish,
    step_reconcile,
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


# --- weekly orchestration ----------------------------------------------------
@pytest.fixture(scope="module")
def world():
    return build_world()


def test_weekday_table_covers_thursday_predict_publish_stamp():
    assert WEEKDAY_STEPS[3] == ("predict", "publish", "stamp")


def test_step_predict_then_publish_then_stamp_dry_run(world, tmp_path):
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

    publish_result = step_publish(predict_result=predict_result, dry_run=True, repo_root=tmp_path)
    assert publish_result["ok"] is True
    assert publish_result["dry_run"] is True

    stamp_result = step_stamp(predict_result=predict_result)
    assert stamp_result["ok"] is True
    assert stamp_result["stamped"] is False


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
        steps=["predict", "publish", "stamp"],
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=snapshot_dir, predictions_dir=predictions_dir,
        repo_root=tmp_path, dry_run_publish=True, asof_utc=world["asof_utc"],
    )
    assert report["predict"]["ok"] is True
    assert report["publish"]["ok"] is True
    assert report["stamp"]["ok"] is True
    assert report["steps_run"] == ["predict", "publish", "stamp"]


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
