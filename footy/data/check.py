"""`footy check` -- cheap regression detection on the built parquet.

Everything here is a property that must hold for *any* correctly parsed E0
season. When one of them breaks it is almost always a parse regression (a
shifted column, a day/month swap) rather than a genuine change in the data,
and a parse regression is exactly the kind of bug that produces a beautiful
backtest and no information (DESIGN.md 6.1).

`ok=False` -> the CLI exits 1. Unknown team spellings are always fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from footy.config import (
    JPN_OVERROUND_BFEC_MAX,
    JPN_OVERROUND_BFEC_MIN,
    JPN_OVERROUND_PSC_MAX,
    JPN_OVERROUND_PSC_MIN,
    OOS_MIN_APPEARANCES,
    OVERROUND_MAX,
    OVERROUND_MIN,
    season_label,
)
from footy.data.teams import canonical_team, canonical_team_j1, near_duplicate_keys

MATCHES_PER_SEASON = 380
MATCHES_PER_TEAM = 38
HOME_PER_TEAM = 19


@dataclass
class CheckResult:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    psc_missing: pd.DataFrame | None = None

    def fail(self, message: str) -> None:
        self.ok = False
        self.problems.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def _check_unknown_teams(matches: pd.DataFrame, result: CheckResult) -> None:
    names = pd.unique(
        pd.concat([matches["home_team"], matches["away_team"]]).dropna().astype(str)
    )
    unknown = sorted(n for n in names if canonical_team(n) is None)
    missing_id = int(matches["home_id"].isna().sum() + matches["away_id"].isna().sum())
    result.stats["distinct_teams"] = int(len(names))
    result.stats["unknown_teams"] = len(unknown)
    if unknown:
        result.fail(
            f"unknown team spellings ({len(unknown)}): {', '.join(unknown[:20])}"
            " -- add them to footy/data/teams.py ALIASES"
        )
    if missing_id:
        result.fail(f"{missing_id} rows have no team_id")


def _check_season_shape(matches: pd.DataFrame, result: CheckResult) -> None:
    bad_counts, bad_teams = [], []
    for season, group in matches.groupby("season", sort=True):
        if len(group) != MATCHES_PER_SEASON:
            bad_counts.append(f"{season_label(int(season))}={len(group)}")
        counts = (
            pd.concat([group["home_team"], group["away_team"]])
            .value_counts()
            .sort_index()
        )
        home_counts = group["home_team"].value_counts()
        for team, total in counts.items():
            home = int(home_counts.get(team, 0))
            if total != MATCHES_PER_TEAM or home != HOME_PER_TEAM:
                bad_teams.append(
                    f"{season_label(int(season))} {team}: {total} matches "
                    f"({home} home)"
                )
    if bad_counts:
        result.fail(
            f"seasons without {MATCHES_PER_SEASON} rows: {', '.join(bad_counts)}"
        )
    if bad_teams:
        result.fail(
            f"teams without 19 home / 19 away ({len(bad_teams)}): "
            + "; ".join(bad_teams[:10])
        )


def _check_results(matches: pd.DataFrame, result: CheckResult) -> None:
    goals = matches[["fthg", "ftag"]]
    missing = int(goals.isna().any(axis=1).sum())
    negative = int(((goals.fillna(0) < 0).any(axis=1)).sum())
    if missing:
        result.fail(f"{missing} rows have a missing full-time score")
    if negative:
        result.fail(f"{negative} rows have a negative score")

    expected = np.where(
        matches["fthg"] > matches["ftag"],
        "H",
        np.where(matches["fthg"] < matches["ftag"], "A", "D"),
    )
    mismatch = matches["ftr"].astype(str).to_numpy() != expected
    n_mismatch = int(mismatch.sum())
    result.stats["ftr_mismatch"] = n_mismatch
    if n_mismatch:
        rows = matches.loc[mismatch, ["date", "home_team", "away_team", "fthg",
                                      "ftag", "ftr"]].head(5)
        result.fail(
            f"FTR disagrees with FTHG/FTAG on {n_mismatch} rows:\n"
            + rows.to_string(index=False)
        )


def _check_duplicates(matches: pd.DataFrame, result: CheckResult) -> None:
    key = ["date", "home_team", "away_team"]
    dupes = matches[matches.duplicated(key, keep=False)]
    result.stats["duplicates"] = int(len(dupes))
    if len(dupes):
        result.fail(
            f"{len(dupes)} duplicate (date, home, away) rows:\n"
            + dupes[key].head(10).to_string(index=False)
        )


def _check_dates(matches: pd.DataFrame, result: CheckResult) -> None:
    """A season's matches must fall inside its own window.

    A day/month swap in the mixed-format parse shows up here immediately: a
    'March' fixture landing outside Aug..Jul is not a fixture-list quirk.
    """
    bad = []
    for season, group in matches.groupby("season", sort=True):
        lo = pd.Timestamp(year=int(season), month=8, day=1)
        hi = pd.Timestamp(year=int(season) + 1, month=8, day=31)
        outside = group[(group["date"] < lo) | (group["date"] > hi)]
        if len(outside):
            bad.append(
                f"{season_label(int(season))}: {len(outside)} rows outside "
                f"{lo.date()}..{hi.date()} "
                f"(first {outside['date'].min().date()})"
            )
    if bad:
        result.fail("season/date mismatch: " + "; ".join(bad))
    result.stats["date_min"] = str(matches["date"].min().date())
    result.stats["date_max"] = str(matches["date"].max().date())


def _check_odds(matches: pd.DataFrame, result: CheckResult) -> None:
    odds = matches[["psch", "pscd", "psca"]]
    have = odds.notna().all(axis=1)
    result.stats["psc_rows"] = int(have.sum())
    result.stats["psc_missing"] = int((~have).sum())

    per_season = (
        matches.assign(_missing=(~have).astype(int))
        .groupby("season", sort=True)
        .agg(rows=("match_id", "size"), psc_missing=("_missing", "sum"))
        .reset_index()
    )
    per_season["season_label"] = per_season["season"].map(
        lambda y: season_label(int(y))
    )
    result.psc_missing = per_season[["season_label", "rows", "psc_missing"]]

    present = odds[have]
    if present.empty:
        result.fail("no Pinnacle closing odds at all -- column names changed?")
        return
    nonpositive = int((present <= 1.0).any(axis=1).sum())
    if nonpositive:
        result.fail(f"{nonpositive} rows have a PSC* price <= 1.00")

    overround = (1.0 / present).sum(axis=1)
    result.stats["overround_mean"] = float(overround.mean())
    result.stats["overround_p01"] = float(overround.quantile(0.01))
    result.stats["overround_p99"] = float(overround.quantile(0.99))
    result.stats["overround_min"] = float(overround.min())
    result.stats["overround_max"] = float(overround.max())

    outside = overround[(overround < OVERROUND_MIN) | (overround > OVERROUND_MAX)]
    result.stats["overround_outside"] = int(len(outside))
    if len(outside):
        result.fail(
            f"{len(outside)} rows have a 1X2 overround outside "
            f"[{OVERROUND_MIN:.2f}, {OVERROUND_MAX:.2f}] "
            f"(min {outside.min():.4f}, max {outside.max():.4f}) "
            "-- likely a column mix-up or a parse regression"
        )


def run_checks(matches: pd.DataFrame) -> CheckResult:
    result = CheckResult()
    result.stats["rows"] = int(len(matches))
    result.stats["seasons"] = int(matches["season"].nunique())
    result.stats["divs"] = ",".join(sorted(matches["div"].dropna().unique()))

    _check_unknown_teams(matches, result)
    _check_season_shape(matches, result)
    _check_results(matches, result)
    _check_duplicates(matches, result)
    _check_dates(matches, result)
    _check_odds(matches, result)

    empty = int(matches["empty_crowd"].sum())
    result.stats["empty_crowd_rows"] = empty
    result.note(
        f"{empty} matches flagged crowd=empty (COVID window; reported both "
        "ways, judged with them in)"
    )
    if matches["xg_home"].notna().any():
        result.note("xg_home is no longer all-NaN -- an xG source was added")
    return result


# ==============================================================================
# Phase 2 (DESIGN_PHASE2.md 5.4, 6.7). Two more profiles: OOS-LEAGUES relaxes
# the E0-specific checks that cannot hold across 15 divisions (a hand-curated
# team whitelist, an exact 380-row season), J1 tightens back down to E0's
# discipline with its own numbers (18/20-team seasons, two odds generations).
# `run_checks` (above) is untouched and stays the default -- every existing
# caller keeps its exact E0 behaviour.
# ==============================================================================


def _check_oos_teams(matches: pd.DataFrame, result: CheckResult) -> None:
    """Non-E0 divisions auto-register (DESIGN_PHASE2.md 5.4): there is no
    'unknown spelling' to fail on, only two warnings -- a likely spelling
    split (near-duplicate keys within one season) and a club too rarely seen
    to be a real top-flight side that season."""
    by_group: dict[tuple, list] = {}
    for (div, season), group in matches.groupby(["div", "season"], sort=True):
        names = pd.concat([group["home_team"], group["away_team"]]).dropna()
        by_group[(str(div), int(season))] = names.unique().tolist()
    dups = near_duplicate_keys(by_group)
    result.stats["near_duplicate_pairs"] = len(dups)
    if dups:
        sample = "; ".join(f"{s}:{a}~{b}" for s, a, b in dups[:10])
        result.note(
            f"{len(dups)} near-duplicate team-key pairs in the same season "
            f"(likely one club auto-registered under two spellings): {sample}"
        )

    counts = pd.concat([matches["home_id"], matches["away_id"]]).value_counts()
    low = counts[counts < OOS_MIN_APPEARANCES]
    result.stats["low_appearance_teams"] = int(len(low))
    if len(low):
        result.note(
            f"{len(low)} auto-registered team ids with < {OOS_MIN_APPEARANCES} "
            "total appearances (cup replays, a single relegation-play-off "
            "leg, or a genuine spelling split)"
        )


def _check_unknown_teams_j1(matches: pd.DataFrame, result: CheckResult) -> None:
    names = pd.unique(
        pd.concat([matches["home_team"], matches["away_team"]]).dropna().astype(str)
    )
    unknown = sorted(n for n in names if canonical_team_j1(n) is None)
    result.stats["distinct_teams"] = int(len(names))
    result.stats["unknown_teams"] = len(unknown)
    if unknown:
        result.fail(
            f"unknown J1 team spellings ({len(unknown)}): {', '.join(unknown[:20])}"
            " -- add them to footy/data/teams.py JPN_ALIASES"
        )


def _check_j1_season_shape(matches: pd.DataFrame, result: CheckResult) -> None:
    """`N(N-1)` matches, `N in {18, 20}`, per season -- meaningful only after
    `j1_filter.filter_regular_season` has dropped the play-off rows
    (DESIGN_PHASE2.md 6.3, 6.7). The in-progress season is skipped: it has
    not been played out far enough for the count to mean anything yet."""
    bad = []
    current = int(matches["season"].max()) if len(matches) else None
    for season, group in matches.groupby("season", sort=True):
        season = int(season)
        if season == current:
            continue
        teams = set(group["home_id"]) | set(group["away_id"])
        n = len(teams)
        expected = n * (n - 1)
        if n not in (18, 20) or len(group) != expected:
            bad.append(f"{season}: N={n} rows={len(group)} expected={expected}")
    if bad:
        result.fail("J1 season shape (post play-off filter): " + "; ".join(bad))


def _check_overround(
    matches: pd.DataFrame,
    result: CheckResult,
    cols: tuple[str, str, str],
    lo: float,
    hi: float,
    label: str,
) -> None:
    """The PSC-close check in `_check_odds`, generalised to any 1X2 triple
    and bound pair -- reused for J1's PSC *and* BFEC generations
    (DESIGN_PHASE2.md 6.7)."""
    odds = matches[list(cols)]
    have = odds.notna().all(axis=1)
    result.stats[f"{label}_rows"] = int(have.sum())
    result.stats[f"{label}_missing"] = int((~have).sum())
    present = odds[have]
    if present.empty:
        result.note(f"no {label.upper()} prices at all")
        return
    nonpositive = int((present <= 0.0).any(axis=1).sum())
    if nonpositive:
        result.fail(f"{nonpositive} rows have a non-positive {label.upper()} price")

    overround = (1.0 / present).sum(axis=1)
    result.stats[f"{label}_overround_mean"] = float(overround.mean())
    outside = overround[(overround < lo) | (overround > hi)]
    result.stats[f"{label}_overround_outside"] = int(len(outside))
    if len(outside):
        result.fail(
            f"{len(outside)} rows have a {label.upper()} overround outside "
            f"[{lo:.2f}, {hi:.2f}] (min {outside.min():.4f}, max "
            f"{outside.max():.4f})"
        )


def _check_dates_oos(matches: pd.DataFrame, result: CheckResult) -> None:
    """`_check_dates`, widened one month earlier (Jul 1, not Aug 1).

    Measured directly off the 2005/06-2024/25 OOS-LEAGUES pull: every row
    E0's Aug1..Aug31(+1y) window rejects falls in 15-31 July, and belongs to
    B1, D2, F2, SC0 or E1 -- Belgian/German-2/French-2/Scottish/English-2
    seasons that routinely open in the second half of July. None land
    outside Jul1..Aug31(+1y), so that (not E0's Aug1) is the window a parse
    bug should be judged against here; this still fails on a real day/month
    swap, it just stops flagging a legitimate early kickoff as one.
    """
    bad = []
    for season, group in matches.groupby("season", sort=True):
        lo = pd.Timestamp(year=int(season), month=7, day=1)
        hi = pd.Timestamp(year=int(season) + 1, month=8, day=31)
        outside = group[(group["date"] < lo) | (group["date"] > hi)]
        if len(outside):
            bad.append(
                f"{season_label(int(season))}: {len(outside)} rows outside "
                f"{lo.date()}..{hi.date()} (first {outside['date'].min().date()})"
            )
    if bad:
        result.fail("season/date mismatch (Jul1..Aug31 window): " + "; ".join(bad))
    result.stats["date_min"] = str(matches["date"].min().date())
    result.stats["date_max"] = str(matches["date"].max().date())


def run_checks_oos(matches: pd.DataFrame) -> CheckResult:
    """OOS-LEAGUES profile: same shape as `run_checks`, minus the checks that
    are structurally E0-only (DESIGN_PHASE2.md 5.4)."""
    result = CheckResult()
    result.stats["rows"] = int(len(matches))
    result.stats["seasons"] = int(matches["season"].nunique())
    result.stats["divs"] = ",".join(sorted(matches["div"].dropna().unique()))

    _check_oos_teams(matches, result)
    _check_results(matches, result)
    _check_duplicates(matches, result)
    _check_dates_oos(matches, result)
    _check_odds(matches, result)
    return result


def run_checks_j1(matches: pd.DataFrame) -> CheckResult:
    """J1 profile: run on the play-off-filtered frame
    (`j1_filter.filter_regular_season`). Explicit team whitelist and the
    18/20-team season shape, same discipline as E0 (DESIGN_PHASE2.md 6.7)."""
    result = CheckResult()
    result.stats["rows"] = int(len(matches))
    result.stats["seasons"] = int(matches["season"].nunique())

    _check_unknown_teams_j1(matches, result)
    _check_j1_season_shape(matches, result)
    _check_results(matches, result)
    _check_duplicates(matches, result)
    _check_dates_range_only(matches, result)
    _check_overround(
        matches, result, ("psch", "pscd", "psca"),
        JPN_OVERROUND_PSC_MIN, JPN_OVERROUND_PSC_MAX, "psc",
    )
    _check_overround(
        matches, result, ("bfech", "bfecd", "bfeca"),
        JPN_OVERROUND_BFEC_MIN, JPN_OVERROUND_BFEC_MAX, "bfec",
    )
    return result


def _check_dates_range_only(matches: pd.DataFrame, result: CheckResult) -> None:
    """J1's Aug-openings/COVID-shuffled calendar does not fit E0's Aug1..Aug31
    per-season window, so only the overall range is recorded here -- the
    actual season/date-consistency question is `Season`-column drift, which
    `source_fd_new.season_label_drift` answers directly off the raw file."""
    result.stats["date_min"] = str(matches["date"].min().date())
    result.stats["date_max"] = str(matches["date"].max().date())


def format_result(result: CheckResult) -> str:
    s = result.stats
    lines = ["== footy check =="]
    lines.append(
        f"  rows            {s.get('rows', 0):,} "
        f"({s.get('seasons', 0)} seasons, div={s.get('divs', '-')})"
    )
    lines.append(
        f"  dates           {s.get('date_min', '-')} .. {s.get('date_max', '-')}"
    )
    lines.append(
        f"  teams           {s.get('distinct_teams', 0)} distinct, "
        f"{s.get('unknown_teams', 0)} unknown"
    )
    lines.append(
        f"  duplicates      {s.get('duplicates', 0)}   "
        f"FTR mismatches {s.get('ftr_mismatch', 0)}"
    )
    lines.append(
        f"  PSC rows        {s.get('psc_rows', 0):,} present / "
        f"{s.get('psc_missing', 0):,} missing"
    )
    if "overround_mean" in s:
        lines.append(
            f"  overround       mean {s['overround_mean']:.4f} "
            f"[p1 {s['overround_p01']:.4f} / p99 {s['overround_p99']:.4f}] "
            f"min {s['overround_min']:.4f} max {s['overround_max']:.4f}"
        )
        lines.append(
            f"  overround out   {s.get('overround_outside', 0)} rows outside "
            f"[{OVERROUND_MIN:.2f}, {OVERROUND_MAX:.2f}]"
        )
    if result.psc_missing is not None:
        missing = result.psc_missing[result.psc_missing["psc_missing"] > 0]
        if len(missing):
            lines.append("  PSC missing by season:")
            for row in missing.itertuples(index=False):
                lines.append(
                    f"    {row.season_label}  {row.psc_missing:>3}/{row.rows}"
                )
    for note in result.notes:
        lines.append(f"  note: {note}")
    for problem in result.problems:
        lines.append(f"  PROBLEM: {problem}")
    lines.append(f"  -> {'OK' if result.ok else 'FAILED'}")
    return "\n".join(lines)
