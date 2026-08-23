"""Per-round OpenTimestamps stamp -- DESIGN_SITE.md 3.2's L2 layer.

`DESIGN_PHASE2.md` 7.5 originally specified a monthly Merkle-root batch for
this. `docs/DESIGN_SITE.md` 3.1 found that flawed on its own terms: a
monthly batch only proves anything once the *month* is over, i.e. after
every match in it has already been played -- it cannot show a prediction was
fixed *before* kickoff, which is the entire point of stamping. 3.2 replaces
it with one stamp per round, taken at `publish` time, so the attestation
race is against that round's own first kickoff, not against the end of the
month.

This module is deliberately thin and network-free, matching the coordinator
note that this layer should stay a small, swappable piece rather than
built-out batch machinery: it computes the digest that would be stamped and
records the *intent* to stamp it (`stamped: false`). Actually calling an
OpenTimestamps calendar server (`ots stamp`) is a real outbound network
request no test here is allowed to make (DESIGN.md 4) and this sandboxed
environment cannot make either -- 3.2 itself only requires the `.ots` file
to land in the *same commit* as the prediction, which a networked publish
run (or a human/cron step with `opentimestamps-client` installed) does by
calling `stamp_round` and then overwriting `stamped`/`ots_path` once the
real `.ots` exists. L2 is one of three independent layers (L0 Wayback, L1
GitHub Actions server witness, L3 social post); a missing stamp here is a
warning, never a publish blocker (9's failure-mode column: "失敗は警告の
み"), and L1 (predictions generated inside GitHub Actions) is the site
phase's job, out of scope here.

**Update (docs/DESIGN_ACTIONS.md 5).** The paragraph above describes the
stub, which is still what `stamp_round` does and still what the test suite
exercises. `ots_stamp_round` below is the networked half the docstring asks
for: under GitHub Actions the `opentimestamps-client` binary is present and
outbound network is allowed, so the workflow calls that instead and a real
`.ots` lands in the *same commit* as the prediction (3.2's requirement).
Both functions return the same record shape, `stamped` being the flag that
says which one ran, and `ots_stamp_round` never raises -- a calendar server
being down is a warning, exactly as 9's failure-mode column requires. The
`subprocess` call is injectable (`runner=`) so the decision logic stays
testable without a network or an installed binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

DEFAULT_OTS_BIN = "ots"
OTS_TIMEOUT_SEC = 120


def stamp_round(json_path, *, out_dir=None) -> dict:
    """The digest DESIGN_SITE.md 3.2-L2 would `ots stamp`, plus a stub
    record of the not-yet-submitted attestation. `out_dir` defaults to the
    prediction file's own directory, so the stub sits next to
    `j1_<season>_<round>.json` as `j1_<season>_<round>.ots.json` -- the same
    place a real `.ots` binary would go once a networked run replaces this.
    """
    path = Path(json_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {
        "file": path.name,
        "sha256": digest,
        "stamped": False,
        "ots_path": None,
        "note": (
            "OpenTimestamps submission not performed by this pipeline run (no "
            "outbound network here); run `ots stamp` on this sha256 (or on the "
            "file itself) from a networked machine, commit the resulting .ots "
            "alongside the prediction, and set stamped=true -- "
            "DESIGN_SITE.md 3.2, layer L2."
        ),
    }
    target = Path(out_dir) if out_dir else path.parent
    target.mkdir(parents=True, exist_ok=True)
    out_path = target / f"{path.stem}.ots.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["path"] = str(out_path)
    return payload


# --- the networked half (docs/DESIGN_ACTIONS.md 5) ----------------------------
def _default_runner(args, *, timeout: int = OTS_TIMEOUT_SEC):
    """`subprocess.run` for the `ots` CLI. Never raises on a non-zero exit --
    the caller turns that into a warning record instead."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ots_binary(env=None) -> str:
    """`FOOTY_OTS_BIN` when set, else `ots` on PATH.

    The workflow installs `opentimestamps-client` into its own throwaway
    virtualenv (docs/SETUP_ACTIONS.md) rather than into the project
    environment, because its `python-bitcoinlib` dependency tracks a slower
    Python-version cadence than this project's pinned interpreter -- so the
    binary is generally *not* on PATH and its absolute path is handed over
    through this variable.
    """
    env = env if env is not None else os.environ
    return env.get("FOOTY_OTS_BIN") or DEFAULT_OTS_BIN


def ots_stamp_round(json_path, *, out_dir=None, runner=None, ots_bin=None) -> dict:
    """`ots stamp <prediction.json>` -> a real `.ots` beside it, plus the same
    record `stamp_round` writes with `stamped: true`.

    Returns rather than raises on every failure path (binary absent, calendar
    unreachable, non-zero exit): DESIGN_PHASE2.md 9 makes L2 a warning-only
    layer, and a publish that stopped because an OpenTimestamps calendar was
    down would trade the strongest guarantee (L1: the prediction exists,
    committed by Actions, before kickoff) for the weakest.
    """
    path = Path(json_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    binary = ots_bin or ots_binary()
    run = runner or _default_runner

    ots_path = path.with_name(path.name + ".ots")
    note = None
    try:
        completed = run([binary, "stamp", str(path)])
        returncode = completed.returncode
        stderr = (completed.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        returncode, stderr = -1, str(exc)

    stamped = returncode == 0 and ots_path.exists()
    if not stamped:
        note = (
            f"`{binary} stamp` did not produce {ots_path.name} "
            f"(exit {returncode}): {stderr[:300] or 'no stderr'} -- L2 is "
            "warning-only (DESIGN_PHASE2.md 9); L1/L3 still stand"
        )

    payload = {
        "file": path.name,
        "sha256": digest,
        "stamped": stamped,
        "ots_path": ots_path.name if stamped else None,
        "upgraded": False,
        "note": note or (
            "OpenTimestamps commitment submitted to the public calendars. The "
            "attestation is incomplete until a Bitcoin block confirms it: run "
            "`ots upgrade` on the .ots a few hours later and re-commit the .ots "
            "only, never the prediction JSON (DESIGN_SITE.md 3.2)."
        ),
    }
    target = Path(out_dir) if out_dir else path.parent
    target.mkdir(parents=True, exist_ok=True)
    out_path = target / f"{path.stem}.ots.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["path"] = str(out_path)
    return payload


def ots_upgrade(ots_path, *, runner=None, ots_bin=None) -> dict:
    """`ots upgrade <file.ots>` -- attaches the Bitcoin attestation once a
    block has confirmed the calendar's commitment.

    DESIGN_SITE.md 3.2's operating rule is that this must never touch the
    prediction JSON's bytes; that is why the command is pointed at the `.ots`
    and why the workflow that calls it stages `*.ots` alone.
    """
    path = Path(ots_path)
    binary = ots_bin or ots_binary()
    run = runner or _default_runner
    before = path.read_bytes() if path.exists() else b""
    try:
        completed = run([binary, "upgrade", str(path)])
        returncode = completed.returncode
        stderr = (completed.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        returncode, stderr = -1, str(exc)
    after = path.read_bytes() if path.exists() else b""
    return {
        "ok": returncode == 0,
        "file": path.name,
        "changed": after != before,
        "note": stderr[:300] or None,
    }


def stamp_records(predictions_dir):
    """Every `<round>.ots.json` record under a predictions directory, paired
    with the `.ots` it points at -- what the upgrade workflow iterates."""
    root = Path(predictions_dir)
    if not root.exists():
        return []
    found = []
    for record_path in sorted(root.glob("*.ots.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not record.get("stamped") or not record.get("ots_path"):
            continue
        ots_path = root / record["ots_path"]
        if ots_path.exists():
            found.append((record_path, record, ots_path))
    return found
