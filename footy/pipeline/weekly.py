"""`footy weekly` -- the orchestrator table in DESIGN_PHASE2.md 9.

Every step below is a small, independently callable, idempotent function
(9: "すべて冪等・再実行可能...各段は独立にも走る"); `run_weekly` just
sequences the ones due on a given JST weekday and stops early exactly where
9's failure-mode column says to. Nothing here touches the network *except*
`step_scores` (see below): `step_fetch` only re-reads what
`footy.data.source_fd_new.fetch_jpn_csv` already has cached, `step_predict`
only reads cached odds snapshots, and `step_publish`/`step_stamp` default to
`dry_run=True`/no-network so calling `run_weekly` from a test never mutates
the real git repository or hits an OpenTimestamps calendar. `step_scores`
only reaches the network when its caller doesn't already hand it
`score_events` -- exactly how the test suite drives it without violating
DESIGN.md 4.

**`stamp` moved from monthly to per-round, at `publish` time.**
DESIGN_PHASE2.md 9 originally put it in a once-a-month batch; DESIGN_SITE.md
3.1 found that flawed (a monthly stamp only lands once every match that
month is already over, so it cannot show a round was fixed *before*
kickoff) and 3.2 replaces it with one stamp per round taken alongside
`publish`. `step_stamp` here follows a round straight out of `step_publish`
rather than being driven off a day-of-month check.

**`stamp` now runs *before* `publish`, not after** (docs/DESIGN_ACTIONS.md
5). DESIGN_SITE.md 3.2-L2 requires the `.ots` to land "同じ commit に"
含める -- in the same commit as the prediction it attests. Stamping after
the commit cannot satisfy that: the artefact would always be one commit
late, and a reader checking whether the attestation predates kickoff would
be looking at a different commit than the one holding the prediction. So
`step_stamp` fires on `predict`'s output first, and `step_publish` takes the
stamp's artefacts as extra paths to stage.

**`scores` is the one step here that touches the network.** Every other
step below is cache-only, as the paragraph after this table says. `scores`
(Fri/Sat/Sun nights, when this round's matches are actually being played) is
the exception: it calls The Odds API's `/scores` endpoint
(`footy/odds/scores.py`, 2 credits/call) to write same-night *provisional*
results, ahead of football-data.co.uk's confirmed ones that Monday's
`reconcile` eventually overwrites them with. A missing `ODDS_API_KEY`, or
the call itself failing, is a warning (`ok=True`, a `note`), never a stop
-- a quiet night for this step just means the site shows "not yet played"
a little longer, never a wrong result, since Monday's `reconcile` is still
the one source of truth.

| 曜日(JST) | 段 | この実装 |
|---|---|---|
| 月 09:00 | fetch | `step_fetch` |
| 月 09:10 | reconcile | `step_reconcile` |
| 月 09:20 | calibrate | `step_calibrate` |
| 月 09:30 | report --rolling | `step_report` |
| 木 12:00 | predict | `step_predict` |
| 木 12:05 | stamp, then publish | `step_stamp`, `step_publish` |
| 金-日 夜 | scores (暫定結果) | `step_scores` |
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
    3: ("predict", "stamp", "publish"),                 # Thursday
    4: ("scores",),                                     # Friday night
    5: ("scores",),                                     # Saturday night
    6: ("scores",),                                     # Sunday night
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
    """Confirms whatever football-data now has scored, including upgrading
    any same-night `scores` provisional result to confirmed. A confirmation
    that disagrees with its provisional guess is never hidden: it is kept in
    the prediction file as `result.discrepancy` (`reconcile.py`'s module
    docstring) and surfaced here as `note` so it shows up in `footy weekly`'s
    printed step summary too."""
    from footy.pipeline.reconcile import reconcile

    matches = matches_history if matches_history is not None else _load_j1_matches(matches_path)
    result = reconcile(matches, predictions_dir=predictions_dir)
    note = None
    discrepancies = result.get("discrepancies") or []
    if discrepancies:
        note = (
            f"{len(discrepancies)} score discrepancy(ies) between the odds-api "
            "provisional result and football-data's confirmed one -- see "
            "result.discrepancy in the affected prediction file(s)"
        )
    return {"ok": True, **result, **({"note": note} if note else {})}


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
    `publish`: a blocked round is never written to `predictions/`.

    So is immutability (DESIGN_SITE.md 3.3). A round already on disk is
    either byte-compatible with what this run just computed -- in which case
    the step is a no-op and says so -- or it is not, in which case the run
    fails rather than overwriting a published prediction. Under the Thursday
    Actions schedule (docs/DESIGN_ACTIONS.md 6) a retry or a manual
    re-dispatch is an ordinary event, and neither must be able to rewrite
    history.
    """
    from footy.eval.tune import load_frozen_params
    from footy.pipeline.predict import existing_conflict, generate_prediction, write_prediction

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

    conflict, existing_path = existing_conflict(payload, predictions_dir=predictions_dir)
    if conflict == "identical":
        return {
            "ok": True, "written": False, "payload": payload,
            "json_path": str(existing_path),
            "md_path": str(existing_path.with_suffix(".md")),
            "note": f"{existing_path.name} already published and unchanged -- not rewritten",
        }
    if conflict is not None:
        return {"ok": False, "payload": payload, "reason": conflict}

    json_path, md_path = write_prediction(payload, predictions_dir=predictions_dir)
    return {
        "ok": True, "written": True, "json_path": str(json_path),
        "md_path": str(md_path), "payload": payload,
    }


def step_publish(*, predict_result: dict, stamp_result: dict | None = None,
                  dry_run: bool = True, repo_root=None) -> dict:
    """Commits the prediction **and this round's stamp artefacts together**
    -- see the module docstring on why the stamp cannot trail the commit."""
    from footy.pipeline.publish import publish, round_tag

    if not predict_result.get("ok") or "json_path" not in predict_result:
        return {"ok": True, "committed": False, "note": "nothing to publish"}

    paths = [predict_result["json_path"], predict_result["md_path"]]
    paths += stamp_paths(stamp_result)
    tag = round_tag(predict_result["payload"])
    return publish(paths, repo_root=repo_root, tag=tag, dry_run=dry_run)


# --- per-round (fired from Thursday, just before publish) --------------------
def stamp_paths(stamp_result: dict | None) -> list[str]:
    """The files `step_stamp` produced that belong in `publish`'s commit: the
    `<round>.ots.json` record always, and the real `.ots` binary when one was
    actually obtained."""
    if not stamp_result or not stamp_result.get("path"):
        return []
    paths = [stamp_result["path"]]
    ots_name = stamp_result.get("ots_path")
    if stamp_result.get("stamped") and ots_name:
        paths.append(str(Path(stamp_result["path"]).parent / ots_name))
    return paths


def step_stamp(*, predict_result: dict, use_ots: bool = False) -> dict:
    """Per-round L2 stamp (DESIGN_SITE.md 3.2), fired for the round that is
    about to be published rather than on a monthly clock -- see this module's
    docstring for why 9's original monthly `stamp` was replaced. A failure
    here is a warning, never a block (9: "失敗は警告のみ").

    `use_ots=True` calls the real `opentimestamps-client` (what the Actions
    workflow passes); the default keeps the network-free stub so no test and
    no local `footy weekly` run reaches a calendar server (DESIGN.md 4).
    """
    from footy.pipeline.stamp import ots_stamp_round, stamp_round

    if not predict_result.get("ok") or "json_path" not in predict_result:
        return {"ok": True, "stamped": False, "note": "nothing to stamp"}
    try:
        stamp = ots_stamp_round if use_ots else stamp_round
        payload = stamp(predict_result["json_path"])
        return {"ok": True, **payload}
    except Exception as exc:                     # 9: "失敗は警告のみ"
        return {"ok": False, "reason": str(exc)}


# --- Friday/Saturday/Sunday night ---------------------------------------------
def step_scores(*, score_events=None, predictions_dir=None, days_from: int = 3) -> dict:
    """Same-night provisional results (`footy/odds/scores.py`'s module
    docstring): The Odds API's `/scores`, ahead of Monday's football-data
    confirmation. The one genuinely network-touching step in this module
    (see the module docstring) -- and even then, only when `score_events`
    isn't already supplied. A missing `ODDS_API_KEY` or a failed fetch is a
    warning, not a stop (9: "失敗は警告のみ"): Monday's `reconcile` is the
    real source of truth regardless of how this step goes.

    `score_events`, when given, skips the network call entirely and applies
    exactly this pre-fetched `/scores` response instead -- what the test
    suite passes so this step can be exercised without touching the network
    (DESIGN.md 4)."""
    from footy.odds.scores import apply_provisional_to_dir, fetch_scores

    if score_events is None:
        import os

        from footy.pipeline.odds_schedule import ApiKeyFetcher

        api_key = os.environ.get("ODDS_API_KEY")
        if not api_key:
            return {
                "ok": True, "updated_files": [], "warnings": [],
                "note": "ODDS_API_KEY not set -- skipped",
            }
        try:
            score_events = fetch_scores(ApiKeyFetcher(api_key), days_from=days_from)
        except Exception as exc:                  # 9: "失敗は警告のみ"
            return {
                "ok": True, "updated_files": [], "warnings": [],
                "note": f"fetch failed: {exc}",
            }

    result = apply_provisional_to_dir(score_events, predictions_dir=predictions_dir)
    note = None
    if result["warnings"]:
        note = f"{len(result['warnings'])} unresolved team spelling(s) skipped"
    return {"ok": True, **result, **({"note": note} if note else {})}


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
    "stamp": step_stamp, "scores": step_scores, "audit": step_audit,
}


def run_weekly(
    *, steps=None, weekday: int | None = None, day_of_month: int | None = None,
    dry_run_publish: bool = True, repo_root=None, snapshot_dir=None,
    matches_path=None, predictions_dir=None, raw_dir=None,
    frozen=None, matches_history=None, asof_utc=None, score_events=None,
    use_ots: bool = False,
) -> dict:
    """Run whichever steps are due. `steps`, if given, overrides the
    weekday/day-of-month gating entirely (used by `footy weekly --steps ...`
    for a manual/backfill run of a specific stage). Stops after `fetch` iff
    it comes back red (9's stated behaviour for that step).

    `use_ots=True` makes the `stamp` step call the real
    `opentimestamps-client` instead of writing the network-free stub. Only
    the GitHub Actions `predict` workflow passes it (docs/DESIGN_ACTIONS.md
    5); every other caller, including the whole test suite, must not.

    `frozen`/`matches_history`, when given, are threaded straight through to
    every step instead of each step re-reading `data/frozen_params.json` /
    `data/matches_jpn1.parquet` -- production never passes these (each step
    is meant to read the current on-disk state), but a test driving the
    whole orchestrator on synthetic data needs to inject both rather than
    touch the real repository's files. `score_events`, similarly, is what a
    test hands `step_scores` instead of letting it reach The Odds API."""
    if steps is None:
        now = pd.Timestamp.now(tz="Asia/Tokyo")
        weekday = now.weekday() if weekday is None else weekday
        day_of_month = now.day if day_of_month is None else day_of_month
        steps = list(WEEKDAY_STEPS.get(weekday, ()))
        if day_of_month == 1:
            steps += list(MONTHLY_STEPS)

    report: dict[str, dict] = {}
    predict_result = None
    stamp_result: dict | None = None
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
                stamp_result=stamp_result, dry_run=dry_run_publish, repo_root=repo_root,
            )
        elif step == "stamp":
            stamp_result = step_stamp(
                predict_result=predict_result or {"ok": False}, use_ots=use_ots
            )
            report[step] = stamp_result
        elif step == "scores":
            report[step] = step_scores(score_events=score_events, predictions_dir=predictions_dir)
        elif step == "audit":
            report[step] = step_audit(frozen=frozen, repo_root=repo_root)
        else:
            report[step] = {"ok": False, "reason": f"unknown step {step!r}"}

    report["steps_run"] = list(steps if "stopped_after" not in report else steps[: steps.index("fetch") + 1])
    return report
