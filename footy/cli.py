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

    if args.to < args.from_:
        print("error: --to is before --from", file=sys.stderr)
        return 2
    print(
        f"fetching {season_label(args.from_)} .. {season_label(args.to)} "
        f"({', '.join(args.divs)}; interval >= {args.interval}s, serial)"
    )
    report = fetch_seasons(
        args.from_,
        args.to,
        divs=tuple(args.divs),
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

    matches = build_matches(divs=tuple(args.divs), out_path=args.out)
    target = args.out or MATCHES_PATH
    seasons = matches["season"].nunique()
    print(
        f"built {len(matches):,} matches over {seasons} seasons "
        f"({matches['date'].min().date()} .. {matches['date'].max().date()})"
    )
    print(f"written: {target}")
    return 0


def cmd_check(args) -> int:
    from footy.data.check import format_result, run_checks
    from footy.data.load import load_matches

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
    from footy.data.load import load_matches
    from footy.eval.report import evaluate, make_run_id, verdict_block, write_report
    from footy.eval.tune import load_frozen_params, model_params
    from footy.eval.walkforward import run_walkforward, save_run

    matches = load_matches(args.matches)
    frozen = load_frozen_params(args.params)
    params = model_params(frozen)
    source = params.pop("source")

    models = list(dict.fromkeys([args.model, "clim"]))
    run_id = args.run_id or make_run_id(f"{args.model}_{args.refit}")

    print(
        f"backtest {season_label(args.from_)} .. {season_label(args.to)} "
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
        season_from=args.from_,
        season_to=args.to,
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


def cmd_report(args) -> int:
    from footy.eval.report import evaluate, verdict_block, write_report
    from footy.eval.walkforward import load_run

    result = load_run(args.run_id, args.reports_dir)
    primary = args.model or result.params.get("models", ["dc"])[0]
    evaluation = evaluate(result, primary=primary, run_id=args.run_id)
    path = write_report(evaluation, args.reports_dir)
    print(verdict_block(evaluation))
    print(f"\nreport: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="footy", description=__doc__)
    parser.add_argument("--matches", default=None, help="path to matches.parquet")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download season CSVs")
    fetch.add_argument("--from", dest="from_", type=_season, default=1995)
    fetch.add_argument("--to", dest="to", type=_season, default=2025)
    fetch.add_argument("--divs", nargs="+", default=list(DEFAULT_DIVS))
    fetch.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC)
    fetch.set_defaults(func=cmd_fetch)

    build = sub.add_parser("build", help="normalise raw CSVs -> matches.parquet")
    build.add_argument("--divs", nargs="+", default=list(DEFAULT_DIVS))
    build.add_argument("--out", default=None)
    build.set_defaults(func=cmd_build)

    check = sub.add_parser("check", help="integrity checks (exit 1 on a problem)")
    check.set_defaults(func=cmd_check)

    tune_cmd = sub.add_parser("tune", help="grid search xi, sigma, pi -> frozen JSON")
    tune_cmd.add_argument("--from", dest="from_", type=_season, default=TUNE_START)
    tune_cmd.add_argument("--through", type=_season, default=TUNE_END)
    tune_cmd.add_argument("--refit", choices=("date", "week"), default="week")
    tune_cmd.add_argument("--out", default=None)
    tune_cmd.set_defaults(func=cmd_tune)

    backtest = sub.add_parser("backtest", help="walk-forward evaluation")
    backtest.add_argument("--from", dest="from_", type=_season, default=TEST_START)
    backtest.add_argument("--to", dest="to", type=_season, default=TEST_END)
    backtest.add_argument("--refit", choices=("date", "week"), default="date")
    backtest.add_argument("--model", choices=("dc", "poisson", "clim"), default="dc")
    backtest.add_argument("--params", default=None, help="frozen_params.json")
    backtest.add_argument("--reports-dir", default=None)
    backtest.add_argument("--run-id", default=None)
    backtest.add_argument("--every", type=int, default=10, help="progress interval")
    backtest.add_argument("--no-save", action="store_true")
    backtest.set_defaults(func=cmd_backtest)

    report = sub.add_parser("report", help="re-render a saved run")
    report.add_argument("run_id")
    report.add_argument("--model", default=None)
    report.add_argument("--reports-dir", default=None)
    report.set_defaults(func=cmd_report)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
