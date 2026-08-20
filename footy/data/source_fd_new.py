"""J1 loader: football-data.co.uk's `new/JPN.csv` (DESIGN_PHASE2.md 6).

Structurally a different file from the mmz4281 season-per-file layout every
other division in this project uses: one CSV holds 2012 to present, the
schema is `Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSC*,Max*,
Avg*,BFEC*,B365C*` rather than `Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,
PSC*`, and the `Season` column is not trustworthy (DESIGN_PHASE2.md 6.4).
Everything below turns it into exactly the shared `matches.parquet` schema so
the rest of the pipeline -- fitting, walk-forward, `check` -- never has to
know J1 came from a different source or a different file layout.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import JPN_DIV, JPN_LEAGUE_ID, JPN_RAW_FILENAME, JPN_URL
from footy.config import jpn_season_label, jpn_season_of
from footy.data.teams import resolve_team

# Betfair exchange close joins the benchmark from 2024 (DESIGN_PHASE2.md 6.1);
# B365C is read in case BFEC is ever missing for a stretch, but DESIGN_PHASE2
# explicitly does not adopt it as a benchmark, so it is not carried through.
_SOURCE_NUMERIC = ("HG", "AG", "PSCH", "PSCD", "PSCA", "BFECH", "BFECD", "BFECA")

COLUMNS = [
    "match_id", "div", "league_id", "season", "season_label",
    "date", "time", "week_start",
    "home_team", "away_team", "home_id", "away_id",
    "fthg", "ftag", "ftr", "hthg", "htag", "htr",
    "psch", "pscd", "psca", "bfech", "bfecd", "bfeca",
    "xg_home", "xg_away",
    "empty_crowd",
]


def raw_path(raw_dir) -> Path:
    return Path(raw_dir) / JPN_RAW_FILENAME


def fetch_jpn_csv(raw_dir, *, fetcher=None) -> Path:
    """Download `new/JPN.csv` if it is not already cached. One request, ever
    -- the whole 2012-present history is one file (DESIGN_PHASE2.md 6.1)."""
    root = Path(raw_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = raw_path(root)
    if target.exists() and target.stat().st_size > 0:
        return target
    from footy.data.download import Fetcher

    client = fetcher or Fetcher()
    body = client.get(JPN_URL)
    if body is None:
        raise RuntimeError(f"{JPN_URL} returned 404")
    target.write_bytes(body)
    return target


def read_raw_jpn(path: Path | str) -> pd.DataFrame:
    """The raw file, BOM-and-encoding tolerant like `load.read_raw_csv`."""
    path = Path(path)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(  # pragma: no cover - latin-1 never fails
        "utf-8-sig", b"", 0, 1, f"cannot decode {path}"
    )


def normalise_jpn(frame: pd.DataFrame) -> pd.DataFrame:
    """The raw `new/JPN.csv` frame -> the unified matches schema.

    This is deliberately independent of `load.normalise`: the two source
    schemas share almost no column names, and folding them into one function
    would just hide the difference behind a pile of `if div == ...` branches.
    """
    if frame.empty:
        return pd.DataFrame(columns=COLUMNS)

    out = pd.DataFrame(index=frame.index)
    out["date"] = pd.to_datetime(frame["Date"], dayfirst=True, format="mixed")

    time = frame["Time"].astype("string").str.strip() if "Time" in frame else pd.NA
    out["time"] = time

    out["fthg"] = pd.to_numeric(frame["HG"], errors="coerce")
    out["ftag"] = pd.to_numeric(frame["AG"], errors="coerce")
    out["ftr"] = frame["Res"].astype("string").str.strip().str.upper()
    out["hthg"] = np.nan
    out["htag"] = np.nan
    out["htr"] = pd.NA

    for source, target in (
        ("PSCH", "psch"), ("PSCD", "pscd"), ("PSCA", "psca"),
        ("BFECH", "bfech"), ("BFECD", "bfecd"), ("BFECA", "bfeca"),
    ):
        out[target] = pd.to_numeric(frame.get(source), errors="coerce")

    # `League` has a stray leading space on some rows ("Japan, J1 League" --
    # the comma sits inside `Country`, not between `Country` and `League`).
    # It is checked, not silently stripped-and-ignored: any other value here
    # means a cup competition or a second league slipped into the file, which
    # `filter_regular_season` is not built to catch (DESIGN_PHASE2.md 6.4).
    league = frame["League"].astype("string").str.strip()
    bad = sorted(set(league.dropna().unique()) - {"J1 League"})
    if bad:
        raise ValueError(f"unexpected League values in new/JPN.csv: {bad}")

    out["div"] = JPN_DIV
    out["league_id"] = JPN_LEAGUE_ID
    out["season"] = out["date"].map(jpn_season_of).astype("int64")
    out["season_label"] = out["season"].map(jpn_season_label)

    for side, source in (("home", "Home"), ("away", "Away")):
        raw = frame[source].astype("string").str.strip()
        resolved = raw.map(
            lambda name: resolve_team(JPN_DIV, name) if pd.notna(name)
            else (pd.NA, pd.NA)
        )
        out[f"{side}_team"] = [c for c, _ in resolved]
        out[f"{side}_id"] = [i for _, i in resolved]
        out[f"{side}_team"] = out[f"{side}_team"].fillna(raw)
        out[f"{side}_id"] = out[f"{side}_id"].fillna(pd.NA)

    days = out["date"].dt.weekday.astype("int64")
    out["week_start"] = out["date"] - pd.to_timedelta(days, unit="D")
    out["empty_crowd"] = False          # J1 never had a behind-closed-doors window
    out["xg_home"] = np.nan
    out["xg_away"] = np.nan

    out["match_id"] = (
        "JPN1_" + out["date"].dt.strftime("%Y%m%d") + "_"
        + out["home_id"].astype("string").fillna("unknown") + "_"
        + out["away_id"].astype("string").fillna("unknown")
    )
    return out[COLUMNS]


def season_label_drift(raw_frame: pd.DataFrame) -> int:
    """Rows where the source's own `Season` disagrees with the date-derived
    season (DESIGN_PHASE2.md 6.4, 6.7). A `footy check --league jpn1`
    warning, never expected to hit zero -- if it ever does, the upstream
    `Season` convention changed and `jpn_season_of` should be revisited."""
    dates = pd.to_datetime(raw_frame["Date"], dayfirst=True, format="mixed")
    derived = dates.map(jpn_season_of)
    raw_season = pd.to_numeric(
        raw_frame["Season"].astype("string").str.split("/").str[0], errors="coerce"
    )
    return int((raw_season != derived).sum())


def build_j1_matches(raw_dir=None, *, fetcher=None) -> pd.DataFrame:
    """Fetch (if needed) and normalise `new/JPN.csv` -> the unified schema.

    Does **not** drop play-off/non-league rows -- that is
    `j1_filter.filter_regular_season`'s job, run separately so a `check` can
    see the raw file's shape before anything is thrown away.
    """
    from footy.config import RAW_DIR

    root = Path(raw_dir) if raw_dir else RAW_DIR
    path = fetch_jpn_csv(root, fetcher=fetcher)
    raw = read_raw_jpn(path)
    frame = normalise_jpn(raw)
    # Known single-row source anomalies are dropped column-wise here, at load
    # time, never by editing the raw CSV (a re-fetch would overwrite it) --
    # see footy/data/known_anomalies.py for the list, each with its reason.
    from footy.data.known_anomalies import apply_known_anomalies

    frame, _ = apply_known_anomalies(frame)
    return frame.sort_values(
        ["date", "home_team", "away_team"], kind="mergesort"
    ).reset_index(drop=True)
