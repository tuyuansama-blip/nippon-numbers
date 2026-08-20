"""Kickoff-anchored odds collection (DESIGN_PHASE2.md 8.4).

The old schedule (`scripts/collect_odds.sh`, 2x/day at fixed clock times)
guarantees only a T-12h grain against a Friday-19:00-JST kickoff -- measured
at T-26.6h/T-50h in the very first snapshot this project took
(DESIGN_PHASE2.md 0.18). This module replaces "call twice a day" with "call
five times per *cluster of kickoffs*, at fixed offsets before that cluster's
earliest kickoff": T-72h, T-24h, T-6h, T-2h, T-25min.

Two things make this cheaper, not more expensive, than the old schedule:

* `GET /v4/.../events` costs 0 credits and returns every upcoming kickoff, so
  clustering can be recomputed every run for free.
* One `/odds` call returns *every* future match regardless of how the
  request was addressed (DESIGN_PHASE2.md 8.4), so hitting a cluster's five
  points covers every match in it -- a Saturday of 7 matches spread across a
  few kickoff times is still one cluster's worth of calls, not seven.

Everything that can be tested without the network lives in pure functions
here (`cluster_kickoffs`, `schedule_points`, `due_points`, the state-file
helpers); `run_schedule` is the only function that touches HTTP, and it is
not exercised by the test suite (DESIGN.md 4: no test may reach the network).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from footy.config import DATA_DIR
from footy.data.download import Fetcher

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
SPORT_KEY = "soccer_japan_j_league"

CLUSTER_WINDOW = pd.Timedelta(minutes=30)
# (label, offset before the cluster anchor). Order matters: the degrade rule
# (8.4) drops from the front (the furthest-out, least urgent points) first.
OFFSETS = (
    ("t72h", pd.Timedelta(hours=72)),
    ("t24h", pd.Timedelta(hours=24)),
    ("t6h", pd.Timedelta(hours=6)),
    ("t2h", pd.Timedelta(hours=2)),
    ("t25min", pd.Timedelta(minutes=25)),
)
LOW_BUDGET_REMAINING = 100        # below this, drop t72h/t24h (8.4's degrade rule)
LOW_BUDGET_DROP_LABELS = ("t72h", "t24h")


@dataclass
class ScheduleState:
    """Which (cluster anchor, offset label) pairs already have a snapshot.

    Persisted as JSON next to the snapshots themselves so a cron restart
    does not re-spend credits re-fetching a point already collected.
    """
    done: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ScheduleState":
        if not path.exists():
            return cls()
        return cls(done=json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.done, indent=2, sort_keys=True), encoding="utf-8")

    def is_done(self, anchor: pd.Timestamp, label: str) -> bool:
        return self.done.get(f"{anchor.isoformat()}|{label}", False)

    def mark_done(self, anchor: pd.Timestamp, label: str) -> None:
        self.done[f"{anchor.isoformat()}|{label}"] = True


def cluster_kickoffs(commence_times, *, window: pd.Timedelta = CLUSTER_WINDOW):
    """Sorted kickoff timestamps -> one anchor (the earliest kickoff) per
    cluster of kickoffs within `window` of each other (DESIGN_PHASE2.md 8.4:
    "30分以内に始まる試合を1つのクラスタにまとめる")."""
    times = sorted(pd.Timestamp(t) for t in commence_times)
    if not times:
        return []
    clusters = [[times[0]]]
    for t in times[1:]:
        if t - clusters[-1][-1] <= window:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [cluster[0] for cluster in clusters]


def schedule_points(anchor: pd.Timestamp, *, offsets=OFFSETS):
    """(label, target_datetime) for one cluster anchor, offsets before it."""
    return [(label, anchor - offset) for label, offset in offsets]


def due_points(anchors, *, now: pd.Timestamp, state: ScheduleState,
                remaining_credits: int | None = None, offsets=OFFSETS):
    """Every (anchor, label, target_time) that is due now and not yet done.

    `remaining_credits` implements the degrade rule (8.4): below
    `LOW_BUDGET_REMAINING`, `t72h`/`t24h` are skipped so the near-kickoff
    points (the ones that actually approximate a close) are protected.
    """
    drop = set()
    if remaining_credits is not None and remaining_credits < LOW_BUDGET_REMAINING:
        drop = set(LOW_BUDGET_DROP_LABELS)

    out = []
    for anchor in anchors:
        for label, target in schedule_points(anchor, offsets=offsets):
            if label in drop:
                continue
            if target > now:
                continue
            if state.is_done(anchor, label):
                continue
            out.append((anchor, label, target))
    return out


# --- network I/O (not covered by tests; DESIGN.md 4) --------------------------
def fetch_events(fetcher: Fetcher, *, sport_key: str = SPORT_KEY) -> list[dict]:
    """`GET /v4/sports/<sport>/events` -- 0 credits."""
    import json as _json

    url = f"{ODDS_API_BASE}/{sport_key}/events?apiKey={fetcher.api_key}"
    body = fetcher.get(url)
    if body is None:
        return []
    return _json.loads(body)


def fetch_odds_snapshot(
    fetcher: Fetcher, *, sport_key: str = SPORT_KEY, regions: str = "eu",
    markets: str = "h2h", odds_format: str = "decimal",
) -> tuple[bytes | None, dict]:
    """`GET /v4/sports/<sport>/odds` -- 1 credit. Returns (body, headers)."""
    url = (
        f"{ODDS_API_BASE}/{sport_key}/odds/?apiKey={fetcher.api_key}"
        f"&regions={regions}&markets={markets}&oddsFormat={odds_format}"
    )
    response = fetcher.session.get(url, timeout=30)
    return (response.content if response.status_code == 200 else None), dict(response.headers)


class ApiKeyFetcher(Fetcher):
    """`Fetcher` plus the one thing the Odds API needs that football-data
    does not: an API key carried alongside every request."""

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key


def run_schedule(
    api_key: str, *, snapshot_dir=None, state_path=None, dry_run: bool = False,
    log=print,
) -> int:
    """One pass: fetch events, cluster, fire every due point. Returns the
    number of snapshots written. `dry_run=True` fetches events (free) but
    never calls `/odds` -- useful for verifying the cluster/due-point logic
    against real upcoming fixtures without spending a credit.
    """
    root = Path(snapshot_dir) if snapshot_dir else DATA_DIR / "odds_snapshots"
    state_file = Path(state_path) if state_path else root / ".schedule_state.json"
    state = ScheduleState.load(state_file)

    fetcher = ApiKeyFetcher(api_key)
    events = fetch_events(fetcher)
    anchors = cluster_kickoffs(e["commence_time"] for e in events)
    now = pd.Timestamp.now(tz="UTC")

    # A first "how many credits are left" pass is not free on its own; the
    # remaining-credits header only appears on a response we already paid
    # for, so the degrade rule reads *last run's* remaining count instead of
    # blocking this run on a probe call.
    last_remaining = state.done.get("_last_remaining")
    points = due_points(anchors, now=now, state=state, remaining_credits=last_remaining)

    log(f"{len(anchors)} clusters, {len(points)} points due")
    if dry_run:
        for anchor, label, target in points:
            log(f"  [dry-run] {label} for cluster {anchor} (target {target})")
        return 0

    written = 0
    for anchor, label, target in points:
        body, headers = fetch_odds_snapshot(fetcher)
        remaining = headers.get("x-requests-remaining")
        if remaining is not None:
            state.done["_last_remaining"] = int(remaining)
        if body is None:
            log(f"  FAILED {label} for {anchor}: no body (remaining={remaining})")
            continue
        ts = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
        out = root / f"j1_h2h_eu_{ts}.json"
        root.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        state.mark_done(anchor, label)
        state.save(state_file)
        written += 1
        log(f"  OK {label} for {anchor} -> {out.name} (remaining={remaining})")
    return written
