"""Regular-season extraction for J1 (DESIGN_PHASE2.md 6.3).

`new/JPN.csv`'s J1 rows include championship/promotion play-offs, and those
play-offs mix in J2-vs-J2 fixtures (DESIGN_PHASE2.md 0.20: e.g. Tokushima vs
Montedio Yamagata in 2019, both J2 clubs). Left in, those sides' a/d would be
estimated from a handful of matches played at J2 strength inside a window
whose scale is set by J1, and the distortion is not isolated to those
clubs -- everyone they played against absorbs some of it too.

The rule below is mechanical, and it is verified against the full 2012-2025
history in `tests/test_j1_filter.py`: applied to every completed season it
reproduces exactly `N(N-1)` matches and `2(N-1)` appearances per club, for
`N in {18, 20}` (DESIGN_PHASE2.md 6.3).
"""

from __future__ import annotations

import pandas as pd

from footy.config import JPN_2026_27_MEMBERS, JPN_MEMBER_MIN_APPEARANCES


def regular_season_members(frame: pd.DataFrame, season: int) -> set[str]:
    """Clubs with >= `JPN_MEMBER_MIN_APPEARANCES` appearances in `season`.

    The in-progress 2026-27 season cannot use an appearance threshold -- too
    few matches have been played to separate "J1 club early in the season"
    from "J2 club that only played a handful of play-off fixtures" -- so its
    20-club membership is pinned explicitly instead (DESIGN_PHASE2.md 6.3;
    the same list is flagged in DESIGN_PHASE2.md 6.6 as the project's
    weakest-covered season for an unrelated reason, the missing 2025 J2 and
    "百年構想リーグ" history for the three promoted sides).
    """
    if season >= 2026:
        return set(JPN_2026_27_MEMBERS)
    part = frame[frame["season"] == season]
    counts = pd.concat([part["home_id"], part["away_id"]]).value_counts()
    return set(counts[counts >= JPN_MEMBER_MIN_APPEARANCES].index)


def filter_regular_season(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop play-off/non-league rows and duplicate fixtures, season by season.

    A duplicate fixture (the same two clubs meeting twice inside what should
    be a single-round-robin half, seen when a promotion play-off re-pairs two
    sides that already met in the regular phase) is resolved by keeping the
    earlier match, matching DESIGN_PHASE2.md 6.3's `drop_duplicates(...,
    keep="first")` exactly.
    """
    kept = []
    for season, part in frame.groupby("season", sort=True):
        members = regular_season_members(frame, int(season))
        keep = part[
            part["home_id"].isin(members) & part["away_id"].isin(members)
        ]
        keep = keep.sort_values("date").drop_duplicates(
            ["home_id", "away_id"], keep="first"
        )
        kept.append(keep)
    if not kept:
        return frame.iloc[0:0]
    return pd.concat(kept, ignore_index=True).sort_values(
        ["date", "home_team", "away_team"], kind="mergesort"
    ).reset_index(drop=True)
