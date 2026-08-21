"""The Odds API `/scores` endpoint -- the *provisional* half of the
two-stage results pipeline (the confirmed half is
`footy.pipeline.reconcile`).

football-data.co.uk's `new/JPN.csv` lags kickoff by a day or two (it is a
scraped, batch-published file, not a live feed), so the public site's
Result column would sit on "not yet played" until well after the match is
over. The Odds API's `/scores` endpoint (`?daysFrom=N`, 2 credits/call --
DESIGN_PHASE2.md 8's cost discipline applies here too) knows a completed
score within minutes, so this module fills the same `result` field with a
same-night guess, tagged `source: "odds_api_provisional"` so
`footy.pipeline.reconcile` knows it is allowed to overwrite it once
football-data's confirmed score arrives (see that module's docstring for
the full two-stage contract, including how a disagreement is recorded
rather than hidden).

**Matching, in order:** the event id `predict.py` already copied from the
`/odds` snapshot into the prediction file is the same id space `/scores`
uses for the same sport, so it is tried first and is exact. The fallback --
needed only if a fixture's `/odds` snapshot event id and its `/scores` id
ever disagree, or a file predates event ids -- resolves `/scores`' own
`home_team`/`away_team` spellings through `footy/data/teams_j1.py`'s
explicit alias table (never a fuzzy match, same discipline as
`footy/odds/ingest.py`) and matches on canonical name plus the same
date window `reconcile.py` already uses for football-data. An unresolved
spelling in that fallback is a warning and a skip, never a hard failure --
unlike `ingest.py`'s pricing path, a provisional result is a convenience,
not something the site's honesty depends on (football-data always confirms
it eventually).

Everything below `fetch_scores` touches the network and is excluded from
the test suite (DESIGN.md 4); every other function here is pure and is
what the tests exercise, fed a hand-built `/scores` response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from footy.config import PREDICTIONS_DIR
from footy.data.teams_j1 import resolve_odds_api_team
from footy.pipeline.odds_schedule import ODDS_API_BASE, SPORT_KEY
from footy.pipeline.reconcile import (
    RESULT_MATCH_WINDOW_DAYS,
    _is_confirmed,
    recompute_round_summary,
    score_result,
)

PROVISIONAL_SOURCE = "odds_api_provisional"


def _ftr_from_scores(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "H"
    if home_score < away_score:
        return "A"
    return "D"


def parse_score_event(event: dict) -> dict | None:
    """One raw `/scores` element -> `{event_id, commence_time, home_team,
    away_team, home_score, away_score}`, or `None` for anything not a
    finished, numerically-scored match: not yet `completed`, no `scores`
    list (in-play matches on this endpoint have that as `None`), or a score
    that isn't a plain integer."""
    if not event.get("completed"):
        return None
    raw_scores = event.get("scores")
    if not raw_scores:
        return None
    home_name, away_name = event.get("home_team"), event.get("away_team")
    by_name: dict[str, int] = {}
    for entry in raw_scores:
        try:
            by_name[entry["name"]] = int(entry["score"])
        except (KeyError, TypeError, ValueError):
            return None
    if home_name not in by_name or away_name not in by_name:
        return None
    return {
        "event_id": event.get("id"),
        "commence_time": event.get("commence_time"),
        "home_team": home_name, "away_team": away_name,
        "home_score": by_name[home_name], "away_score": by_name[away_name],
    }


def _normalize_utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def _resolve_events(events: list[dict], *, warn) -> list[dict]:
    """Parsed `/scores` events, each stamped with its canonical J1 team
    names -- or dropped (with `warn`) if either spelling is unresolved.
    De-duplicates warnings per raw name so one unknown club doesn't spam
    one line per fixture it appears in."""
    resolved = []
    warned: set[str] = set()
    for event in events:
        home_canon = resolve_odds_api_team(event["home_team"])
        away_canon = resolve_odds_api_team(event["away_team"])
        unknown = [n for n, c in ((event["home_team"], home_canon), (event["away_team"], away_canon))
                   if c is None]
        if unknown:
            for name in unknown:
                if name not in warned:
                    warned.add(name)
                    warn(
                        f"unresolved J1 team spelling {name!r} (scores event "
                        f"{event.get('event_id')}) -- add to footy/data/teams_j1.py "
                        "ODDS_API_ALIASES; skipping this fixture"
                    )
            continue
        resolved.append({**event, "home_canon": home_canon, "away_canon": away_canon})
    return resolved


def _find_provisional(match: dict, resolved_events: list[dict]) -> dict | None:
    """Event id first (exact, same id space as the `/odds` snapshot
    `predict.py` copied it from); team name + date window as the fallback."""
    for event in resolved_events:
        if event["event_id"] and event["event_id"] == match.get("event_id"):
            return event
    commence = _normalize_utc(match["commence_time"])
    window = pd.Timedelta(days=RESULT_MATCH_WINDOW_DAYS)
    for event in resolved_events:
        if event["home_canon"] != match["home_team"] or event["away_canon"] != match["away_team"]:
            continue
        if abs(_normalize_utc(event["commence_time"]) - commence) <= window:
            return event
    return None


def apply_provisional_results(
    payload: dict, score_events: list[dict], *, now=None, warn=None,
) -> tuple[dict, bool, list[str]]:
    """One prediction payload -> `(updated payload, changed?, warnings)`.
    `score_events` is the raw `/scores` list (not yet parsed) so a caller
    fetching once and applying it to every file in `predictions/` doesn't
    have to parse it repeatedly. Never touches an already-confirmed match
    (`reconcile._is_confirmed`); a match already carrying a provisional
    result is re-scored in place (the same-night score can still change
    between calls, e.g. half-time -> full-time)."""
    warnings: list[str] = []

    def _warn(message: str) -> None:
        warnings.append(message)
        if warn:
            warn(message)

    parsed = [e for e in (parse_score_event(raw) for raw in score_events) if e is not None]
    resolved_events = _resolve_events(parsed, warn=_warn)
    fetched_at = pd.Timestamp(now).isoformat() if now is not None else pd.Timestamp.now(tz="UTC").isoformat()

    changed = False
    for match in payload["matches"]:
        if _is_confirmed(match.get("result")):
            continue
        found = _find_provisional(match, resolved_events)
        if found is None:
            continue
        ftr = _ftr_from_scores(found["home_score"], found["away_score"])
        result = score_result(match, found["home_score"], found["away_score"], ftr)
        result["source"] = PROVISIONAL_SOURCE
        result["fetched_at"] = fetched_at
        match["result"] = result
        changed = True

    payload["round_summary"] = recompute_round_summary(payload)
    return payload, changed, warnings


def apply_provisional_to_dir(
    score_events: list[dict], *, predictions_dir=None, now=None, warn=None,
) -> dict:
    """`footy odds scores`'s whole job: apply one `/scores` response to
    every `predictions/j1_*.json` file, writing back only the ones that
    changed -- mirrors `reconcile.reconcile`'s shape so `footy weekly`'s
    printed step summary reads the same way for both stages."""
    root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    if not root.exists():
        return {"updated_files": [], "warnings": []}

    updated = []
    all_warnings: list[str] = []
    for path in sorted(root.glob("j1_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if "matches" not in payload:
            continue
        payload, changed, warnings = apply_provisional_results(payload, score_events, now=now, warn=warn)
        all_warnings.extend(warnings)
        if changed:
            path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
            updated.append(path.name)

    return {"updated_files": updated, "warnings": all_warnings}


# --- network I/O (not covered by tests; DESIGN.md 4) --------------------------
def fetch_scores(fetcher, *, sport_key: str = SPORT_KEY, days_from: int = 3) -> list[dict]:
    """`GET /v4/sports/<sport>/scores/?daysFrom=N` -- 2 credits. `fetcher`
    is an `odds_schedule.ApiKeyFetcher` (or anything with the same
    `.api_key` and `.get`)."""
    url = f"{ODDS_API_BASE}/{sport_key}/scores/?daysFrom={days_from}&apiKey={fetcher.api_key}"
    body = fetcher.get(url)
    if body is None:
        return []
    return json.loads(body)
