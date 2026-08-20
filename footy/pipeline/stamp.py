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
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


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
