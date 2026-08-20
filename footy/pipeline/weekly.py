"""`footy weekly` -- the orchestrator table in DESIGN_PHASE2.md 9.

Every step below is a small, independently callable, idempotent function
(9: "すべて冪等・再実行可能...各段は独立にも走る"); `run_weekly` just
sequences the ones due on a given JST weekday and stops early exactly where
9's failure-mode column says to. Nothing here touches the network:
`step_fetch` only re-reads what `footy.data.source_fd_new.fetch_jpn_csv`
already has cached, `step_predict` only reads cached odds snapshots, and
`step_publish`/`step_stamp` default to `dry_run=True`/no-network so calling
`run_weekly` from a test never mutates the real git repository or hits an
OpenTimestamps calendar.

**`stamp` moved from monthly to per-round, at `publish` time.**
DESIGN_PHASE2.md 9 originally put it in a once-a-month batch; DESIGN_SITE.md
3.1 found that flawed (a monthly stamp only lands once every match that
month is already over, so it cannot show a round was fixed *before*
kickoff) and 3.2 replaces it with one stamp per round taken alongside
`publish`. `step_stamp` here follows a round straight out of `step_publish`
rather than being driven off a day-of-month check.

| 曜日(JST) | 段 | この実装 |
|---|---|---|
| 月 09:00 | fetch | `step_fetch` |
| 月 09:10 | reconcile | `step_reconcile` |
| 月 09:20 | calibrate | `step_calibrate` |
| 月 09:30 | report --rolling | `step_report` |
| 木 12:00 | predict | `step_predict` |
| 木 12:05 | publish (+ per-round stamp) | `step_publish`, `step_stamp` |
| 月次 | audit | `step_audit` |

(火-木 の `odds` cron は `footy.pipeline.odds_schedule.run_schedule` -- it
already exists, is network-only by design (DESIGN.md 4), and is deliberately
not re-driven from here. GitHub Actions as the server witness for `predict`
itself, DESIGN_SITE.md 3.2's L1, is the site phase's job, not this
pipeline's.)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from footy.config import DATA_DIR, JPN_DIV, PREDICTIONS_DIR, REPORTS_DIR

WEEKDAY_STEPS = {
    0: ("fetch", "reconcile", "calibrate", "report"),   # Monday
    3: ("predict", "publish", "stamp"),                 # Thursday
}
MONTHLY_STEPS = ("audit",)


def _load_j1_matches(matches_path=None) -> pd.DataFrame:
    path = Path(matches_path) if matches_path else DATA_DIR / "matches_jpn1.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run `footy build --league jpn1` first")
    frame = pd.read_parquet(path)
    if "div" not in frame.columns:
        frame = frame.assign(div=JPN_DIV)
    return frame


# --- Monday ---------------------------------------------------------------
def step_fetch(*, raw_dir=None, out_path=None) -> dict:
    """`JPN.csv` (cache-respecting -- no re-download when already cached) ->
    `build` -> `check`. `check` going red is this step's stop condition (9);
    the caller (`run_weekly`) is the one that acts on `ok=False`."""
    from footy.data.check import format_result, run_checks_j1
    from footy.data.j1_filter import filter_regular_season
    from footy.data.source_fd_new import build_j1_matches

    matches = build_j1_matches(raw_dir)
    filtered = filter_regular_season(matches)
    target = Path(out_path) if out_path else DATA_DIR / "matches_jpn1.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    filtered.to_parquet(target, index=False)

    result = run_checks_j1(filtered)
    return {
        "ok": result.ok, "rows": int(len(filtered)), "path": str(target),
        "problems": result.problems, "report": format_result(result),
    }


def step_reconcile(*, matches_history=None, predictions_dir=None, matches_path=None) -> dict:
    from footy.pipeline.reconcile import reconcile

    matches = matches_history if matches_history is not None else _load_j1_matches(matches_path)
    result = reconcile(matches, predictions_dir=predictions_dir)
    return {"ok": True, **result}


def step_calibrate(*, matches_history=None, frozen=None, matches_path=None, out_path=None) -> dict:
    """Refreshes the phi time series used by `report --rolling` and by CL-3
    (DESIGN_PHASE2.md 4.4) monitoring. `predict` does **not** depend on this
    step's output -- it always re-derives phi itself (see
    `footy/pipeline/predict.py`'s module docstring) -- so a failure here
    never blocks Thursday's prediction, exactly as 9 specifies ("phi 更新失
    敗なら前回 phi を継続使用")."""
    from footy.eval.tune import load_frozen_params
    from footy.pipeline.predict import compute_prediction_state

    matches = matches_history if matches_history is not None else _load_j1_matches(matches_path)
    frozen = frozen if frozen is not None else load_frozen_params()
    if not frozen:
        return {"ok": False, "reason": "data/frozen_params.json not found -- run `footy tune` first"}

    try:
        _, _, phi_state, _, _ = compute_prediction_state(
            matches, frozen=frozen, asof_date=pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
        )
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    target = Path(out_path) if out_path else REPORTS_DIR / "j1_phi_latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    import json

    target.write_text(json.dumps(phi_state, indent=2), encoding="utf-8")
    return {"ok": True, "phi": phi_state, "path": str(target)}


def step_report(*, predictions_dir=None, reports_dir=None) -> dict:
    """`report --rolling` (9): a calibration snapshot of the FORWARD track
    record (DESIGN_PHASE2.md 7.1 -- never pooled with the backtest). Lighter
    than the full Murphy-decomposition report `eval/report.py` produces for
    the OOS-LEAGUES/J1 confirmation run, on purpose: this runs weekly against
    however many forward matches have been reconciled so far, which for most
    of the season is far too few for that report's own bin-size floors."""
    from footy.eval.metrics import own_decile_table
    from footy.pipeline.reconcile import build_track_record

    pred_root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    track = build_track_record(pred_root)
    out_root = Path(reports_dir) if reports_dir else REPORTS_DIR
    out_root.mkdir(parents=True, exist_ok=True)
    out_path = out_root / "j1_forward_rolling.md"

    if track.empty:
        out_path.write_text(
            "# J1 forward track record\n\nNo reconciled forward matches yet.\n",
            encoding="utf-8",
        )
        return {"ok": True, "n_matches": 0, "path": str(out_path)}

    p_cal = track[["p_cal_h", "p_cal_d", "p_cal_a"]].to_numpy(dtype="float64")
    decile = own_decile_table(p_cal, track["y"].to_numpy())

    lines = [
        "# J1 forward track record",
        "",
        f"- matches reconciled: {len(track)}",
        f"- rounds: {track['round_id'].nunique()}",
        f"- mean RPS (raw): {track['rps_raw'].mean():.5f}",
        f"- mean RPS (calibrated): {track['rps_cal'].mean():.5f}",
        f"- mean log loss (raw): {track['ll_raw'].mean():.5f}",
        f"- mean log loss (calibrated): {track['ll_cal'].mean():.5f}",
        "",
        "## Own-probability-decile calibration (calibrated model, CAL-2 shape)",
        "",
        "| decile | n | model_mean | observed | gap |",
        "|---|---|---|---|---|",
    ]
    for _, row in decile.iterrows():
        lines.append(
            f"| {int(row['decile'])} | {int(row['n'])} | {row['model_mean']:.4f} "
            f"| {row['observed']:.4f} | {row['gap']:+.4f} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "n_matches": int(len(track)), "path": str(out_path)}


# --- Thursday ---------------------------------------------------------------
def step_predict(*, matches_history=None, frozen=None, snapshot_dir=None,
                  matches_path=None, predictions_dir=None, asof_utc=None) -> dict:
    """The publish gate (DESIGN_PHASE2.md 9) is enforced here, not just in
    `publish`: a blocked round is never written to `predictions/`."""
    from footy.eval.tune import load_frozen_params
    from footy.pipeline.predict import generate_prediction, write_prediction

    matches = matches_history if matches_history is not None else _load_j1_matches(matches_path)
    frozen = frozen if frozen is not None else load_frozen_params()
    if not frozen:
        return {"ok": False, "reason": "data/frozen_params.json not found -- run `footy tune` first"}

    result = generate_prediction(
        matches_history=matches, frozen=frozen, snapshot_dir=snapshot_dir, asof_utc=asof_utc,
    )
    if not result["ok"]:
        return result

    payload = result["payload"]
    if not payload["publish_gate"]["ok"]:
        return {
            "ok": False, "gate": payload["publish_gate"], "payload": payload,
            "reason": "publish gate blocked -- no prediction written",
        }

    json_path, md_path = write_prediction(payload, predictions_dir=predictions_dir)
    return {"ok": True, "json_path": str(json_path), "md_path": str(md_path), "payload": payload}


def step_publish(*, predict_result: dict, dry_run: bool = True, repo_root=None) -> dict:
    from footy.pipeline.publish import publish, round_tag

    if not predict_result.get("ok") or "json_path" not in predict_result:
        return {"ok": True, "committed": False, "note": "nothing to publish"}

    paths = [predict_result["json_path"], predict_result["md_path"]]
    tag = round_tag(predict_result["payload"])
    return publish(paths, repo_root=repo_root, tag=tag, dry_run=dry_run)


# --- per-round (fired from Thursday, right after publish) --------------------
def step_stamp(*, predict_result: dict) -> dict:
    """Per-round L2 stamp (DESIGN_SITE.md 3.2), fired for the round that was
    just published rather than on a monthly clock -- see this module's
    docstring for why 9's original monthly `stamp` was replaced. A failure
    here is a warning, never a block (9: "失敗は警告のみ")."""
    from footy.pipeline.stamp import stamp_round

    if not predict_result.get("ok") or "json_path" not in predict_result:
        return {"ok": True, "stamped": False, "note": "nothing to stamp"}
    try:
        payload = stamp_round(predict_result["json_path"])
        return {"ok": True, **payload}
    except Exception as exc:                     # 9: "失敗は警告のみ"
        return {"ok": False, "reason": str(exc)}


# --- monthly ------------------------------------------------------------------
def step_audit(*, frozen=None, matches_history=None, repo_root=None) -> dict:
    """Light-weight audit: params_hash-vs-preregistration-tag and a phi
    sanity check. **Not** a re-evaluation of CAL-1..CAL-4 -- those require
    the OOS-LEAGUES/J1 confirmation run (DESIGN_PHASE2.md 5), a one-off
    protocol step outside this weekly pipeline's scope, not something a
    Monday cron can honestly redo on 1/52 of a season's worth of new data."""
    from footy.eval.tune import load_frozen_params
    from footy.pipeline.predict import check_params_hash

    frozen = frozen if frozen is not None else load_frozen_params()
    if not frozen:
        return {"ok": False, "reason": "data/frozen_params.json not found"}
    hash_check = check_params_hash(frozen, repo_root=repo_root)
    return {
        "ok": hash_check["ok"], "params_hash_check": hash_check,
        "note": (
            "CAL-1..CAL-4 are not re-evaluated here -- see DESIGN_PHASE2.md 5; "
            "this audit only confirms frozen_params.json has not drifted."
        ),
    }


# --- orchestration --------------------------------------------------------
STEP_FUNCS = {
    "fetch": step_fetch, "reconcile": step_reconcile, "calibrate": step_calibrate,
    "report": step_report, "predict": step_predict, "publish": step_publish,
    "stamp": step_stamp, "audit": step_audit,
}


def run_weekly(
    *, steps=None, weekday: int | None = None, day_of_month: int | None = None,
    dry_run_publish: bool = True, repo_root=None, snapshot_dir=None,
    matches_path=None, predictions_dir=None, raw_dir=None,
    frozen=None, matches_history=None, asof_utc=None,
) -> dict:
    """Run whichever steps are due. `steps`, if given, overrides the
    weekday/day-of-month gating entirely (used by `footy weekly --steps ...`
    for a manual/backfill run of a specific stage). Stops after `fetch` iff
    it comes back red (9's stated behaviour for that step).

    `frozen`/`matches_history`, when given, are threaded straight through to
    every step instead of each step re-reading `data/frozen_params.json` /
    `data/matches_jpn1.parquet` -- production never passes these (each step
    is meant to read the current on-disk state), but a test driving the
    whole orchestrator on synthetic data needs to inject both rather than
    touch the real repository's files."""
    if steps is None:
        now = pd.Timestamp.now(tz="Asia/Tokyo")
        weekday = now.weekday() if weekday is None else weekday
        day_of_month = now.day if day_of_month is None else day_of_month
        steps = list(WEEKDAY_STEPS.get(weekday, ()))
        if day_of_month == 1:
            steps += list(MONTHLY_STEPS)

    report: dict[str, dict] = {}
    predict_result = None
    for step in steps:
        if step == "fetch":
            report[step] = step_fetch(raw_dir=raw_dir, out_path=matches_path)
            if not report[step]["ok"]:
                report["stopped_after"] = "fetch"
                break
        elif step == "reconcile":
            report[step] = step_reconcile(
                matches_history=matches_history, matches_path=matches_path,
                predictions_dir=predictions_dir,
            )
        elif step == "calibrate":
            report[step] = step_calibrate(matches_history=matches_history, frozen=frozen, matches_path=matches_path)
        elif step == "report":
            report[step] = step_report(predictions_dir=predictions_dir)
        elif step == "predict":
            predict_result = step_predict(
                matches_history=matches_history, frozen=frozen, matches_path=matches_path,
                predictions_dir=predictions_dir, snapshot_dir=snapshot_dir, asof_utc=asof_utc,
            )
            report[step] = predict_result
        elif step == "publish":
            report[step] = step_publish(
                predict_result=predict_result or {"ok": False},
                dry_run=dry_run_publish, repo_root=repo_root,
            )
        elif step == "stamp":
            report[step] = step_stamp(predict_result=predict_result or {"ok": False})
        elif step == "audit":
            report[step] = step_audit(frozen=frozen, repo_root=repo_root)
        else:
            report[step] = {"ok": False, "reason": f"unknown step {step!r}"}

    report["steps_run"] = list(steps if "stopped_after" not in report else steps[: steps.index("fetch") + 1])
    return report
