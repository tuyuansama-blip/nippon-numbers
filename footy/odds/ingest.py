"""Snapshot JSON -> `data/odds_quotes.parquet` (DESIGN_PHASE2.md 8.2).

The raw snapshots (`data/odds_snapshots/j1_h2h_eu_<UTC>.json`) are immutable
and append-only; this module is the only thing allowed to read them, and it
never mutates or deletes one. `data/odds_quotes.parquet` is the long-form,
idempotent result: re-running `ingest` on the same snapshots, in any order,
any number of times, produces the same parquet, because rows are deduplicated
on `(event_id, book_key, book_last_update)`.

Two rules carry the whole module (DESIGN_PHASE2.md 8.2-1, 8.2-2):

* **Outcomes are matched by name, never by array index.** The Odds API does
  not guarantee `outcomes[]` order; a live snapshot in this project's own
  `data/odds_snapshots/` already shows an away side listed first
  (DESIGN_PHASE2.md 0.19). An outcome whose name is not the event's own
  `home_team`, `away_team` or `"Draw"` gets the *whole match* dropped, with a
  warning -- never silently skipped.
* **Team identity is the explicit crosswalk (`teams_j1.py`) only.** An
  unresolved spelling is a hard error, not a best-effort guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from footy.data.teams_j1 import odds_api_team_id, resolve_odds_api_team

QUOTES_COLUMNS = [
    "event_id", "snapshot_ts", "commence_time", "book_key",
    "outcome", "price", "book_last_update",
    "home_raw", "away_raw", "home_id", "away_id", "staleness_sec",
]


def _snapshot_ts_from_name(path: Path) -> str:
    """`j1_h2h_eu_20260820T072241Z.json` -> `2026-08-20T07:22:41Z`."""
    stem = path.stem
    ts = stem.rsplit("_", 1)[-1]
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z"


def parse_snapshot(path: Path | str, *, warn=None) -> list[dict]:
    """One raw snapshot JSON -> a list of quote rows (H/D/A, per bookmaker).

    An event whose outcome names cannot all be matched to `home_team`,
    `away_team` or `"Draw"` is dropped whole and reported through `warn`
    (defaults to a no-op) rather than silently skipped (DESIGN_PHASE2.md
    8.2-1). An unresolved team spelling raises -- resolving spellings is
    `teams_j1`'s job and it is never allowed to guess.
    """
    path = Path(path)
    warn = warn or (lambda message: None)
    snapshot_ts = _snapshot_ts_from_name(path)
    events = json.loads(path.read_text(encoding="utf-8"))

    home_name = None
    rows: list[dict] = []
    for event in events:
        event_id = event["id"]
        commence_time = pd.Timestamp(event["commence_time"])
        home_name = event["home_team"]
        away_name = event["away_team"]

        home_canon = resolve_odds_api_team(home_name)
        away_canon = resolve_odds_api_team(away_name)
        if home_canon is None or away_canon is None:
            unknown = [n for n, c in ((home_name, home_canon), (away_name, away_canon))
                       if c is None]
            raise ValueError(
                f"{path.name}: unresolved J1 team spelling(s) {unknown!r} for "
                f"event {event_id} -- add to footy/data/teams_j1.py "
                "ODDS_API_ALIASES, never guess"
            )
        home_id = odds_api_team_id(home_name)
        away_id = odds_api_team_id(away_name)

        for book in event.get("bookmakers", []):
            book_key = book["key"]
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue                       # h2h_lay etc. are not close prices
                last_update = pd.Timestamp(market["last_update"])
                event_rows = []
                bad = False
                for outcome in market.get("outcomes", []):
                    name = outcome["name"]
                    price = outcome["price"]
                    if name == home_name:
                        code = "H"
                    elif name == away_name:
                        code = "A"
                    elif name == "Draw":
                        code = "D"
                    else:
                        bad = True
                        warn(
                            f"{path.name}: event {event_id} ({home_name} v "
                            f"{away_name}) book {book_key} has an outcome "
                            f"name {name!r} matching neither side nor "
                            "'Draw' -- dropping the whole match"
                        )
                        break
                    staleness = (commence_time - last_update).total_seconds()
                    event_rows.append({
                        "event_id": event_id, "snapshot_ts": snapshot_ts,
                        "commence_time": commence_time, "book_key": book_key,
                        "outcome": code, "price": float(price),
                        "book_last_update": last_update,
                        "home_raw": home_name, "away_raw": away_name,
                        "home_id": home_id, "away_id": away_id,
                        "staleness_sec": staleness,
                    })
                if bad:
                    continue
                rows.extend(event_rows)
    return rows


def ingest(
    snapshot_dir=None, *, out_path=None, warn=print
) -> pd.DataFrame:
    """Every cached snapshot -> `data/odds_quotes.parquet`, merged
    idempotently with whatever was already there."""
    from footy.config import DATA_DIR

    root = Path(snapshot_dir) if snapshot_dir else DATA_DIR / "odds_snapshots"
    target = Path(out_path) if out_path else DATA_DIR / "odds_quotes.parquet"

    rows: list[dict] = []
    for path in sorted(root.glob("j1_h2h_eu_*.json")):
        rows.extend(parse_snapshot(path, warn=warn))
    new = pd.DataFrame(rows, columns=QUOTES_COLUMNS)

    if target.exists():
        existing = pd.read_parquet(target)
        combined = pd.concat([existing, new], ignore_index=True)
    else:
        combined = new
    key = ["event_id", "book_key", "book_last_update", "outcome"]
    combined = combined.drop_duplicates(key, keep="last").sort_values(
        ["commence_time", "event_id", "book_key", "outcome"]
    ).reset_index(drop=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(target, index=False)
    return combined
