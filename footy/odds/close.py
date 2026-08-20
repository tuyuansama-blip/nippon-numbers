"""Pseudo-close extraction: `odds_quotes.parquet` -> `odds_close.parquet`
(DESIGN_PHASE2.md 8.2, 8.3).

One row per event: Pinnacle if it has a quote before kickoff, otherwise the
simple-normalised median of `MEDIAN_BOOKS`' individually Shin-devigged
probabilities, otherwise missing -- never filled in (DESIGN_PHASE2.md 8.3).
Every row also carries `staleness_sec` and `close_quality`: a quote whose
gap to kickoff exceeds `JPN_STALENESS_MAX_SEC` (6h) is `'poor'` and excluded
from head-to-head comparison, but kept in the table with the reason visible.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import JPN_STALENESS_MAX_SEC
from footy.odds.devig import shin

# Cross-book coverage measured at 10/10 J1 events in a live snapshot
# (DESIGN_PHASE2.md 8.3). betfair_ex_eu/sport888/williamhill/betanysports are
# excluded from the median for the same reason -- too few events actually
# carry them to trust a median built partly from them.
MEDIAN_BOOKS = (
    "coolbet", "nordicbet", "unibet_fr", "betsson", "betclic_fr",
    "winamax_fr", "winamax_de", "pinnacle", "marathonbet", "onexbet",
    "betonlineag", "tipico_de", "pmu_fr", "leovegas_se", "unibet_se",
    "unibet_nl",
)

CLOSE_COLUMNS = [
    "event_id", "commence_time", "home_id", "away_id", "benchmark_source",
    "p_h", "p_d", "p_a", "book_last_update", "staleness_sec", "close_quality",
]


def _pre_kickoff_last_quote(quotes: pd.DataFrame) -> pd.DataFrame:
    """Per (event_id, book_key): the *three* H/D/A rows sharing the greatest
    `book_last_update` that is still `< commence_time` (DESIGN_PHASE2.md
    8.2-3) -- the closest thing to a "close" this source can offer.

    `idxmax` alone would pick a single row (one outcome), silently dropping
    the other two -- H/D/A are separate physical rows in the long-form
    quotes table, so the match has to be on the *value* of the max, not on
    a single row's index.
    """
    pre = quotes[quotes["book_last_update"] < quotes["commence_time"]]
    if pre.empty:
        return pre
    latest = pre.groupby(["event_id", "book_key"])["book_last_update"].transform("max")
    return pre[pre["book_last_update"] == latest]


def _book_probs(latest: pd.DataFrame) -> pd.DataFrame:
    """One row per (event_id, book_key): Shin-devigged (p_h, p_d, p_a)."""
    wide = latest.pivot_table(
        index=["event_id", "book_key", "commence_time", "book_last_update"],
        columns="outcome", values="price", aggfunc="first",
    ).reset_index()
    for col in ("H", "D", "A"):
        if col not in wide:
            wide[col] = np.nan
    odds = wide[["H", "D", "A"]].to_numpy(dtype="float64")
    probs, _ = shin(odds)
    wide["p_h"], wide["p_d"], wide["p_a"] = probs[:, 0], probs[:, 1], probs[:, 2]
    return wide


def build_close(quotes: pd.DataFrame) -> pd.DataFrame:
    """`odds_quotes.parquet` (long) -> one pseudo-close row per event."""
    latest = _pre_kickoff_last_quote(quotes)
    if latest.empty:
        return pd.DataFrame(columns=CLOSE_COLUMNS)
    per_book = _book_probs(latest)

    ids = (
        quotes[["event_id", "home_id", "away_id"]]
        .drop_duplicates("event_id")
        .set_index("event_id")
    )

    rows = []
    for event_id, group in per_book.groupby("event_id"):
        commence = group["commence_time"].iloc[0]
        home_id = ids.loc[event_id, "home_id"] if event_id in ids.index else None
        away_id = ids.loc[event_id, "away_id"] if event_id in ids.index else None

        pinnacle = group[group["book_key"] == "pinnacle"]
        if len(pinnacle) and np.isfinite(pinnacle[["p_h", "p_d", "p_a"]].to_numpy()).all():
            row = pinnacle.iloc[0]
            source = "pinnacle"
            p = row[["p_h", "p_d", "p_a"]].to_numpy(dtype="float64")
            last_update = row["book_last_update"]
        else:
            pool = group[group["book_key"].isin(MEDIAN_BOOKS)]
            pool = pool.dropna(subset=["p_h", "p_d", "p_a"])
            if pool.empty:
                continue
            median = pool[["p_h", "p_d", "p_a"]].median(axis=0).to_numpy(dtype="float64")
            p = median / median.sum()          # simple normalisation, not Shin again
            source = "median16"
            last_update = pool["book_last_update"].max()

        staleness = float((commence - last_update).total_seconds())
        quality = "poor" if staleness > JPN_STALENESS_MAX_SEC else "ok"
        rows.append({
            "event_id": event_id, "commence_time": commence,
            "home_id": home_id, "away_id": away_id, "benchmark_source": source,
            "p_h": float(p[0]), "p_d": float(p[1]), "p_a": float(p[2]),
            "book_last_update": last_update, "staleness_sec": staleness,
            "close_quality": quality,
        })
    return pd.DataFrame(rows, columns=CLOSE_COLUMNS).sort_values("commence_time").reset_index(drop=True)


def write_close(quotes_path=None, out_path=None) -> Path:
    from footy.config import DATA_DIR

    src = Path(quotes_path) if quotes_path else DATA_DIR / "odds_quotes.parquet"
    target = Path(out_path) if out_path else DATA_DIR / "odds_close.parquet"
    quotes = pd.read_parquet(src)
    close = build_close(quotes)
    target.parent.mkdir(parents=True, exist_ok=True)
    close.to_parquet(target, index=False)
    return target
