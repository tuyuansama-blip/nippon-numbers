"""footy-ev command line interface.

    ./bin/footy fetch --from 1995 --to 2025
    ./bin/footy build
    ./bin/footy check
    ./bin/footy tune --through 2011-12
    ./bin/footy backtest --from 2012-13 --to 2024-25 [--refit date|week]
                         [--model dc|poisson|clim]
    ./bin/footy report <run_id>

`backtest` prints the `== 判定 ==` block and writes
`reports/backtest_{run_id}.md` plus its two PNGs.
"""

from __future__ import annotations

import argparse
import sys

from footy.config import (
    DEFAULT_DIVS,
    MIN_INTERVAL_SEC,
    TEST_END,
    TEST_START,
    TUNE_END,
    TUNE_START,
    parse_season,
    season_label,
)


def _season(text: str) -> int:
    try:
        return parse_season(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_fetch(args) -> int:
    from footy.data.download import Fetcher, fetch_seasons

    if args.league == "jpn1":
        from footy.data.source_fd_new import fetch_jpn_csv

        path = fetch_jpn_csv(args.raw_dir or None, refresh=getattr(args, "refresh", False))
        print(f"J1: {path}")
        return 0

    divs = tuple(args.divs)
    if args.league == "oos":
        from footy.config import OOS_DIVS

        divs = OOS_DIVS

    if args.to < args.from_:
        print("error: --to is before --from", file=sys.stderr)
        return 2
    print(
        f"fetching {season_label(args.from_)} .. {season_label(args.to)} "
        f"({', '.join(divs)}; interval >= {args.interval}s, serial)"
    )
    report = fetch_seasons(
        args.from_,
        args.to,
        divs=divs,
        fetcher=Fetcher(min_interval=args.interval),
        progress=lambda tag, note: print(f"  {tag:<12} {note}", flush=True),
    )
    print(report.summary())
    if report.missing:
        print("missing (not published yet?): " + ", ".join(report.missing))
    return 0


def cmd_build(args) -> int:
    from footy.config import MATCHES_PATH
    from footy.data.load import build_matches

    if args.league == "jpn1":
        from footy.data.j1_filter import filter_regular_season
        from footy.data.source_fd_new import build_j1_matches

        matches = filter_regular_season(build_j1_matches(args.raw_dir or None))
        target = args.out or (MATCHES_PATH.parent / "matches_jpn1.parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        matches.to_parquet(target, index=False)
    else:
        divs = tuple(args.divs)
        default_out = None
        if args.league == "oos":
            from footy.config import OOS_DIVS

            divs = OOS_DIVS
            default_out = MATCHES_PATH.parent / "matches_oos.parquet"
        matches = build_matches(divs=divs, out_path=args.out or default_out)
        target = args.out or default_out or MATCHES_PATH

    seasons = matches["season"].nunique()
    print(
        f"built {len(matches):,} matches over {seasons} seasons "
        f"({matches['date'].min().date()} .. {matches['date'].max().date()})"
    )
    print(f"written: {target}")
    return 0


def cmd_check(args) -> int:
    from footy.data.check import format_result, run_checks, run_checks_j1, run_checks_oos
    from footy.data.j1_filter import filter_regular_season
    from footy.data.load import load_matches

    if args.league == "oos":
        from footy.config import OOS_DIVS

        matches = load_matches(args.matches)
        matches = matches[matches["div"].isin(OOS_DIVS)]
        result = run_checks_oos(matches)
    elif args.league == "jpn1":
        from footy.config import JPN_DIV

        matches = load_matches(args.matches)
        matches = matches[matches["div"] == JPN_DIV]
        result = run_checks_j1(filter_regular_season(matches))
    else:
        result = run_checks(load_matches(args.matches))
    print(format_result(result))
    return 0 if result.ok else 1


def cmd_tune(args) -> int:
    from footy.data.load import load_matches
    from footy.eval.tune import tune, write_frozen_params

    matches = load_matches(args.matches)
    print(
        f"tuning on {season_label(args.from_)} .. {season_label(args.through)} "
        f"(refit={args.refit}). This is the expensive one: run it once, "
        "then never again."
    )
    payload = tune(
        matches,
        season_from=args.from_,
        season_to=args.through,
        refit=args.refit,
        progress=lambda stage, point: print(
            f"  {stage:<10} "
            f"HL={point['half_life_days']:>6} sigma={point['sigma']:<5} "
            f"rps={point['rps']:.5f} ({point['n_folds']} fits, "
            f"{point['fit_seconds']:.1f}s)",
            flush=True,
        ),
    )
    path = write_frozen_params(payload, args.out)
    print(
        f"\nfrozen: half_life={payload['half_life_days']} "
        f"sigma={payload['sigma']} pi={payload['pi']} "
        f"({payload['total_fits']:,} fits)"
    )
    print(f"written: {path}")
    return 0


def cmd_backtest(args) -> int:
    from footy.eval.report import evaluate, make_run_id, verdict_block, write_report
    from footy.eval.tune import load_frozen_params, model_params
    from footy.eval.walkforward import run_walkforward, save_run

    if args.league in ("oos", "jpn1"):
        return _cmd_backtest_v2(args)

    from footy.data.load import load_matches

    season_from = args.from_ if args.from_ is not None else TEST_START
    season_to = args.to if args.to is not None else TEST_END

    matches = load_matches(args.matches)
    frozen = load_frozen_params(args.params)
    params = model_params(frozen)
    source = params.pop("source")

    models = list(dict.fromkeys([args.model, "clim"]))
    run_id = args.run_id or make_run_id(f"{args.model}_{args.refit}")

    print(
        f"backtest {season_label(season_from)} .. {season_label(season_to)} "
        f"model={args.model} refit={args.refit}"
    )
    print(f"  params: {params}")
    print(f"  source: {source}")

    def progress(number, total, row):
        if number % args.every == 0 or number == total:
            seconds = row.get(f"{args.model}_seconds")
            print(
                f"  fold {number:>4}/{total} {row['fold'].date()} "
                f"train={row['n_train']:>6,} test={row['n_test']:>3} "
                + (f"fit={seconds:.3f}s" if seconds is not None else ""),
                flush=True,
            )

    result = run_walkforward(
        matches,
        season_from=season_from,
        season_to=season_to,
        models=models,
        refit=args.refit,
        params=params,
        progress=progress,
    )
    result.params["frozen_params"] = source
    evaluation = evaluate(result, primary=args.model, run_id=run_id)

    if not args.no_save:
        save_run(result, run_id, args.reports_dir)
    path = write_report(evaluation, args.reports_dir)

    print()
    print(verdict_block(evaluation))
    print(f"\nreport: {path}")
    print(f"run_id: {run_id}")
    return 0


def _cmd_backtest_v2(args) -> int:
    """`--league oos|jpn1` (DESIGN_PHASE2.md 5, 7): calibration-v2 judging,
    the OOS-LEAGUES per-division pooling, and J1's absolute-difference scale."""
    import pandas as pd

    from footy.eval.report import evaluate_v2, make_run_id, verdict_block_v2, write_report_v2
    from footy.eval.tune import load_frozen_params
    from footy.eval.walkforward import run_walkforward, save_run

    frozen = load_frozen_params(args.params)
    if not frozen:
        print("error: data/frozen_params.json not found -- run `footy tune` first",
              file=sys.stderr)
        return 2

    if args.league == "oos":
        from footy.config import OOS_DIVS, TEST_END, TEST_START, TUNE_END, TUNE_START
        from footy.eval.multileague import run_multileague_walkforward

        matches_path = args.matches or "data/matches_oos.parquet"
        matches = pd.read_parquet(matches_path)
        season_from = args.from_ or TEST_START
        season_to = args.to or TEST_END
        run_id = args.run_id or make_run_id("oos_dc_cal")

        def progress(div, number, total, row):
            if number % args.every == 0 or number == total:
                print(f"  {div:<4} {number:>4}/{total} {row['fold'].date()}", flush=True)

        result = run_multileague_walkforward(
            matches, OOS_DIVS, season_from=season_from, season_to=season_to,
            pi_from=TUNE_START, pi_to=TUNE_END, frozen_params=frozen,
            refit=args.refit, calibrate="tb", progress=progress,
        )
        league_mode = "gap"
    else:
        from footy.config import JPN_TEST_END, JPN_TEST_START

        matches_path = args.matches or "data/matches_jpn1.parquet"
        matches = pd.read_parquet(matches_path)
        season_from = args.from_ or JPN_TEST_START
        season_to = args.to or JPN_TEST_END
        run_id = args.run_id or make_run_id("jpn1_dc_cal")

        def progress(number, total, row):
            if number % args.every == 0 or number == total:
                print(f"  {number:>4}/{total} {row['fold']}", flush=True)

        params = {
            "half_life_days": frozen["half_life_days"], "sigma": frozen["sigma"],
            "pi": (0.0, 0.0),   # pi_online (6.6) not implemented -- see report
            "window_seasons": frozen.get("window_seasons", 6),
        }
        result = run_walkforward(
            matches, season_from=season_from, season_to=season_to,
            models=("dc", "clim"), refit=args.refit, params=params,
            calibrate="tb", progress=progress,
        )
        league_mode = "absolute"

    if not args.no_save:
        save_run(result, run_id, args.reports_dir)

    primary = args.model if args.model != "dc" else "dc_cal"
    evaluation = evaluate_v2(result, primary=primary, run_id=run_id, league_mode=league_mode)
    path = write_report_v2(evaluation, args.reports_dir, league_mode=league_mode)

    print()
    print(verdict_block_v2(evaluation, league_mode=league_mode))
    print(f"\nreport: {path}")
    print(f"run_id: {run_id}")
    return 0


def cmd_report(args) -> int:
    from footy.eval.walkforward import load_run

    result = load_run(args.run_id, args.reports_dir)
    primary = args.model or result.params.get("models", ["dc"])[0]

    if args.v2:
        from footy.eval.report import evaluate_v2, verdict_block_v2, write_report_v2

        evaluation = evaluate_v2(
            result, primary=primary, run_id=args.run_id, league_mode=args.league_mode
        )
        path = write_report_v2(evaluation, args.reports_dir, league_mode=args.league_mode)
        print(verdict_block_v2(evaluation, league_mode=args.league_mode))
    else:
        from footy.eval.report import evaluate, verdict_block, write_report

        evaluation = evaluate(result, primary=primary, run_id=args.run_id)
        path = write_report(evaluation, args.reports_dir)
        print(verdict_block(evaluation))
    print(f"\nreport: {path}")
    return 0


def cmd_odds_ingest(args) -> int:
    from footy.odds.ingest import ingest

    frame = ingest(args.snapshot_dir, out_path=args.out)
    print(f"odds_quotes: {len(frame):,} rows, {frame['event_id'].nunique()} events")
    return 0


def cmd_odds_close(args) -> int:
    from footy.odds.close import write_close

    path = write_close(args.quotes, args.out)
    print(f"written: {path}")
    return 0


def cmd_odds_schedule(args) -> int:
    import os

    from footy.pipeline.odds_schedule import run_schedule

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("error: ODDS_API_KEY not set", file=sys.stderr)
        return 2
    written = run_schedule(
        api_key, snapshot_dir=args.snapshot_dir, dry_run=args.dry_run,
        wait=not args.no_wait,
    )
    print(f"snapshots written: {written}")
    return 0


def cmd_odds_sync(args) -> int:
    """`footy odds sync {pull,push}` (docs/DESIGN_ACTIONS.md 2): move raw
    snapshots and the schedule state between `data/odds_snapshots/` and the
    private R2 bucket. Never touches the public repository -- the raw feed
    must not enter it (DESIGN_SITE.md 2.6)."""
    from footy.config import DATA_DIR
    from footy.odds import sync as sync_module
    from footy.odds.r2 import missing_env_names

    client = sync_module.client_from_env()
    if client is None:
        names = ", ".join(missing_env_names())
        print(f"error: R2 is not configured -- missing {names}", file=sys.stderr)
        print("see docs/SETUP_ACTIONS.md for how to create the token", file=sys.stderr)
        return 2

    snapshot_dir = args.snapshot_dir or (DATA_DIR / "odds_snapshots")
    include = "state" if args.state_only else ("snapshots" if args.snapshots_only else "all")
    action = sync_module.pull if args.direction == "pull" else sync_module.push
    result = action(client, snapshot_dir=snapshot_dir, include=include, dry_run=args.dry_run)
    moved = result.get("pulled" if args.direction == "pull" else "pushed", [])
    print(f"{args.direction}: {len(moved)} snapshot(s), state={result.get('state')}")
    return 0


def cmd_stamp(args) -> int:
    """`footy stamp <prediction.json>` (DESIGN_SITE.md 3.2-L2). `--ots` calls
    the real `opentimestamps-client`; without it the network-free stub record
    is written instead."""
    from footy.pipeline.stamp import ots_stamp_round, stamp_round

    stamp = ots_stamp_round if args.ots else stamp_round
    payload = stamp(args.path)
    print(f"sha256: {payload['sha256']}")
    print(f"stamped: {payload['stamped']}")
    if payload.get("ots_path"):
        print(f"ots: {payload['ots_path']}")
    print(f"written: {payload['path']}")
    if not payload["stamped"] and args.ots:
        print(f"warning: {payload['note']}", file=sys.stderr)
    return 0


def cmd_stamp_upgrade(args) -> int:
    """`footy stamp-upgrade` -- `ots upgrade` every `.ots` under
    `predictions/`, so a calendar commitment picks up its Bitcoin
    attestation. Touches only `.ots` bytes, never a prediction JSON
    (DESIGN_SITE.md 3.2's operating rule)."""
    from footy.config import PREDICTIONS_DIR
    from footy.pipeline.stamp import ots_upgrade, stamp_records

    root = args.predictions_dir or PREDICTIONS_DIR
    records = stamp_records(root)
    if not records:
        print("no stamped predictions to upgrade")
        return 0
    changed = 0
    for _, record, ots_path in records:
        result = ots_upgrade(ots_path)
        changed += bool(result["changed"])
        state = "upgraded" if result["changed"] else ("ok" if result["ok"] else "pending")
        print(f"{record['file']}: {state}" + (f" ({result['note']})" if result["note"] else ""))
    print(f"{changed} of {len(records)} .ots file(s) changed")
    return 0


def cmd_odds_scores(args) -> int:
    """`footy odds scores` -- the provisional half of the two-stage results
    pipeline (footy/odds/scores.py's module docstring). 2 credits/call."""
    import os

    from footy.odds.scores import apply_provisional_to_dir, fetch_scores
    from footy.pipeline.odds_schedule import ApiKeyFetcher

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("error: ODDS_API_KEY not set", file=sys.stderr)
        return 2

    fetcher = ApiKeyFetcher(api_key)
    events = fetch_scores(fetcher, days_from=args.days_from)
    completed = [e for e in events if e.get("completed")]
    print(f"scores: {len(events)} event(s) returned, {len(completed)} completed")

    result = apply_provisional_to_dir(
        events, predictions_dir=args.predictions_dir,
        warn=lambda message: print(f"  warning: {message}", file=sys.stderr),
    )
    for name in result["updated_files"]:
        print(f"updated: {name}")
    if not result["updated_files"]:
        print("no matching unresolved fixtures found")
    return 0


def cmd_predict(args) -> int:
    """`footy predict --league jpn1 --round next` (DESIGN_PHASE2.md 9, 7.5)."""
    import pandas as pd

    from footy.eval.tune import load_frozen_params
    from footy.pipeline.predict import generate_prediction, write_prediction

    matches_path = args.matches or "data/matches_jpn1.parquet"
    matches = pd.read_parquet(matches_path)
    frozen = load_frozen_params(args.params)
    if not frozen:
        print("error: data/frozen_params.json not found -- run `footy tune` first",
              file=sys.stderr)
        return 2

    result = generate_prediction(
        matches_history=matches, frozen=frozen, snapshot_dir=args.snapshot_dir,
    )
    if not result["ok"]:
        print(f"error: {result['reason']}", file=sys.stderr)
        return 2

    payload = result["payload"]
    gate = payload["publish_gate"]
    print(
        f"round {payload['round_id']} ({payload['season_label']}): "
        f"{len(payload['matches'])} fixture(s)"
    )
    phi = payload["phi"]
    print(
        f"  phi: temperature={phi['temperature']:.4f} phi_home={phi['phi_home']:.4f} "
        f"phi_draw={phi['phi_draw']:.4f} (n_history={phi['n_history']}, warm={phi['warm']})"
    )
    print(f"  training window: {payload['training_window']['n_matches']} matches")
    print(f"  params_hash: {payload['params_hash']} ({payload['params_hash_check']['note']})")

    if not gate["ok"]:
        print("publish gate BLOCKED -- no prediction file written:", file=sys.stderr)
        for reason in gate["reasons"]:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    json_path, md_path = write_prediction(payload, predictions_dir=args.predictions_dir)
    print(f"written: {json_path}")
    print(f"written: {md_path}")
    return 0


def cmd_site_build(args) -> int:
    """`footy site build` (DESIGN_SITE.md 2.1): predictions/*.json -> static
    HTML in site/. Writes nothing if there is no prediction to render."""
    from footy.site.build import build_site

    result = build_site(predictions_dir=args.predictions_dir, output_dir=args.out)
    if not result["ok"]:
        print(f"error: {result['reason']}", file=sys.stderr)
        return 2

    print(f"round {result['round_id']}: {result['n_fixtures']} fixture(s)")
    print(f"track record: {result['track_record_rows']} resolved match(es)")
    for name, path in result["written"].items():
        print(f"written: {path}")
    print(f"output dir: {result['output_dir']}")
    return 0


def cmd_weekly(args) -> int:
    """`footy weekly` -- runs the DESIGN_PHASE2.md 9 pipeline steps due today
    (JST), or exactly `--steps` when given."""
    from footy.pipeline.weekly import run_weekly

    result = run_weekly(
        steps=args.steps, dry_run_publish=not args.publish, use_ots=args.ots
    )
    for step in result.get("steps_run", []):
        payload = result.get(step, {})
        ok = payload.get("ok") if isinstance(payload, dict) else None
        note = payload.get("reason") or payload.get("note") or ""
        print(f"[{step}] ok={ok}" + (f"  {note}" if note else ""))
    if "stopped_after" in result:
        print(f"stopped after: {result['stopped_after']}")
    failed = [
        s for s in result.get("steps_run", [])
        if isinstance(result.get(s), dict) and result[s].get("ok") is False
    ]
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="footy", description=__doc__)
    parser.add_argument("--matches", default=None, help="path to matches.parquet")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download season CSVs")
    fetch.add_argument("--from", dest="from_", type=_season, default=1995)
    fetch.add_argument("--refresh", action="store_true", help="discard the cached JPN.csv and re-download (jpn1 only)")
    fetch.add_argument("--to", dest="to", type=_season, default=2025)
    fetch.add_argument("--divs", nargs="+", default=list(DEFAULT_DIVS))
    fetch.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC)
    fetch.add_argument("--league", choices=("e0", "oos", "jpn1"), default="e0")
    fetch.add_argument("--raw-dir", dest="raw_dir", default=None)
    fetch.set_defaults(func=cmd_fetch)

    build = sub.add_parser("build", help="normalise raw CSVs -> matches.parquet")
    build.add_argument("--divs", nargs="+", default=list(DEFAULT_DIVS))
    build.add_argument("--out", default=None)
    build.add_argument("--league", choices=("e0", "oos", "jpn1"), default="e0")
    build.add_argument("--raw-dir", dest="raw_dir", default=None)
    build.set_defaults(func=cmd_build)

    check = sub.add_parser("check", help="integrity checks (exit 1 on a problem)")
    check.add_argument("--league", choices=("e0", "oos", "jpn1"), default="e0")
    check.set_defaults(func=cmd_check)

    tune_cmd = sub.add_parser("tune", help="grid search xi, sigma, pi -> frozen JSON")
    tune_cmd.add_argument("--from", dest="from_", type=_season, default=TUNE_START)
    tune_cmd.add_argument("--through", type=_season, default=TUNE_END)
    tune_cmd.add_argument("--refit", choices=("date", "week"), default="week")
    tune_cmd.add_argument("--out", default=None)
    tune_cmd.set_defaults(func=cmd_tune)

    backtest = sub.add_parser("backtest", help="walk-forward evaluation")
    backtest.add_argument("--from", dest="from_", type=_season, default=None)
    backtest.add_argument("--to", dest="to", type=_season, default=None)
    backtest.add_argument("--refit", choices=("date", "week"), default="date")
    backtest.add_argument("--model", choices=("dc", "poisson", "clim"), default="dc")
    backtest.add_argument("--params", default=None, help="frozen_params.json")
    backtest.add_argument("--reports-dir", default=None)
    backtest.add_argument("--run-id", default=None)
    backtest.add_argument("--every", type=int, default=10, help="progress interval")
    backtest.add_argument("--no-save", action="store_true")
    backtest.add_argument("--league", choices=("epl", "oos", "jpn1"), default="epl")
    backtest.set_defaults(func=cmd_backtest)

    report = sub.add_parser("report", help="re-render a saved run")
    report.add_argument("run_id")
    report.add_argument("--model", default=None)
    report.add_argument("--reports-dir", default=None)
    report.add_argument("--v2", action="store_true",
                         help="re-score with calibration v2 (DESIGN_PHASE2.md 3)")
    report.add_argument("--league-mode", choices=("gap", "absolute"), default="gap")
    report.set_defaults(func=cmd_report)

    odds = sub.add_parser("odds", help="J1 forward odds pipeline (DESIGN_PHASE2.md 8)")
    odds_sub = odds.add_subparsers(dest="odds_command", required=True)

    odds_ingest = odds_sub.add_parser("ingest", help="snapshots -> odds_quotes.parquet")
    odds_ingest.add_argument("--snapshot-dir", dest="snapshot_dir", default=None)
    odds_ingest.add_argument("--out", default=None)
    odds_ingest.set_defaults(func=cmd_odds_ingest)

    odds_close = odds_sub.add_parser("close", help="odds_quotes.parquet -> odds_close.parquet")
    odds_close.add_argument("--quotes", default=None)
    odds_close.add_argument("--out", default=None)
    odds_close.set_defaults(func=cmd_odds_close)

    odds_schedule = odds_sub.add_parser(
        "schedule", help="kickoff-anchored collection pass (DESIGN_PHASE2.md 8.4)"
    )
    odds_schedule.add_argument("--snapshot-dir", dest="snapshot_dir", default=None)
    odds_schedule.add_argument("--dry-run", action="store_true")
    odds_schedule.add_argument(
        "--no-wait", dest="no_wait", action="store_true",
        help="never sleep for a near-kickoff point (old crontab behaviour; "
             "docs/DESIGN_ACTIONS.md 4)",
    )
    odds_schedule.set_defaults(func=cmd_odds_schedule)

    odds_sync = odds_sub.add_parser(
        "sync", help="move snapshots/state between data/odds_snapshots/ and R2"
    )
    odds_sync.add_argument("direction", choices=("pull", "push"))
    odds_sync.add_argument("--snapshot-dir", dest="snapshot_dir", default=None)
    odds_sync.add_argument("--state-only", dest="state_only", action="store_true")
    odds_sync.add_argument("--snapshots-only", dest="snapshots_only", action="store_true")
    odds_sync.add_argument("--dry-run", action="store_true")
    odds_sync.set_defaults(func=cmd_odds_sync)

    odds_scores = odds_sub.add_parser(
        "scores", help="provisional results from /scores, ahead of football-data (2 credits/call)"
    )
    odds_scores.add_argument("--days-from", dest="days_from", type=int, default=3)
    odds_scores.add_argument("--predictions-dir", dest="predictions_dir", default=None)
    odds_scores.set_defaults(func=cmd_odds_scores)

    predict = sub.add_parser(
        "predict", help="next-round J1 predictions (DESIGN_PHASE2.md 9, 7.5)"
    )
    predict.add_argument("--league", choices=("jpn1",), default="jpn1")
    predict.add_argument("--round", choices=("next",), default="next")
    predict.add_argument("--params", default=None, help="frozen_params.json")
    predict.add_argument("--snapshot-dir", dest="snapshot_dir", default=None)
    predict.add_argument("--predictions-dir", dest="predictions_dir", default=None)
    predict.set_defaults(func=cmd_predict)

    site = sub.add_parser("site", help="public site generator (DESIGN_SITE.md 2.1)")
    site_sub = site.add_subparsers(dest="site_command", required=True)

    site_build = site_sub.add_parser(
        "build", help="predictions/*.json -> static HTML in site/"
    )
    site_build.add_argument("--predictions-dir", dest="predictions_dir", default=None)
    site_build.add_argument("--out", default=None, help="output directory (default: site/)")
    site_build.set_defaults(func=cmd_site_build)

    weekly = sub.add_parser("weekly", help="run the weekly pipeline (DESIGN_PHASE2.md 9)")
    weekly.add_argument(
        "--steps", nargs="+", default=None,
        help="run exactly these steps instead of today's weekday schedule",
    )
    weekly.add_argument(
        "--publish", action="store_true",
        help="actually git-commit/tag the publish step (default: dry-run)",
    )
    weekly.add_argument(
        "--ots", action="store_true",
        help="stamp with the real opentimestamps-client (Actions only; "
             "default writes the network-free stub)",
    )
    weekly.set_defaults(func=cmd_weekly)

    stamp = sub.add_parser("stamp", help="per-round OpenTimestamps stamp (DESIGN_SITE.md 3.2)")
    stamp.add_argument("path", help="path to a prediction JSON")
    stamp.add_argument("--ots", action="store_true", help="call the real `ots stamp`")
    stamp.set_defaults(func=cmd_stamp)

    stamp_upgrade = sub.add_parser(
        "stamp-upgrade", help="`ots upgrade` every .ots under predictions/"
    )
    stamp_upgrade.add_argument("--predictions-dir", dest="predictions_dir", default=None)
    stamp_upgrade.set_defaults(func=cmd_stamp_upgrade)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
