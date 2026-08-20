"""Raw season CSVs -> one normalised `data/matches.parquet`.

Three properties of the source files are load-bearing and are handled here
rather than being discovered later as a silent wrong answer:

1. **Mixed date formats.** Old seasons write 2-digit years, new ones 4-digit.
   `pd.to_datetime(..., dayfirst=True, format="mixed")` is the parse that was
   verified to handle both (DESIGN.md 6.1). Getting this wrong reorders the
   season and leaks the future into the training window without any error.
2. **Ragged rows.** 2003/04 and 2004/05 have rows with more fields than the
   header. Every extra field is empty; we assert that and truncate. Pandas'
   C parser simply refuses the file.
3. **Trailing blank rows.** 1995/96, 1996/97 and 1999/00 carry ~170 empty
   lines after the last match.

The frame keeps `Div` / `league_id` and reserves all-NaN `xg_home` /
`xg_away`, so adding a second division or an xG source later is a data-layer
change only (DESIGN.md 5).
"""

from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import (
    DEFAULT_DIVS,
    EMPTY_CROWD_END,
    EMPTY_CROWD_START,
    MATCHES_PATH,
    RAW_DIR,
    season_label,
)
from footy.data.teams import league_id, resolve_team

# Source column -> parquet column. Anything not listed is dropped.
_NUMERIC = ("fthg", "ftag", "hthg", "htag", "psch", "pscd", "psca")
_WANTED = {
    "Div": "div",
    "Date": "date_raw",
    "Time": "time",
    "HomeTeam": "home_team_raw",
    "AwayTeam": "away_team_raw",
    "FTHG": "fthg",
    "FTAG": "ftag",
    "FTR": "ftr",
    "HTHG": "hthg",
    "HTAG": "htag",
    "HTR": "htr",
    "PSCH": "psch",
    "PSCD": "pscd",
    "PSCA": "psca",
}

# `bfech/bfecd/bfeca` (Betfair exchange close) are reserved for J1 2024- --
# the mmz4281 files (E0 and the 15 OOS-LEAGUES divisions) never populate them
# (DESIGN_PHASE2.md 6.1), same all-NaN-reservation discipline as xg_home.
COLUMNS = [
    "match_id", "div", "league_id", "season", "season_label",
    "date", "time", "week_start",
    "home_team", "away_team", "home_id", "away_id",
    "fthg", "ftag", "ftr", "hthg", "htag", "htr",
    "psch", "pscd", "psca", "bfech", "bfecd", "bfeca",
    "xg_home", "xg_away",
    "empty_crowd",
]


def season_from_code(code: str) -> int:
    """'9596' -> 1995, '1213' -> 2012."""
    head = int(str(code)[:2])
    return 1900 + head if head >= 90 else 2000 + head


def _read_rows(path: Path) -> list[list[str]]:
    """Read a CSV that may be ragged, BOM-prefixed and latin-1 encoded."""
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(path, encoding=encoding, newline="") as handle:
                return list(csv.reader(handle))
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(  # pragma: no cover - latin-1 never fails
        "utf-8-sig", b"", 0, 1, f"cannot decode {path}"
    )


def read_raw_csv(path: Path | str) -> pd.DataFrame:
    """One raw season file -> a DataFrame with the source column names."""
    path = Path(path)
    rows = _read_rows(path)
    if not rows:
        return pd.DataFrame()
    header = [h.strip().lstrip("﻿") for h in rows[0]]
    width = len(header)

    records = []
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) > width:
            extra = [value.strip() for value in row[width:]]
            if any(extra):
                raise ValueError(
                    f"{path.name}:{lineno}: {len(row)} fields for a {width}-column "
                    f"header and the surplus is not blank: {extra!r}"
                )
            row = row[:width]
        elif len(row) < width:
            row = row + [""] * (width - len(row))
        records.append([value.strip() for value in row])

    frame = pd.DataFrame(records, columns=header)
    # Trailing blank lines: no division, no teams, nothing.
    keep = frame.get("HomeTeam", pd.Series(dtype="object")).astype("string").fillna("")
    frame = frame[keep.str.len() > 0].reset_index(drop=True)
    return frame


def normalise(frame: pd.DataFrame, season: int) -> pd.DataFrame:
    """Source columns -> the canonical schema, for one season."""
    out = pd.DataFrame(index=frame.index)
    for source, target in _WANTED.items():
        out[target] = frame[source] if source in frame.columns else pd.NA

    out["date"] = pd.to_datetime(out["date_raw"], dayfirst=True, format="mixed")
    out = out.drop(columns=["date_raw"])

    time = out["time"].astype("string").str.strip()
    out["time"] = time.where(time.notna() & (time.str.len() > 0), pd.NA)

    for column in _NUMERIC:
        out[column] = pd.to_numeric(out[column], errors="coerce")

    out["ftr"] = out["ftr"].astype("string").str.strip().str.upper()
    out["htr"] = out["htr"].astype("string").str.strip().str.upper()

    out["div"] = out["div"].astype("string").str.strip().str.upper()
    out["div"] = out["div"].where(out["div"].notna() & (out["div"] != ""), "E0")
    out["league_id"] = out["div"].map(league_id)

    out["season"] = int(season)
    out["season_label"] = season_label(int(season))

    div_value = str(out["div"].iloc[0]) if len(out) else "E0"
    for side in ("home", "away"):
        raw = out[f"{side}_team_raw"].astype("string").str.strip()
        resolved = raw.map(
            lambda name: resolve_team(div_value, name) if pd.notna(name)
            else (pd.NA, pd.NA)
        )
        out[f"{side}_team"] = [c for c, _ in resolved]
        out[f"{side}_id"] = [i for _, i in resolved]
        # Unknown spellings survive into the frame so `footy check` can name
        # them; they are never silently renamed. (Non-E0/JPN1 divisions
        # auto-register and never come back unresolved -- DESIGN_PHASE2.md 5.4.)
        out[f"{side}_team"] = out[f"{side}_team"].fillna(raw)
        out[f"{side}_id"] = out[f"{side}_id"].fillna(pd.NA)
    out = out.drop(columns=["home_team_raw", "away_team_raw"])

    out["bfech"] = np.nan
    out["bfecd"] = np.nan
    out["bfeca"] = np.nan

    # Monday of the match's week. This single definition is both the
    # bootstrap block (DESIGN.md 2.5) and the `--refit week` fold boundary,
    # which is why a fold boundary can never split a match day.
    days = out["date"].dt.weekday.astype("int64")
    out["week_start"] = out["date"] - pd.to_timedelta(days, unit="D")

    empty_start = pd.Timestamp(EMPTY_CROWD_START)
    empty_end = pd.Timestamp(EMPTY_CROWD_END)
    out["empty_crowd"] = (out["date"] >= empty_start) & (out["date"] <= empty_end)

    # Reserved for a future xG source; deliberately all-NaN for now.
    out["xg_home"] = np.nan
    out["xg_away"] = np.nan

    out["match_id"] = (
        out["div"].astype("string")
        + "_"
        + out["date"].dt.strftime("%Y%m%d")
        + "_"
        + out["home_id"].astype("string").fillna("unknown")
        + "_"
        + out["away_id"].astype("string").fillna("unknown")
    )
    return out[COLUMNS]


def build_matches(
    raw_dir: Path | str | None = None,
    *,
    divs=DEFAULT_DIVS,
    out_path: Path | str | None = None,
    write: bool = True,
    extra_frames=(),
) -> pd.DataFrame:
    """Every cached season -> one sorted frame, optionally written to parquet.

    `extra_frames` lets a division with its own loader (J1's `new/JPN.csv`,
    which is one file for 2012-present rather than one mmz4281 file per
    season) join the same `matches.parquet` without this function knowing
    anything about how it was produced -- it only has to already be in the
    unified schema (DESIGN_PHASE2.md 6, 10).
    """
    root = Path(raw_dir) if raw_dir else RAW_DIR
    frames = []
    for div in divs:
        for path in sorted(root.glob(f"{div}_*.csv")):
            season = season_from_code(path.stem.split("_")[-1])
            raw = read_raw_csv(path)
            if raw.empty:
                continue
            frames.append(normalise(raw, season))
    frames.extend(f for f in extra_frames if f is not None and not f.empty)
    if not frames:
        raise FileNotFoundError(f"no raw CSVs for {list(divs)} under {root}")

    matches = pd.concat(frames, ignore_index=True)
    matches = matches.sort_values(
        ["date", "div", "home_team", "away_team"], kind="mergesort"
    ).reset_index(drop=True)

    if write:
        target = Path(out_path) if out_path else MATCHES_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        matches.to_parquet(target, index=False)
    return matches


def load_matches(path: Path | str | None = None) -> pd.DataFrame:
    """Read the parquet built by `footy build`."""
    target = Path(path) if path else MATCHES_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"{target} not found -- run `./bin/footy fetch` then `./bin/footy build`"
        )
    matches = pd.read_parquet(target)
    return matches.sort_values(
        ["date", "div", "home_team", "away_team"], kind="mergesort"
    ).reset_index(drop=True)


def played(matches: pd.DataFrame) -> pd.DataFrame:
    """Rows with a full-time score -- the only rows the likelihood may see."""
    return matches[matches["fthg"].notna() & matches["ftag"].notna()]


def as_of(matches: pd.DataFrame, kickoff_date) -> pd.DataFrame:
    """Strictly `date < kickoff_date` (DESIGN.md 2.3-1).

    `Time` only exists from 2019/20, so same-day matches are always excluded
    rather than ordered within the day. Row-order `shift` is banned.
    """
    cut = pd.Timestamp(kickoff_date)
    if isinstance(kickoff_date, (_dt.date, _dt.datetime)):
        cut = pd.Timestamp(kickoff_date)
    return matches[matches["date"] < cut]
