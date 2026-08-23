"""The two CI guards under `.github/scripts/` (docs/DESIGN_ACTIONS.md 7).

`check_prediction_immutability.py` is required to be independent of the
`footy` package (DESIGN_SITE.md 3.3's rule for `verify.py`, applied for the
same reason: a bug in the model's code must not be able to wave through a
change to the record that code produced). Independence buys a duplicated
field list, and a duplicated field list silently rots. This module is the
seam that stops it: it imports both copies and asserts they agree.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from footy.pipeline import predict as predict_module

SCRIPTS = Path(__file__).resolve().parent.parent / ".github" / "scripts"


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "check_prediction_immutability", SCRIPTS / "check_prediction_immutability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def test_the_guards_field_list_matches_the_pipelines():
    assert guard.IMMUTABLE_MATCH_FIELDS == predict_module.IMMUTABLE_MATCH_FIELDS
    assert guard.IMMUTABLE_TOP_FIELDS == predict_module.IMMUTABLE_TOP_FIELDS


def test_the_guard_does_not_import_footy():
    """Independence is the whole point; an `import footy` would quietly undo
    it long after anyone remembers why it mattered."""
    source = (SCRIPTS / "check_prediction_immutability.py").read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "footy" in line
    ]
    assert code_lines == []


def test_both_views_agree_on_a_real_payload_shape():
    payload = {
        "round_id": "2026-08-21", "season": 2026, "model_version": "v1",
        "params_hash": "abc", "asof": "2026-08-20T12:00:00+00:00",
        "matches": [{
            "event_id": "e1", "commence_time": "2026-08-21T10:00:00+00:00",
            "p_raw": {"h": 0.4, "d": 0.3, "a": 0.3},
            "p_calibrated": {"h": 0.41, "d": 0.29, "a": 0.30},
            "result": None,
        }],
    }
    assert guard.immutable_view(payload) == predict_module.immutable_view(payload)


# --- end-to-end against a throwaway repository --------------------------------
def _repo(tmp_path):
    def git(*args, **kwargs):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                               capture_output=True, text=True, **kwargs)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    return git


PAYLOAD = {
    "round_id": "2026-08-21", "season": 2026, "model_version": "v1",
    "params_hash": "abc",
    "matches": [{
        "event_id": "e1", "commence_time": "2026-08-21T10:00:00+00:00",
        "p_raw": {"h": 0.4, "d": 0.3, "a": 0.3},
        "p_calibrated": {"h": 0.41, "d": 0.29, "a": 0.30},
        "result": None,
    }],
}


def _run_guard(tmp_path, base):
    return subprocess.run(
        ["python3", str(SCRIPTS / "check_prediction_immutability.py"), base, "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    )


@pytest.fixture
def published(tmp_path):
    git = _repo(tmp_path)
    target = tmp_path / "predictions" / "j1_2026_2026-08-21.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(PAYLOAD, indent=2), encoding="utf-8")
    git("add", "predictions")
    git("commit", "-q", "-m", "publish round")
    return tmp_path, git, target


def test_guard_allows_a_result_being_filled_in(published):
    tmp_path, git, target = published
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["matches"][0]["result"] = {"fthg": 2, "ftag": 1, "ftr": "H"}
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    git("commit", "-q", "-am", "reconcile")

    result = _run_guard(tmp_path, "HEAD~1")
    assert result.returncode == 0, result.stdout


def test_guard_rejects_a_changed_probability(published):
    tmp_path, git, target = published
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["matches"][0]["p_calibrated"]["h"] = 0.99
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    git("commit", "-q", "-am", "oops")

    result = _run_guard(tmp_path, "HEAD~1")
    assert result.returncode == 1
    assert "fixture probabilities" in result.stdout


def test_guard_rejects_a_deleted_round(published):
    tmp_path, git, target = published
    target.unlink()
    git("commit", "-q", "-am", "remove")

    result = _run_guard(tmp_path, "HEAD~1")
    assert result.returncode == 1
    assert "never removed" in result.stdout


def test_guard_ignores_a_newly_added_round(published):
    """A first publication has no earlier revision to be immutable against."""
    tmp_path, git, _ = published
    new = tmp_path / "predictions" / "j1_2026_2026-08-28.json"
    new.write_text(json.dumps({**PAYLOAD, "round_id": "2026-08-28"}, indent=2), encoding="utf-8")
    git("add", "predictions")
    git("commit", "-q", "-m", "next round")

    result = _run_guard(tmp_path, "HEAD~1")
    assert result.returncode == 0, result.stdout


def test_guard_ignores_stamp_records(published):
    tmp_path, git, _ = published
    record = tmp_path / "predictions" / "j1_2026_2026-08-21.ots.json"
    record.write_text('{"stamped": false}', encoding="utf-8")
    git("add", "predictions")
    git("commit", "-q", "-m", "stamp")
    record.write_text('{"stamped": true}', encoding="utf-8")
    git("commit", "-q", "-am", "upgrade")

    result = _run_guard(tmp_path, "HEAD~1")
    assert result.returncode == 0, result.stdout


# --- the data boundary --------------------------------------------------------
def test_data_boundary_script_is_executable_and_covers_every_2_6_pattern():
    script = SCRIPTS / "check_data_boundary.sh"
    source = script.read_text(encoding="utf-8")
    for pattern in ("data/odds_snapshots/*", "data/raw/*", ".env", ".env.*"):
        assert f"check '{pattern}'" in source
    assert script.stat().st_mode & 0o111, "must be committed with the executable bit"


def test_data_boundary_script_fails_on_a_tracked_snapshot(tmp_path):
    git = _repo(tmp_path)
    snapshots = tmp_path / "data" / "odds_snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "j1_h2h_eu_20260820T072241Z.json").write_text("[]", encoding="utf-8")
    git("add", "-f", "data")
    git("commit", "-q", "-m", "oops")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "check_data_boundary.sh")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "must never enter the public repo" in result.stdout


def test_data_boundary_script_passes_on_a_clean_tree(tmp_path):
    git = _repo(tmp_path)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "init")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "check_data_boundary.sh")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "data boundary ok" in result.stdout


# --- the workflows themselves -------------------------------------------------
WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
DOCS = Path(__file__).resolve().parent.parent / "docs"


def _referenced_secrets():
    import re

    names = set()
    for path in WORKFLOWS.glob("*.yml"):
        names |= set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", path.read_text("utf-8")))
    return names


def test_every_secret_a_workflow_reads_is_documented():
    """A workflow that quietly grows a seventh secret is a workflow that
    fails on the user's first run with `''` where a key should be. Both the
    design note and the step-by-step setup guide have to name it."""
    design = (DOCS / "DESIGN_ACTIONS.md").read_text("utf-8")
    setup = (DOCS / "SETUP_ACTIONS.md").read_text("utf-8")
    for name in _referenced_secrets():
        assert f"`{name}`" in design, f"{name} is not in DESIGN_ACTIONS.md 8.1"
        assert name in setup, f"{name} is not in SETUP_ACTIONS.md STEP 5"


def test_the_secret_set_is_exactly_the_six_that_were_designed_for():
    assert _referenced_secrets() == {
        "ODDS_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
        "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    }


def test_no_workflow_stages_the_whole_tree():
    """`git add -A` in a workflow would sweep up whatever an earlier step
    left in the working directory -- including, on a bad day, a pulled odds
    snapshot. Every commit step names its paths."""
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text("utf-8")
        assert "git add -A" not in text and "git add ." not in text, path.name


def test_workflows_that_push_share_one_concurrency_group():
    """Four workflows push to main. Without a shared group two of them will
    eventually collide (docs/DESIGN_ACTIONS.md 6)."""
    pushers = [p for p in WORKFLOWS.glob("*.yml")
               if "git push origin" in p.read_text("utf-8")]
    assert {p.name for p in pushers} == {
        "predict.yml", "results.yml", "reconcile.yml", "ots-upgrade.yml"
    }
    for path in pushers:
        assert "group: repo-write" in path.read_text("utf-8"), path.name


def test_workflows_needing_tags_do_a_full_checkout():
    """`check_params_hash` reads the preregistration tag; a shallow checkout
    has none and the gate reports itself inactive instead of failing
    (docs/DESIGN_ACTIONS.md 0.10)."""
    for name in ("predict.yml", "reconcile.yml"):
        assert "fetch-depth: 0" in (WORKFLOWS / name).read_text("utf-8"), name
