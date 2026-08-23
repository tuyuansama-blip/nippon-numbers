"""`footy odds sync` -- keep `data/odds_snapshots/` and the R2 bucket in step
(docs/DESIGN_ACTIONS.md 2, 3).

Three properties carry the whole module, and each is a pure function below so
the network layer stays a thin shell (DESIGN.md 4):

* **Snapshots are immutable and append-only.** `odds/ingest.py` already says
  so; sync therefore never overwrites or deletes one. A name present on both
  sides is skipped without reading either copy, which is what makes the
  Thursday `predict` pull cheap as the season grows.
* **The schedule state is the one mutable object**, and it is the reason the
  odds workflow needs a `concurrency` group: two runners that both read it,
  both collect, and both write it would each believe they had the whole
  picture and would re-spend credits on the other's points.
* **Direction is explicit.** There is no "mirror" mode that could delete. The
  worst a wrong invocation can do is upload a snapshot twice under the same
  (content-addressed-by-timestamp) name.
"""

from __future__ import annotations

import json
from pathlib import Path

from footy.odds.r2 import SNAPSHOT_PREFIX, STATE_KEY, R2Client, R2Config

SNAPSHOT_GLOB = "j1_h2h_eu_*.json"
STATE_FILENAME = ".schedule_state.json"


def snapshot_key(name: str, *, prefix: str = SNAPSHOT_PREFIX) -> str:
    return f"{prefix}{name}"


def snapshot_names(keys, *, prefix: str = SNAPSHOT_PREFIX) -> list[str]:
    """Remote keys -> bare filenames, ignoring anything outside the prefix."""
    return sorted(
        key[len(prefix):] for key in keys
        if key.startswith(prefix) and key != prefix
    )


def local_snapshot_names(snapshot_dir) -> list[str]:
    root = Path(snapshot_dir)
    if not root.exists():
        return []
    return sorted(path.name for path in root.glob(SNAPSHOT_GLOB))


def plan_pull(remote_names, local_names) -> list[str]:
    """Snapshots present remotely and missing locally."""
    return sorted(set(remote_names) - set(local_names))


def plan_push(local_names, remote_names) -> list[str]:
    """Snapshots present locally and missing remotely."""
    return sorted(set(local_names) - set(remote_names))


def state_path(snapshot_dir) -> Path:
    return Path(snapshot_dir) / STATE_FILENAME


def merge_state(local: dict, remote: dict) -> dict:
    """Union of two `ScheduleState.done` maps, `True` winning over absent.

    A point that either side believes is collected *is* collected -- the
    snapshot for it exists somewhere, and re-fetching it would spend a credit
    for a duplicate. `_last_remaining` is the one non-boolean key and takes
    the smaller (more pessimistic) of the two, so the degrade rule in
    `odds_schedule.due_points` never over-estimates the budget left.
    """
    merged = dict(remote)
    for key, value in local.items():
        if key == "_last_remaining":
            continue
        if value:
            merged[key] = True
    remainings = [
        int(source["_last_remaining"]) for source in (local, remote)
        if isinstance(source.get("_last_remaining"), (int, float))
    ]
    if remainings:
        merged["_last_remaining"] = min(remainings)
    return merged


# --- network I/O ---------------------------------------------------------------
def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def pull(
    client: R2Client, *, snapshot_dir, include: str = "all",
    dry_run: bool = False, log=print,
) -> dict:
    """Remote -> local. `include` is `all` / `state` / `snapshots`."""
    root = Path(snapshot_dir)
    result: dict = {"pulled": [], "state": None, "dry_run": dry_run}

    if include in ("all", "state"):
        body = client.get(STATE_KEY)
        if body is None:
            result["state"] = "absent"
            log("state: absent in R2 (first run?)")
        else:
            remote_state = json.loads(body.decode("utf-8"))
            target = state_path(root)
            merged = merge_state(_read_json(target), remote_state)
            if not dry_run:
                root.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8"
                )
            result["state"] = "merged"
            log(f"state: {len(merged)} entries -> {target}")

    if include in ("all", "snapshots"):
        remote = snapshot_names(client.list(SNAPSHOT_PREFIX))
        wanted = plan_pull(remote, local_snapshot_names(root))
        log(f"snapshots: {len(remote)} remote, {len(wanted)} to download")
        for name in wanted:
            if not dry_run:
                body = client.get(snapshot_key(name))
                if body is None:                    # pragma: no cover - race only
                    log(f"  MISSING {name} (listed but gone)")
                    continue
                root.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(body)
            result["pulled"].append(name)
    return result


def push(
    client: R2Client, *, snapshot_dir, include: str = "all",
    dry_run: bool = False, log=print,
) -> dict:
    """Local -> remote. Uploads snapshots R2 has never seen, then the state.

    Order matters: the state is written *last*, so a run that dies half-way
    leaves R2 believing fewer points are done than it really has snapshots
    for. That direction of error costs at most one duplicated API credit; the
    other direction would lose a snapshot's record entirely.
    """
    root = Path(snapshot_dir)
    result: dict = {"pushed": [], "state": None, "dry_run": dry_run}

    if include in ("all", "snapshots"):
        remote = snapshot_names(client.list(SNAPSHOT_PREFIX))
        wanted = plan_push(local_snapshot_names(root), remote)
        log(f"snapshots: {len(wanted)} to upload")
        for name in wanted:
            if not dry_run:
                client.put(snapshot_key(name), (root / name).read_bytes())
            result["pushed"].append(name)

    if include in ("all", "state"):
        local_state = _read_json(state_path(root))
        if not local_state:
            result["state"] = "absent"
            log("state: nothing local to push")
        else:
            remote_body = client.get(STATE_KEY)
            remote_state = json.loads(remote_body.decode("utf-8")) if remote_body else {}
            merged = merge_state(local_state, remote_state)
            if not dry_run:
                client.put(
                    STATE_KEY,
                    json.dumps(merged, indent=2, sort_keys=True).encode("utf-8"),
                )
            result["state"] = "written"
            log(f"state: {len(merged)} entries -> r2://{STATE_KEY}")
    return result


def client_from_env(env=None, *, session=None) -> R2Client | None:
    config = R2Config.from_env(env)
    return None if config is None else R2Client(config, session=session)
