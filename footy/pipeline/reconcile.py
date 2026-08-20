"""Result reconciliation and the cumulative track record (DESIGN_PHASE2.md
9's Monday `reconcile` step).

Per-match, not per-round: a round predicted on Thursday plays out across
Friday-Monday (sometimes longer, with a postponement), so "the round's
results" arrives incrementally. `reconcile_file` fills in whichever
individual matches now have a confirmed score in `matches_history` and
leaves the rest `result: null` for tomorrow's retry (9's stated failure
mode: "未確定試合はスキップし翌日再試行") -- it never fails or blocks on a
still-unplayed fixture. The prediction JSON is *appended to*, never replaced:
`round_summary` is recomputed from whatever is resolved so far, and every
field written at predict time (7.5's `asof`/`model_version`/`params_hash`/
`phi`/`odds_snapshot_sha256`) is left untouched.

`build_track_record` rebuilds the flat table from every prediction file's
resolved matches on each run rather than appending incrementally, so a
partially-reconciled file, a rerun, or a manually-edited prediction can never
leave the track record out of sync with what the JSON files actually say.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import PREDICTIONS_DIR
from footy.eval.metrics import encode_outcome, logloss_array, rps_array

RESULT_MATCH_WINDOW_DAYS = 4   # commence_time (Odds API) vs. JPN.csv `date` can drift by a day

TRACK_RECORD_COLUMNS = [
    "round_id", "season", "commence_time", "home_team", "away_team",
    "p_raw_h", "p_raw_d", "p_raw_a", "p_cal_h", "p_cal_d", "p_cal_a",
    "y", "ftr", "rps_raw", "rps_cal", "ll_raw", "ll_cal",
    "model_version", "params_hash",
]


def _actual_results(matches_history: pd.DataFrame) -> pd.DataFrame:
    played = matches_history[matches_history["fthg"].notna() & matches_history["ftag"].notna()]
    return played[["date", "home_id", "away_id", "fthg", "ftag", "ftr"]]


def _find_result(actual: pd.DataFrame, home_id, away_id, commence_time) -> dict | None:
    if not home_id or not away_id:
        return None
    commence_date = pd.Timestamp(commence_time)
    if commence_date.tzinfo is not None:
        commence_date = commence_date.tz_convert("UTC").tz_localize(None)
    commence_date = commence_date.normalize()
    candidates = actual[(actual["home_id"] == home_id) & (actual["away_id"] == away_id)]
    if candidates.empty:
        return None
    delta = (candidates["date"] - commence_date).abs()
    candidates = candidates[delta <= pd.Timedelta(days=RESULT_MATCH_WINDOW_DAYS)]
    if candidates.empty:
        return None
    row = candidates.loc[delta.reindex(candidates.index).idxmin()]
    return {"fthg": float(row["fthg"]), "ftag": float(row["ftag"]), "ftr": str(row["ftr"])}


def reconcile_file(path: Path, actual: pd.DataFrame) -> tuple[dict, bool]:
    """One `predictions/j1_*.json` -> `(updated payload, changed?)`. Never
    touches a match that already has a `result`."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for match in payload["matches"]:
        if match.get("result") is not None:
            continue
        found = _find_result(actual, match.get("home_id"), match.get("away_id"), match["commence_time"])
        if found is None:
            continue
        y = int(encode_outcome([found["ftr"]])[0])
        p_raw = np.array([[match["p_raw"]["h"], match["p_raw"]["d"], match["p_raw"]["a"]]])
        p_cal = np.array([[match["p_calibrated"]["h"], match["p_calibrated"]["d"], match["p_calibrated"]["a"]]])
        match["result"] = {
            **found,
            "y": y,
            "rps_raw": float(rps_array(p_raw, [y])[0]),
            "rps_cal": float(rps_array(p_cal, [y])[0]),
            "ll_raw": float(logloss_array(p_raw, [y])[0]),
            "ll_cal": float(logloss_array(p_cal, [y])[0]),
        }
        changed = True

    resolved = [m["result"] for m in payload["matches"] if m.get("result") is not None]
    payload["round_summary"] = {
        "n_matches": len(payload["matches"]),
        "n_resolved": len(resolved),
        "complete": len(resolved) == len(payload["matches"]),
        "mean_rps_raw": float(np.mean([r["rps_raw"] for r in resolved])) if resolved else None,
        "mean_rps_cal": float(np.mean([r["rps_cal"] for r in resolved])) if resolved else None,
        "mean_ll_raw": float(np.mean([r["ll_raw"] for r in resolved])) if resolved else None,
        "mean_ll_cal": float(np.mean([r["ll_cal"] for r in resolved])) if resolved else None,
    }
    return payload, changed


def build_track_record(predictions_dir=None) -> pd.DataFrame:
    root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    rows = []
    for path in sorted(root.glob("j1_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for match in payload.get("matches", []):
            result = match.get("result")
            if result is None:
                continue
            rows.append({
                "round_id": payload["round_id"], "season": payload["season"],
                "commence_time": match["commence_time"],
                "home_team": match["home_team"], "away_team": match["away_team"],
                "p_raw_h": match["p_raw"]["h"], "p_raw_d": match["p_raw"]["d"],
                "p_raw_a": match["p_raw"]["a"],
                "p_cal_h": match["p_calibrated"]["h"], "p_cal_d": match["p_calibrated"]["d"],
                "p_cal_a": match["p_calibrated"]["a"],
                "y": result["y"], "ftr": result["ftr"],
                "rps_raw": result["rps_raw"], "rps_cal": result["rps_cal"],
                "ll_raw": result["ll_raw"], "ll_cal": result["ll_cal"],
                "model_version": payload.get("model_version"),
                "params_hash": payload.get("params_hash"),
            })
    if not rows:
        return pd.DataFrame(columns=TRACK_RECORD_COLUMNS)
    return (
        pd.DataFrame(rows, columns=TRACK_RECORD_COLUMNS)
        .sort_values("commence_time")
        .reset_index(drop=True)
    )


def reconcile(matches_history: pd.DataFrame, *, predictions_dir=None) -> dict:
    """The whole `reconcile` step: fill in whatever results are now known,
    rewrite only the files that changed, and rebuild the track record."""
    root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    if not root.exists():
        return {"updated_files": [], "track_record_rows": 0, "track_record_path": None}

    actual = _actual_results(matches_history)
    updated = []
    for path in sorted(root.glob("j1_*.json")):
        payload, changed = reconcile_file(path, actual)
        if changed:
            path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
            updated.append(path.name)

    track = build_track_record(root)
    track_path = root / "track_record.csv"
    track.to_csv(track_path, index=False)
    return {
        "updated_files": updated,
        "track_record_rows": int(len(track)),
        "track_record_path": str(track_path),
    }
