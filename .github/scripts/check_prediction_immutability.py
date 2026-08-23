#!/usr/bin/env python3
"""Refuse a diff that changes a published prediction (DESIGN_SITE.md 3.3).

3.3's rule: once `predictions/j1_*.json` is committed, only `result` may be
filled in afterwards. Everything else -- the fixtures, the probabilities, the
round's identity, the params hash -- is the record, and rewriting it is the
single failure this project cannot recover from.

`footy/pipeline/predict.py`'s `existing_conflict` already enforces this
*inside* the pipeline, which covers the Thursday workflow retrying or being
re-dispatched. This script covers the other half: a human (or a future
script) editing a file and pushing it. It compares each modified prediction
against its parent revision.

Deliberately stdlib-only and free of any `footy` import -- the same
independence rule DESIGN_SITE.md 3.3 sets for `verify.py`, for the same
reason: a bug in the model's own code must not be able to wave through a
change to the record it produced. `tests/test_ci_guards.py` pins the field
list below against `predict.py`'s so the two cannot silently diverge.

    check_prediction_immutability.py <base-ref> [<head-ref>]
"""

from __future__ import annotations

import json
import subprocess
import sys

IMMUTABLE_MATCH_FIELDS = ("event_id", "commence_time", "p_raw", "p_calibrated")
IMMUTABLE_TOP_FIELDS = ("round_id", "season", "model_version", "params_hash")
PREDICTION_PREFIX = "predictions/j1_"


def immutable_view(payload: dict) -> dict:
    return {
        "top": {key: payload.get(key) for key in IMMUTABLE_TOP_FIELDS},
        "matches": [
            {key: match.get(key) for key in IMMUTABLE_MATCH_FIELDS}
            for match in payload.get("matches", [])
        ],
    }


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def modified_predictions(base: str, head: str) -> list[str]:
    """Prediction files this range *modified* -- an added file has no earlier
    revision to be immutable against, and a deleted one is caught by the
    `D` filter below rather than parsed."""
    out = _git("diff", "--name-status", "--diff-filter=MD", base, head)
    names = []
    for line in out.splitlines():
        status, _, name = line.partition("\t")
        name = name.strip()
        if not name.startswith(PREDICTION_PREFIX) or not name.endswith(".json"):
            continue
        if name.endswith(".ots.json"):
            continue                       # a stamp record, not a prediction
        names.append((status.strip(), name))
    return names


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    base = argv[0]
    head = argv[1] if len(argv) > 1 else "HEAD"

    problems: list[str] = []
    for status, name in modified_predictions(base, head):
        if status.startswith("D"):
            problems.append(f"{name}: deleted -- a published round is never removed")
            continue
        try:
            before = json.loads(_git("show", f"{base}:{name}"))
            after = json.loads(_git("show", f"{head}:{name}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            problems.append(f"{name}: could not be compared ({exc})")
            continue

        old, new = immutable_view(before), immutable_view(after)
        if old == new:
            continue
        changed = [k for k in IMMUTABLE_TOP_FIELDS if old["top"][k] != new["top"][k]]
        if len(old["matches"]) != len(new["matches"]):
            changed.append(f"fixture count {len(old['matches'])} -> {len(new['matches'])}")
        elif old["matches"] != new["matches"]:
            changed.append("fixture probabilities or identities")
        problems.append(f"{name}: changed {', '.join(changed) or 'an immutable field'}")

    if problems:
        print("::error::a published prediction was modified (DESIGN_SITE.md 3.3)")
        for problem in problems:
            print(f"    {problem}")
        print("\nOnly `result` may be filled in after a round is published.")
        return 1

    print("prediction immutability ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
