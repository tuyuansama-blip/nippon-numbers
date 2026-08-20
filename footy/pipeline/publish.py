"""Git-commit publish step, one layer of DESIGN_PHASE2.md 7.5 / DESIGN_SITE.md
3.2 (the Thursday `publish` step of 9).

    1. 必須・自動: 予測ファイルを公開git リポジトリに push。
    2. 必須・自動: 同じ round のオッズスナップショットを同時にコミットする。
    3. 推奨: OpenTimestamps (footy/pipeline/stamp.py).

**This layer is weaker than 7.5-1 originally claimed.** DESIGN_SITE.md 3.1
found that a commit's author/committer date is not third-party evidence --
`GIT_COMMITTER_DATE` lets the author set it to anything, so a local `git
commit` alone proves nothing about *when* a prediction was actually fixed.
What third-party record exists is GitHub's own receipt of the push (short
retention) or, properly, a workflow run's server-side `created_at` when the
commit is made *inside* GitHub Actions (DESIGN_SITE.md 3.2's L1) -- that
"predictions are generated and committed by github-actions[bot], not a
human, from a public workflow file" layer is the site phase's job and is out
of scope here. This module supplies the commit half only; combine it with
`stamp.py`'s per-round digest (L2) for anything that needs to survive a
skeptic, and treat the commit itself as bookkeeping (a stable, diffable
history) rather than proof.

**Deviation from 7.5-2, and why.** This project's own `.gitignore` excludes
`data/odds_snapshots/` with the comment "Snapshots pulled from a paid odds
API ... not ours to redistribute" -- a licensing constraint tier 2 does not
account for. Committing the raw snapshot bytes into a public repository
would contradict that existing decision, so `publish` does not do it. What
it *does* commit is exactly what 7.5-1 requires of the prediction file
itself: `odds_snapshot["sha256"]`/`["combined_sha256"]` are already baked
into the JSON payload by `predict.generate_prediction` before this module
ever runs, so the market evidence is still frozen at commit time -- provable
against a re-derived hash of the (locally retained, not redistributed)
snapshot -- even though the snapshot bytes themselves never enter git
history. This is recorded once, here, rather than silently: tier 2's actual
intent ("公開時点で市場が何を言っていたか」を後から作れないようにする") is
met by the hash; only the redistribution of third-party priced data is
declined.

`publish` never pushes. Pushing to a remote is a network action outside this
module's job (and outside what a test can exercise); it stops at a local
commit (+ optional tag), which is what DESIGN_PHASE2.md 7.5-1 actually
requires for the "third-party timestamp" property once a push does happen.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from footy.config import ROOT


def _default_git(cwd: Path):
    def run(args):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    return run


def publish(
    paths, *, repo_root=None, tag: str | None = None, message: str | None = None,
    dry_run: bool = True, git=None,
) -> dict:
    """`git add` the given paths, commit, and optionally tag. `dry_run=True`
    (the default -- callers must opt into a real commit) reports exactly what
    it would do without touching the repository."""
    root = Path(repo_root) if repo_root else ROOT
    paths = [Path(p) for p in paths]
    rel_paths = [str(p.relative_to(root)) if p.is_absolute() else str(p) for p in paths]

    if dry_run:
        return {
            "ok": True, "dry_run": True, "committed": False,
            "would_add": rel_paths, "would_tag": tag,
        }

    run = git or _default_git(root)
    add = run(["add", *rel_paths])
    if add.returncode != 0:
        return {"ok": False, "dry_run": False, "step": "add", "stderr": add.stderr}

    commit_msg = message or f"predict: publish {', '.join(p.stem for p in paths)}"
    commit = run(["commit", "-m", commit_msg])
    if commit.returncode != 0:
        if "nothing to commit" in (commit.stdout + commit.stderr):
            return {"ok": True, "dry_run": False, "committed": False, "note": "nothing to commit"}
        return {"ok": False, "dry_run": False, "step": "commit", "stderr": commit.stderr}

    sha = run(["rev-parse", "HEAD"]).stdout.strip()
    tag_result = None
    if tag:
        tag_result = run(["tag", tag])
        if tag_result.returncode != 0 and "already exists" not in tag_result.stderr:
            return {
                "ok": False, "dry_run": False, "step": "tag",
                "commit": sha, "stderr": tag_result.stderr,
            }

    return {
        "ok": True, "dry_run": False, "committed": True, "commit": sha,
        "tag": tag if (tag_result is None or tag_result.returncode == 0) else None,
    }


def round_tag(payload: dict) -> str:
    """`j1-<season>-r<round_id>` (DESIGN_PHASE2.md 9's publish step)."""
    return f"j1-{payload['season']}-r{payload['round_id']}"
