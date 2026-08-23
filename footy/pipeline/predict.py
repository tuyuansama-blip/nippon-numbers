"""Next-round J1 predictions and pre-timestamping (DESIGN_PHASE2.md 9's
Thursday `predict` step, and 7.5's timestamp mechanism).

**Where the fixture list comes from.** `new/JPN.csv` (`footy/data/source_fd_new.py`)
only carries rows football-data.co.uk has already scored -- there is no
"future, no result yet" row to read a next-round schedule off of. The Odds
API's `/odds` snapshots do carry unplayed fixtures (they are, definitionally,
odds on matches that have not kicked off), so this module reads the *fixture
list* -- `home_team`/`away_team`/`commence_time` -- out of the cached
snapshots under `data/odds_snapshots/` (read-only, never re-fetched here)
rather than out of the results source. `next_round_fixtures` turns "every
future match the API currently knows about" (DESIGN_PHASE2.md 8.4: one
`/odds` call returns *all* of them, not just the nearest round) into exactly
one round, by walking kickoffs in order and stopping a side's second
appearance -- the same greedy idea as a round-robin draw, run in reverse.

**Where phi comes from.** DESIGN_PHASE2.md 4.2 defines phi_t as the argmin
over *all* history strictly before t, fit on the *honest* walk-forward
sequence of (raw model probability, outcome) pairs -- not on today's model
retrodicting the past, which would leak next-round information backward into
phi. Replaying `run_walkforward(..., calibrate="tb")` over the whole J1 TEST
period is the only way to get that sequence honestly, and it is cheap enough
to do at every `predict` call (measured: ~4s for the full 2014-present
history on this machine) that no separate cached calibration artifact is
needed for `predict` to be correct -- matching 9's own note that the
`calibrate` step and `predict`'s own reproducibility are deliberately
decoupled.

**Timestamping (7.5).** Tier 1+2 (git commit of the prediction file, and of
"what the market said" evidence) is `footy/pipeline/publish.py`. This module
supplies the ingredients that make a committed prediction file *itself* a
falsifiable record: `asof`, `model_version`, `params_hash`, `phi`, and
`odds_snapshot_sha256` all end up in the JSON payload before anything is
written to disk, never appended after the fact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import (
    JPN_DIV,
    JPN_PUBLISH_MIN_TRAIN_MATCHES,
    JPN_TEST_START,
    MODEL_VERSION,
    PREDICTIONS_DIR,
    PREREGISTERED_TAG_PREFIX,
    ROOT,
    jpn_season_label,
    jpn_season_of,
)
from footy.data.check import run_checks_j1
from footy.data.j1_filter import filter_regular_season, regular_season_members
from footy.data.teams_j1 import odds_api_team_id, resolve_odds_api_team
from footy.eval.tune import model_params
from footy.eval.walkforward import run_walkforward
from footy.model.calibrate import apply_tb
from footy.model.dixon_coles import DixonColes

EVENT_COLUMNS = [
    "event_id", "commence_time", "home_raw", "away_raw",
    "home_team", "away_team", "home_id", "away_id",
    "snapshot_file", "snapshot_ts",
]


# --- integrity: params_hash vs. the preregistration tag (5.3-5, 9) -----------
def frozen_params_hash(frozen: dict) -> str:
    """Hash of exactly the constants DESIGN_PHASE2.md 5.3-1 forbids touching
    (xi via half_life_days, sigma, pi, window_seasons, rho_bounds, k_max).
    Anything else in `frozen_params.json` (timings, grid diagnostics) is
    allowed to vary between `tune` runs without tripping this gate."""
    keys = ("half_life_days", "sigma", "pi", "window_seasons", "rho_bounds", "k_max")
    canon = {k: frozen.get(k) for k in keys}
    blob = json.dumps(canon, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _run_git(args, *, cwd) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def preregistered_hash(*, repo_root=None, tag_prefix: str = PREREGISTERED_TAG_PREFIX):
    """`(tag_name, frozen_params_sha256)` read off the most recent
    `phase2-preregistered-*` tag's message, or `(None, None)` if no such tag
    exists yet -- the OOS-LEAGUES/J1 confirmation run (DESIGN_PHASE2.md 5)
    is what creates it, and is a separate, one-off protocol step from this
    weekly pipeline."""
    root = Path(repo_root) if repo_root else ROOT
    listing = _run_git(["tag", "-l", f"{tag_prefix}*"], cwd=root)
    if not listing or not listing.strip():
        return None, None
    latest = sorted(listing.split())[-1]
    message = _run_git(["tag", "-l", "--format=%(contents)", latest], cwd=root) or ""
    for line in message.splitlines():
        line = line.strip()
        if line.startswith("frozen_params_sha256="):
            return latest, line.split("=", 1)[1].strip()
    return latest, None


def check_params_hash(frozen: dict, *, repo_root=None) -> dict:
    """The publish gate's `params_hash` condition (9). Inactive (always
    `ok=True`, with a note saying why) until a preregistration tag exists to
    check against -- there is nothing to have drifted from yet."""
    current = frozen_params_hash(frozen)
    tag, expected = preregistered_hash(repo_root=repo_root)
    if tag is None:
        return {
            "ok": True, "current": current, "tag": None, "expected": None,
            "note": (
                f"no {PREREGISTERED_TAG_PREFIX}* tag found -- gate inactive "
                "until the OOS-LEAGUES/J1 confirmation run (DESIGN_PHASE2.md 5) tags one"
            ),
        }
    if expected is None:
        return {
            "ok": True, "current": current, "tag": tag, "expected": None,
            "note": f"tag {tag} has no frozen_params_sha256= line -- gate inactive",
        }
    ok = current == expected
    return {
        "ok": ok, "current": current, "expected": expected, "tag": tag,
        "note": "matches" if ok else "MISMATCH -- frozen_params.json drifted since preregistration",
    }


# --- fixture list: cached odds snapshots -> the next round (8.2, 8.4, 9) -----
def _snapshot_ts_from_name(path: Path) -> str:
    stem = path.stem
    ts = stem.rsplit("_", 1)[-1]
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[9:11]}:{ts[11:13]}:{ts[13:15]}Z"


def load_snapshot_events(snapshot_dir=None) -> pd.DataFrame:
    """Every cached `j1_h2h_eu_*.json` snapshot -> one row per event (the
    most recently *seen* sighting of that event wins, so a shifted kickoff
    time picks up the latest information). Same team-identity discipline as
    `odds/ingest.py`: an unresolved spelling is a hard error, never a guess.
    """
    from footy.config import DATA_DIR

    root = Path(snapshot_dir) if snapshot_dir else DATA_DIR / "odds_snapshots"
    rows: list[dict] = []
    for path in sorted(root.glob("j1_h2h_eu_*.json")):
        snapshot_ts = _snapshot_ts_from_name(path)
        events = json.loads(path.read_text(encoding="utf-8"))
        for event in events:
            home_name, away_name = event["home_team"], event["away_team"]
            home_canon = resolve_odds_api_team(home_name)
            away_canon = resolve_odds_api_team(away_name)
            if home_canon is None or away_canon is None:
                unknown = [n for n, c in ((home_name, home_canon), (away_name, away_canon))
                           if c is None]
                raise ValueError(
                    f"{path.name}: unresolved J1 team spelling(s) {unknown!r} for "
                    f"event {event['id']} -- add to footy/data/teams_j1.py "
                    "ODDS_API_ALIASES, never guess"
                )
            rows.append({
                "event_id": event["id"],
                "commence_time": pd.Timestamp(event["commence_time"]),
                "home_raw": home_name, "away_raw": away_name,
                "home_team": home_canon, "away_team": away_canon,
                "home_id": odds_api_team_id(home_name),
                "away_id": odds_api_team_id(away_name),
                "snapshot_file": path.name,
                "snapshot_ts": snapshot_ts,
            })
    if not rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    frame = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    return (
        frame.sort_values("snapshot_ts")
        .drop_duplicates("event_id", keep="last")
        .reset_index(drop=True)
    )


def next_round_fixtures(events: pd.DataFrame, *, asof_utc, members=None) -> pd.DataFrame:
    """Greedy earliest-kickoff-first pick: keep an event only if neither side
    has appeared yet. A `/odds` snapshot can hold every future fixture the
    API knows about (DESIGN_PHASE2.md 8.4), not just the nearest round, so
    without this the frame would silently include a second round's matches
    for whichever team's next-next fixture happened to already have odds
    posted. `members`, when given, additionally drops anything that is not
    a current-season J1 side (defensive: `soccer_japan_j_league` is believed
    J1-only but is not documented as such -- DESIGN_PHASE2.md 11.4b)."""
    if events.empty:
        return events
    upcoming = events[events["commence_time"] > pd.Timestamp(asof_utc)]
    upcoming = upcoming.sort_values("commence_time")
    seen: set[str] = set()
    rows = []
    for _, row in upcoming.iterrows():
        home_id, away_id = row["home_id"], row["away_id"]
        if home_id is None or away_id is None:
            continue
        if home_id in seen or away_id in seen:
            continue
        if members is not None and (home_id not in members or away_id not in members):
            continue
        rows.append(row)
        seen.add(home_id)
        seen.add(away_id)
    if not rows:
        return upcoming.iloc[0:0]
    return pd.DataFrame(rows, columns=events.columns).reset_index(drop=True)


def snapshot_digests(paths) -> dict[str, str]:
    return {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}


def combined_snapshot_hash(digests: dict[str, str]) -> str:
    blob = json.dumps(dict(sorted(digests.items())), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --- model + calibration state at asof (4.2, 9) -------------------------------
def compute_prediction_state(matches_history: pd.DataFrame, *, frozen: dict, asof_date):
    """Fit the model on every played match before `asof_date`, and derive
    `phi` by replaying the honest walk-forward from JPN_TEST_START up to
    `asof_date` (see module docstring). Returns
    `(model, phi, phi_state, params, frozen_source)`."""
    params = model_params(frozen)
    source = params.pop("source")
    params["pi"] = tuple(params.get("pi", (0.0, 0.0)))
    # pi_online (DESIGN_PHASE2.md 6.6) is not implemented; this reuses the
    # same documented gap as `_cmd_backtest_v2`'s J1 path in footy/cli.py
    # ("pi": (0.0, 0.0), # pi_online (6.6) not implemented -- see report).

    season_to = jpn_season_of(asof_date)
    wf = run_walkforward(
        matches_history, season_from=JPN_TEST_START, season_to=season_to,
        models=("dc",), refit="week", params=params, calibrate="tb",
    )
    if wf.fits.empty:
        raise ValueError("walk-forward produced no folds -- no J1 history before asof")
    last = wf.fits.iloc[-1]
    phi = np.array([
        float(np.log(last["cal_temperature"])),
        float(last["cal_phi_home"]),
        float(last["cal_phi_draw"]),
    ])
    phi_state = {
        "temperature": float(last["cal_temperature"]),
        "phi_home": float(last["cal_phi_home"]),
        "phi_draw": float(last["cal_phi_draw"]),
        "n_history": int(last["cal_n_history"]) + int(last["n_test"]),
        "warm": bool(last["cal_warm"]),
        "as_of_fold": str(last["fold"]),
    }

    model = DixonColes(
        half_life_days=params["half_life_days"], sigma=params["sigma"],
        pi=params["pi"], window_seasons=params["window_seasons"],
    )
    model.fit(matches_history, asof_date)
    return model, phi, phi_state, params, source


def publish_gate(*, check_result, model_meta, fixtures: pd.DataFrame, params_hash_check: dict) -> dict:
    """The four preregistered conditions (9's "publish gate（予測を出さない条件）")."""
    reasons = []
    if not check_result.ok:
        reasons.append(
            "footy check --league jpn1 is red: " + "; ".join(check_result.problems)
        )
    if model_meta.n_matches < JPN_PUBLISH_MIN_TRAIN_MATCHES:
        reasons.append(
            f"training window has {model_meta.n_matches} matches "
            f"< {JPN_PUBLISH_MIN_TRAIN_MATCHES}"
        )
    unresolved = fixtures[fixtures["home_id"].isna() | fixtures["away_id"].isna()]
    if len(unresolved):
        reasons.append(f"{len(unresolved)} fixture(s) have an unresolved team_id")
    if not params_hash_check["ok"]:
        reasons.append(
            "params_hash does not match the preregistered tag: " + params_hash_check["note"]
        )
    return {"ok": not reasons, "reasons": reasons}


# --- orchestration -------------------------------------------------------------
def generate_prediction(
    *, matches_history: pd.DataFrame, frozen: dict, snapshot_dir=None,
    asof_utc=None, repo_root=None,
) -> dict:
    """The whole `predict` step (9): fixtures -> model+phi -> publish gate ->
    a JSON-ready payload. Returns `{"ok": False, "reason": ...}` when there is
    nothing to predict (no cached fixtures) -- distinct from the publish gate,
    which returns `ok=True` with `payload["publish_gate"]["ok"] = False` so a
    blocked round is still inspectable, just not written by `write_prediction`
    unless the caller overrides that."""
    asof_utc = pd.Timestamp(asof_utc) if asof_utc is not None else pd.Timestamp.now(tz="UTC")
    asof_date = asof_utc.tz_localize(None).normalize()

    events = load_snapshot_events(snapshot_dir)
    if events.empty:
        return {"ok": False, "reason": "no cached odds snapshots under data/odds_snapshots/"}

    j1_history = matches_history[matches_history["div"] == JPN_DIV]
    season = jpn_season_of(asof_date)
    members = regular_season_members(j1_history, season)
    fixtures = next_round_fixtures(events, asof_utc=asof_utc, members=members)
    if fixtures.empty:
        return {"ok": False, "reason": "no upcoming fixtures found in the cached odds snapshots"}

    check_result = run_checks_j1(filter_regular_season(j1_history))
    params_hash_check = check_params_hash(frozen, repo_root=repo_root)
    model, phi, phi_state, params, source = compute_prediction_state(
        j1_history, frozen=frozen, asof_date=asof_date
    )
    raw = model.predict_1x2(fixtures)
    calibrated = apply_tb(raw, phi)

    gate = publish_gate(
        check_result=check_result, model_meta=model.meta,
        fixtures=fixtures, params_hash_check=params_hash_check,
    )

    round_id = fixtures["commence_time"].min().strftime("%Y-%m-%d")
    used_files = sorted(set(fixtures["snapshot_file"]))
    from footy.config import DATA_DIR

    snapshot_root = Path(snapshot_dir) if snapshot_dir else DATA_DIR / "odds_snapshots"
    digests = snapshot_digests([snapshot_root / name for name in used_files])
    combined = combined_snapshot_hash(digests)

    matches_payload = []
    for i, (_, row) in enumerate(fixtures.iterrows()):
        matches_payload.append({
            "event_id": row["event_id"],
            "commence_time": row["commence_time"].isoformat(),
            "home_team": row["home_team"], "away_team": row["away_team"],
            "home_id": row["home_id"], "away_id": row["away_id"],
            "p_raw": {"h": float(raw[i, 0]), "d": float(raw[i, 1]), "a": float(raw[i, 2])},
            "p_calibrated": {
                "h": float(calibrated[i, 0]), "d": float(calibrated[i, 1]), "a": float(calibrated[i, 2])
            },
            "result": None,
        })

    payload = {
        "schema_version": 1,
        "league": "jpn1",
        "season": season,
        "season_label": jpn_season_label(season),
        "round_id": round_id,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "asof": asof_utc.isoformat(),
        "model_version": MODEL_VERSION,
        "params_hash": params_hash_check["current"],
        "params_hash_check": params_hash_check,
        "frozen_params_source": source,
        "phi": phi_state,
        "training_window": {
            "n_matches": model.meta.n_matches,
            "n_teams": model.meta.n_teams,
            "from_season": model.meta.window_from_season,
        },
        "odds_snapshot": {
            "files": used_files, "sha256": digests, "combined_sha256": combined,
        },
        "check": {"ok": check_result.ok, "problems": check_result.problems},
        "publish_gate": gate,
        "matches": matches_payload,
    }
    return {"ok": True, "gate": gate, "payload": payload}


def render_markdown(payload: dict) -> str:
    gate = payload["publish_gate"]
    lines = [
        f"# J1 {payload['season_label']} -- round of {payload['round_id']}",
        "",
        f"- asof (UTC): `{payload['asof']}`",
        f"- generated_at (UTC): `{payload['generated_at']}`",
        f"- model_version: `{payload['model_version']}`",
        f"- params_hash: `{payload['params_hash']}` ({payload['params_hash_check']['note']})",
        (
            f"- calibration phi: temperature={payload['phi']['temperature']:.4f} "
            f"phi_home={payload['phi']['phi_home']:.4f} phi_draw={payload['phi']['phi_draw']:.4f} "
            f"(n_history={payload['phi']['n_history']}, warm={payload['phi']['warm']})"
        ),
        (
            f"- training window: {payload['training_window']['n_matches']} matches, "
            f"{payload['training_window']['n_teams']} teams, from season "
            f"{payload['training_window']['from_season']}"
        ),
        (
            f"- odds snapshot sha256: `{payload['odds_snapshot']['combined_sha256']}` "
            f"({len(payload['odds_snapshot']['files'])} file(s))"
        ),
        (
            "- publish gate: " + ("PASS" if gate["ok"] else "BLOCKED -- " + "; ".join(gate["reasons"]))
        ),
        "",
        "Calibrated probabilities (the layer in DESIGN_PHASE2.md 4):",
        "",
        "| Kickoff (UTC) | Home | Away | P(H) | P(D) | P(A) |",
        "|---|---|---|---|---|---|",
    ]
    for m in payload["matches"]:
        p = m["p_calibrated"]
        lines.append(
            f"| {m['commence_time']} | {m['home_team']} | {m['away_team']} "
            f"| {p['h']:.3f} | {p['d']:.3f} | {p['a']:.3f} |"
        )
    lines += [
        "",
        "Raw (uncalibrated) model probabilities, for reference:",
        "",
        "| Kickoff (UTC) | Home | Away | P(H) | P(D) | P(A) |",
        "|---|---|---|---|---|---|",
    ]
    for m in payload["matches"]:
        p = m["p_raw"]
        lines.append(
            f"| {m['commence_time']} | {m['home_team']} | {m['away_team']} "
            f"| {p['h']:.3f} | {p['d']:.3f} | {p['a']:.3f} |"
        )
    return "\n".join(lines) + "\n"


# --- immutability of an already-published round (DESIGN_SITE.md 3.3) ---------
# DESIGN_SITE.md 3.3 forbids these fields ever changing after a round's first
# commit; only `result` may be filled in later. Locally that rule is enforced
# by a human noticing; under an unattended GitHub Actions schedule
# (docs/DESIGN_ACTIONS.md 6) nothing would notice, and a retried or manually
# re-dispatched Thursday run would quietly rewrite a published prediction
# with a fresh `generated_at` and marginally different probabilities. So the
# check moved into the pipeline itself, ahead of the write.
#
# `asof` and `generated_at` are on 3.3's forbidden-to-change list but are
# deliberately *not* part of the comparison below. They move by construction
# on every re-run (both are "now"), so comparing them would make a harmless
# retry -- the round already computed, committed, and only the `git push`
# having failed -- indistinguishable from a genuine rewrite. What protects
# them is the guard's action rather than its test: an already-published file
# is never rewritten, so the `asof` it was published with is exactly the one
# that stays. A same-day re-run reaches the identical model fit anyway
# (`compute_prediction_state` normalises `asof` to a date), so the
# probabilities below are the honest discriminator between "retry" and
# "someone changed the model".
IMMUTABLE_MATCH_FIELDS = ("event_id", "commence_time", "p_raw", "p_calibrated")
IMMUTABLE_TOP_FIELDS = ("round_id", "season", "model_version", "params_hash")


def immutable_view(payload: dict) -> dict:
    """Exactly the part of a prediction that may never change again."""
    return {
        "top": {key: payload.get(key) for key in IMMUTABLE_TOP_FIELDS},
        "matches": [
            {key: match.get(key) for key in IMMUTABLE_MATCH_FIELDS}
            for match in payload.get("matches", [])
        ],
    }


def existing_conflict(payload: dict, *, predictions_dir=None):
    """`(status, path)` for a round that has already been written.

    `status` is `None` (nothing on disk), `"identical"` (a re-run that would
    be a no-op) or a human-readable description of what would change.
    """
    root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    path = root / f"j1_{payload['season']}_{payload['round_id']}.json"
    if not path.exists():
        return None, path
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return f"{path.name} exists but is unreadable ({exc})", path
    if immutable_view(existing) == immutable_view(payload):
        return "identical", path

    old, new = immutable_view(existing), immutable_view(payload)
    changed = [key for key in IMMUTABLE_TOP_FIELDS if old["top"][key] != new["top"][key]]
    if len(old["matches"]) != len(new["matches"]):
        changed.append(f"fixture count {len(old['matches'])} -> {len(new['matches'])}")
    elif any(a != b for a, b in zip(old["matches"], new["matches"])):
        changed.append("fixture probabilities")
    return (
        f"{path.name} is already published and this run would change "
        + ", ".join(changed or ["an immutable field"])
        + " -- DESIGN_SITE.md 3.3 allows only `result` to be filled in afterwards"
    ), path


def write_prediction(payload: dict, *, predictions_dir=None) -> tuple[Path, Path]:
    root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    root.mkdir(parents=True, exist_ok=True)
    stem = f"j1_{payload['season']}_{payload['round_id']}"
    json_path = root / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    md_path = root / f"{stem}.md"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path
