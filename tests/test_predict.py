"""`footy predict` (DESIGN_PHASE2.md 9's Thursday step, 7.5's timestamping
ingredients). All synthetic -- `tests.j1_world` builds a name-resolvable J1
history plus a held-out matchday expressed as odds-snapshot events, never
touching the network or the real repository's `data/`.
"""

from __future__ import annotations

import json
import subprocess

import numpy as np
import pandas as pd
import pytest

from footy.config import JPN_PUBLISH_MIN_TRAIN_MATCHES
from footy.pipeline.predict import (
    check_params_hash,
    combined_snapshot_hash,
    frozen_params_hash,
    generate_prediction,
    load_snapshot_events,
    next_round_fixtures,
    publish_gate,
    render_markdown,
    snapshot_digests,
    write_prediction,
)
from tests.j1_world import build_world, write_snapshot


@pytest.fixture(scope="module")
def world():
    return build_world()


@pytest.fixture
def snapshot_dir(tmp_path, world):
    root = tmp_path / "odds_snapshots"
    root.mkdir()
    ts = world["asof_utc"].strftime("%Y%m%dT%H%M%SZ")
    write_snapshot(world["events"], root / f"j1_h2h_eu_{ts}.json")
    return root


# --- params_hash / preregistration gate --------------------------------------
def test_frozen_params_hash_is_deterministic():
    frozen = {"half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
              "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12}
    assert frozen_params_hash(frozen) == frozen_params_hash(dict(frozen))


def test_frozen_params_hash_ignores_non_model_fields():
    base = {"half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
            "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12}
    noisy = dict(base, created_at="2099-01-01", total_fits=99999, grid={"a": 1})
    assert frozen_params_hash(base) == frozen_params_hash(noisy)


def test_frozen_params_hash_is_sensitive_to_model_fields():
    base = {"half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
            "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12}
    changed = dict(base, sigma=0.5)
    assert frozen_params_hash(base) != frozen_params_hash(changed)


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_check_params_hash_inactive_without_a_tag(tmp_path):
    _init_repo(tmp_path)
    frozen = {"half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
              "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12}
    result = check_params_hash(frozen, repo_root=tmp_path)
    assert result["ok"] is True
    assert result["tag"] is None


def test_check_params_hash_matches_a_preregistered_tag(tmp_path):
    _init_repo(tmp_path)
    frozen = {"half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
              "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12}
    digest = frozen_params_hash(frozen)
    subprocess.run(
        ["git", "tag", "-a", "phase2-preregistered-2026-08-20",
         "-m", f"frozen_params_sha256={digest}\n"],
        cwd=tmp_path, check=True,
    )
    result = check_params_hash(frozen, repo_root=tmp_path)
    assert result["ok"] is True
    assert result["tag"] == "phase2-preregistered-2026-08-20"


def test_check_params_hash_flags_drift_from_the_tag(tmp_path):
    _init_repo(tmp_path)
    frozen = {"half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
              "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12}
    digest = frozen_params_hash(frozen)
    subprocess.run(
        ["git", "tag", "-a", "phase2-preregistered-2026-08-20",
         "-m", f"frozen_params_sha256={digest}\n"],
        cwd=tmp_path, check=True,
    )
    drifted = dict(frozen, sigma=0.8)
    result = check_params_hash(drifted, repo_root=tmp_path)
    assert result["ok"] is False
    assert "MISMATCH" in result["note"]


# --- odds-snapshot fixture list ----------------------------------------------
def test_load_snapshot_events_reads_the_round(snapshot_dir, world):
    events = load_snapshot_events(snapshot_dir)
    assert len(events) == len(world["events"])
    assert set(events["home_team"]) | set(events["away_team"]) <= set(world["teams"])
    assert events["home_id"].notna().all() and events["away_id"].notna().all()


def test_load_snapshot_events_latest_sighting_wins(tmp_path):
    root = tmp_path / "snap"
    root.mkdir()
    early = [{
        "id": "e1", "sport_key": "soccer_japan_j_league",
        "commence_time": "2026-08-21T10:00:00Z",
        "home_team": "Kashiwa Reysol", "away_team": "V-Varen Nagasaki",
        "bookmakers": [],
    }]
    late = [{
        "id": "e1", "sport_key": "soccer_japan_j_league",
        "commence_time": "2026-08-21T11:00:00Z",   # kickoff moved an hour later
        "home_team": "Kashiwa Reysol", "away_team": "V-Varen Nagasaki",
        "bookmakers": [],
    }]
    write_snapshot(early, root / "j1_h2h_eu_20260820T070000Z.json")
    write_snapshot(late, root / "j1_h2h_eu_20260820T080000Z.json")
    events = load_snapshot_events(root)
    assert len(events) == 1
    assert events.iloc[0]["commence_time"] == pd.Timestamp("2026-08-21T11:00:00Z")


def test_load_snapshot_events_hard_errors_on_unresolved_team(tmp_path):
    root = tmp_path / "snap"
    root.mkdir()
    events = [{
        "id": "e1", "sport_key": "soccer_japan_j_league",
        "commence_time": "2026-08-21T10:00:00Z",
        "home_team": "Not A Real Club", "away_team": "Kyoto",
        "bookmakers": [],
    }]
    write_snapshot(events, root / "j1_h2h_eu_20260820T070000Z.json")
    with pytest.raises(ValueError, match="unresolved J1 team spelling"):
        load_snapshot_events(root)


def test_next_round_fixtures_picks_only_the_nearest_round(snapshot_dir, world):
    """A snapshot can hold every future fixture the API knows about
    (DESIGN_PHASE2.md 8.4); `next_round_fixtures` must still stop at the
    first round, never carry a team into a second occurrence."""
    events = load_snapshot_events(snapshot_dir)
    # Duplicate the round a week later, as if the API also already had
    # odds posted for the round after -- same ids, later kickoffs.
    second_round = events.copy()
    second_round["commence_time"] = second_round["commence_time"] + pd.Timedelta(days=7)
    second_round["event_id"] = second_round["event_id"] + "_r2"
    combined = pd.concat([events, second_round], ignore_index=True)

    fixtures = next_round_fixtures(combined, asof_utc=world["asof_utc"])
    assert len(fixtures) == len(world["events"])
    assert fixtures["commence_time"].max() < events["commence_time"].max() + pd.Timedelta(days=1)
    # every team appears at most once
    ids = pd.concat([fixtures["home_id"], fixtures["away_id"]])
    assert ids.is_unique


def test_next_round_fixtures_respects_members_filter(snapshot_dir, world):
    events = load_snapshot_events(snapshot_dir)
    one_team_id = events.iloc[0]["home_id"]
    members = set(events["home_id"]) | set(events["away_id"])
    members.discard(one_team_id)
    fixtures = next_round_fixtures(events, asof_utc=world["asof_utc"], members=members)
    assert one_team_id not in set(fixtures["home_id"]) | set(fixtures["away_id"])


def test_next_round_fixtures_excludes_past_events(world):
    # Built directly (not via the loader) to isolate just the time filter.
    from footy.pipeline.predict import EVENT_COLUMNS

    rows = []
    for e in world["events"]:
        rows.append({
            "event_id": e["id"], "commence_time": pd.Timestamp(e["commence_time"]),
            "home_raw": e["home_team"], "away_raw": e["away_team"],
            "home_team": e["home_team"], "away_team": e["away_team"],
            "home_id": "x", "away_id": "y",
            "snapshot_file": "f", "snapshot_ts": "t",
        })
    frame = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    past_asof = frame["commence_time"].max() + pd.Timedelta(days=1)
    fixtures = next_round_fixtures(frame, asof_utc=past_asof)
    assert fixtures.empty


# --- snapshot hashing ----------------------------------------------------------
def test_snapshot_digests_and_combined_hash(snapshot_dir):
    paths = sorted(snapshot_dir.glob("*.json"))
    digests = snapshot_digests(paths)
    assert set(digests) == {p.name for p in paths}
    for path in paths:
        assert digests[path.name] == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    # deterministic regardless of dict insertion order
    reversed_digests = dict(reversed(list(digests.items())))
    assert combined_snapshot_hash(digests) == combined_snapshot_hash(reversed_digests)


# --- publish gate (unit) -------------------------------------------------------
class _FakeCheck:
    def __init__(self, ok, problems=()):
        self.ok = ok
        self.problems = list(problems)


class _FakeMeta:
    def __init__(self, n_matches):
        self.n_matches = n_matches


def _ok_hash_check():
    return {"ok": True, "current": "x", "note": "no tag"}


def test_publish_gate_passes_when_everything_is_clean():
    fixtures = pd.DataFrame({"home_id": ["a", "b"], "away_id": ["c", "d"]})
    gate = publish_gate(
        check_result=_FakeCheck(True), model_meta=_FakeMeta(400),
        fixtures=fixtures, params_hash_check=_ok_hash_check(),
    )
    assert gate == {"ok": True, "reasons": []}


def test_publish_gate_blocks_on_red_check():
    fixtures = pd.DataFrame({"home_id": ["a"], "away_id": ["c"]})
    gate = publish_gate(
        check_result=_FakeCheck(False, ["bad thing"]), model_meta=_FakeMeta(400),
        fixtures=fixtures, params_hash_check=_ok_hash_check(),
    )
    assert gate["ok"] is False
    assert any("footy check" in r for r in gate["reasons"])


def test_publish_gate_blocks_on_small_training_window():
    fixtures = pd.DataFrame({"home_id": ["a"], "away_id": ["c"]})
    gate = publish_gate(
        check_result=_FakeCheck(True), model_meta=_FakeMeta(JPN_PUBLISH_MIN_TRAIN_MATCHES - 1),
        fixtures=fixtures, params_hash_check=_ok_hash_check(),
    )
    assert gate["ok"] is False
    assert any("training window" in r for r in gate["reasons"])


def test_publish_gate_blocks_on_unresolved_team_id():
    fixtures = pd.DataFrame({"home_id": ["a", None], "away_id": ["c", "d"]})
    gate = publish_gate(
        check_result=_FakeCheck(True), model_meta=_FakeMeta(400),
        fixtures=fixtures, params_hash_check=_ok_hash_check(),
    )
    assert gate["ok"] is False
    assert any("unresolved team_id" in r for r in gate["reasons"])


def test_publish_gate_blocks_on_params_hash_mismatch():
    fixtures = pd.DataFrame({"home_id": ["a"], "away_id": ["c"]})
    gate = publish_gate(
        check_result=_FakeCheck(True), model_meta=_FakeMeta(400),
        fixtures=fixtures,
        params_hash_check={"ok": False, "note": "MISMATCH -- drift"},
    )
    assert gate["ok"] is False
    assert any("params_hash" in r for r in gate["reasons"])


# --- generate_prediction end to end --------------------------------------------
def test_generate_prediction_end_to_end(world, snapshot_dir, tmp_path):
    result = generate_prediction(
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=snapshot_dir, asof_utc=world["asof_utc"], repo_root=tmp_path,
    )
    assert result["ok"] is True
    payload = result["payload"]
    assert payload["publish_gate"]["ok"] is True
    assert payload["league"] == "jpn1"
    assert len(payload["matches"]) == len(world["events"])
    assert payload["training_window"]["n_matches"] >= JPN_PUBLISH_MIN_TRAIN_MATCHES

    # 7.5's required fields, all present before anything is written to disk
    for key in ("asof", "model_version", "params_hash", "phi"):
        assert key in payload
    assert set(payload["odds_snapshot"]["files"]) <= {p.name for p in snapshot_dir.glob("*.json")}
    assert len(payload["odds_snapshot"]["sha256"]) == len(payload["odds_snapshot"]["files"])

    for match in payload["matches"]:
        assert match["result"] is None
        h, d, a = match["p_calibrated"]["h"], match["p_calibrated"]["d"], match["p_calibrated"]["a"]
        assert h > 0 and d > 0 and a > 0
        assert abs(h + d + a - 1.0) < 1e-9

    json_path, md_path = write_prediction(payload, predictions_dir=tmp_path / "predictions")
    assert json_path.exists() and md_path.exists()
    reloaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert reloaded == payload
    assert "publish gate: PASS" in md_path.read_text(encoding="utf-8")


def test_generate_prediction_blocks_on_a_bad_overround_row(world, snapshot_dir, tmp_path):
    """A single historically-bad row is enough to turn `footy check` red and
    block the whole publish gate -- exercised here with a synthetic outlier
    rather than the real 2013 J1 row this project's own data happens to have."""
    bad_history = world["matches_history"].copy()
    bad_history.loc[bad_history.index[0], "psch"] = 1.01
    bad_history.loc[bad_history.index[0], "pscd"] = 1.01
    bad_history.loc[bad_history.index[0], "psca"] = 1.01

    result = generate_prediction(
        matches_history=bad_history, frozen=world["frozen"],
        snapshot_dir=snapshot_dir, asof_utc=world["asof_utc"], repo_root=tmp_path,
    )
    assert result["ok"] is True
    assert result["payload"]["publish_gate"]["ok"] is False
    assert any("footy check" in r for r in result["payload"]["publish_gate"]["reasons"])


def test_generate_prediction_no_snapshot_available(world, tmp_path):
    empty_dir = tmp_path / "empty_snapshots"
    empty_dir.mkdir()
    result = generate_prediction(
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=empty_dir, asof_utc=world["asof_utc"],
    )
    assert result["ok"] is False
    assert "snapshot" in result["reason"]


def test_render_markdown_lists_every_fixture(world, snapshot_dir, tmp_path):
    result = generate_prediction(
        matches_history=world["matches_history"], frozen=world["frozen"],
        snapshot_dir=snapshot_dir, asof_utc=world["asof_utc"], repo_root=tmp_path,
    )
    md = render_markdown(result["payload"])
    for match in result["payload"]["matches"]:
        assert match["home_team"] in md
        assert match["away_team"] in md
