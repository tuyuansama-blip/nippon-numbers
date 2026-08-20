"""Known source-data anomalies, excluded row-by-row with a stated reason.

These are *not* parse regressions: each entry is a single row whose upstream
CSV carries one internally inconsistent odds triple (e.g. a PSCH far below
the same row's own MaxCH/AvgCH), pushing the overround outside the regression
band `footy check` guards. Widening the band would blunt the very check that
catches real parse regressions, and hand-editing `data/raw/*.csv` is futile
because a re-fetch overwrites it -- so the exclusion lives here, in code.

Discipline:

* every entry names exactly which odds columns are dropped and carries a
  mandatory human-readable `reason` -- what was excluded and why is always
  answerable;
* only the named columns are NaN-ed; the match row itself (result, the other
  odds generations) survives untouched;
* `apply_known_anomalies` reports every application, and `footy check`
  prints that breakdown -- nothing is suppressed silently.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class KnownAnomaly:
    div: str                   # e.g. "JPN1"
    date: str                  # "YYYY-MM-DD" (match date)
    home_team: str             # canonical team name (post-resolve)
    away_team: str
    columns: tuple[str, ...]   # odds columns to NaN on the matching row
    reason: str                # mandatory: what is wrong, with evidence

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(
                f"KnownAnomaly({self.div} {self.date} {self.home_team} v "
                f"{self.away_team}): reason must not be empty"
            )
        if not self.columns:
            raise ValueError(
                f"KnownAnomaly({self.div} {self.date} {self.home_team} v "
                f"{self.away_team}): columns must not be empty"
            )


KNOWN_ANOMALIES: tuple[KnownAnomaly, ...] = (
    KnownAnomaly(
        div="JPN1",
        date="2013-07-13",
        home_team="Vegalta Sendai",
        away_team="Iwata",
        columns=("psch", "pscd", "psca"),
        reason=(
            "source PSCH=2.00 contradicts the same row's own market columns "
            "(MaxCH=2.45, AvgCH=2.26); PSC overround 1.1077 breaches the "
            "[1.00, 1.10] band -- single-row error in new/JPN.csv"
        ),
    ),
    KnownAnomaly(
        div="JPN1",
        date="2024-11-22",
        home_team="Urawa Reds",
        away_team="Kawasaki Frontale",
        columns=("bfech", "bfecd", "bfeca"),
        reason=(
            "source BFECH=2.34 contradicts the same row's own market columns "
            "(PSCH=2.61, AvgCH=2.59, MaxCH=2.88); BFEC overround 1.0623 "
            "breaches the [0.98, 1.05] band -- single-row error in new/JPN.csv"
        ),
    ),
)


def apply_known_anomalies(
    matches: pd.DataFrame,
    anomalies: tuple[KnownAnomaly, ...] = KNOWN_ANOMALIES,
) -> tuple[pd.DataFrame, list[dict]]:
    """Return `(matches_copy, applied)` with the listed odds columns NaN-ed
    on each matching row.

    `applied` holds one dict per entry that matched at least one row --
    `n_rows` matched, `n_cleared` rows that actually still carried a value
    (zero when the exclusion was already applied upstream, e.g. at build
    time), plus the entry's own fields -- so callers can show exactly what
    was excluded and why. Idempotent: re-applying changes nothing.
    """
    if matches.empty:
        return matches, []
    out = matches.copy()
    dates = pd.to_datetime(out["date"]).dt.normalize()
    applied: list[dict] = []
    for anomaly in anomalies:
        mask = (
            (out["div"].astype("string") == anomaly.div)
            & (dates == pd.Timestamp(anomaly.date))
            & (out["home_team"].astype("string") == anomaly.home_team)
            & (out["away_team"].astype("string") == anomaly.away_team)
        )
        mask = mask.fillna(False)
        n_rows = int(mask.sum())
        if not n_rows:
            continue
        columns = [c for c in anomaly.columns if c in out.columns]
        n_cleared = int(out.loc[mask, columns].notna().any(axis=1).sum())
        out.loc[mask, columns] = np.nan
        applied.append({
            "div": anomaly.div,
            "date": anomaly.date,
            "home_team": anomaly.home_team,
            "away_team": anomaly.away_team,
            "columns": tuple(columns),
            "n_rows": n_rows,
            "n_cleared": n_cleared,
            "reason": anomaly.reason,
        })
    return out, applied
