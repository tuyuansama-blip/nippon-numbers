"""Deterministic synthetic leagues, generated from a known Dixon-Coles truth.

Two jobs, and they are different jobs:

* **Ground truth.** Scores are drawn from the very model the fitter estimates,
  so `test_dc.py` can ask the only question that separates "the optimiser
  runs" from "the optimiser is right": do the recovered `a`, `d`, `gamma`,
  `rho` come back? DESIGN.md 6.4 names this as the first thing to check when
  a backtest disappoints.
* **A calendar with structure.** Real fixture lists spread a matchweek over
  Friday, Saturday and Sunday, and clubs are promoted and relegated. Both are
  reproduced here, because both are what the leak and fold-boundary tests are
  actually about.

It is a fixture, not a simulator. No conclusion about football may be drawn
from it, and nothing here touches the network or `data/`.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

from footy.model.scoreline import score_matrix

MATCHDAY_OFFSETS = (4, 5, 5, 5, 5, 5, 5, 6, 6, 6)   # Fri, 6x Sat, 3x Sun


def _first_matchweek_monday(year: int) -> _dt.date:
    """The Monday of the week containing the second Saturday of August."""
    day = _dt.date(year, 8, 8)
    while day.weekday() != 5:                        # Saturday
        day += _dt.timedelta(days=1)
    return day - _dt.timedelta(days=5)


def _round_robin(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Circle-method double round robin: 2(n-1) rounds of n/2 fixtures."""
    n = len(teams)
    rotation = list(teams)
    first_half = []
    for round_no in range(n - 1):
        pairs = []
        for i in range(n // 2):
            home, away = rotation[i], rotation[n - 1 - i]
            if (round_no + i) % 2:
                home, away = away, home
            pairs.append((home, away))
        first_half.append(pairs)
        rotation = [rotation[0]] + [rotation[-1]] + rotation[1:-1]
    second_half = [[(a, h) for h, a in pairs] for pairs in first_half]
    return first_half + second_half


def synth_league(
    *,
    seed: int = 11,
    n_seasons: int = 8,
    n_teams: int = 20,
    rotate: int = 3,
    start_year: int = 2000,
    mu0: float = 0.20,
    gamma: float = 0.26,
    rho: float = -0.13,
    attack_sd: float = 0.28,
    defence_sd: float = 0.22,
    promoted_attack: float = -0.20,
    promoted_defence: float = -0.16,
    margin: float = 0.024,
    market_blur: float = 0.0,
) -> tuple[pd.DataFrame, dict]:
    """A synthetic league in the `matches.parquet` schema, plus its truth.

    `rotate` clubs are relegated and replaced every summer; the incoming ones
    are drawn weaker by `promoted_*`, which is what gives `pi` something to
    estimate. `rotate=0` freezes the membership, which is what the parameter
    recovery test wants.
    """
    rng = np.random.default_rng(seed)
    pool_size = n_teams + rotate * n_seasons
    pool = [f"team_{i:02d}" for i in range(pool_size)]

    attack = dict(zip(pool, rng.normal(0.0, attack_sd, pool_size)))
    defence = dict(zip(pool, rng.normal(0.0, defence_sd, pool_size)))
    # Clubs that only ever arrive later are the promoted ones: make them worse.
    for name in pool[n_teams:]:
        attack[name] += promoted_attack
        defence[name] += promoted_defence

    rows = []
    current = list(pool[:n_teams])
    next_in = n_teams
    for season_index in range(n_seasons):
        year = start_year + season_index
        monday = _first_matchweek_monday(year)
        rounds = _round_robin(current)
        for week, pairs in enumerate(rounds):
            week_monday = monday + _dt.timedelta(days=7 * week)
            for slot, (home, away) in enumerate(pairs):
                offset = MATCHDAY_OFFSETS[slot % len(MATCHDAY_OFFSETS)]
                rows.append(
                    {
                        "season": year,
                        "date": pd.Timestamp(week_monday
                                             + _dt.timedelta(days=offset)),
                        "home_id": home,
                        "away_id": away,
                    }
                )
        if rotate:
            current = current[rotate:] + pool[next_in : next_in + rotate]
            next_in += rotate

    frame = pd.DataFrame(rows)
    lam = np.exp(
        mu0 + gamma
        + frame["home_id"].map(attack).to_numpy()
        - frame["away_id"].map(defence).to_numpy()
    )
    mu = np.exp(
        mu0
        + frame["away_id"].map(attack).to_numpy()
        - frame["home_id"].map(defence).to_numpy()
    )

    matrix = score_matrix(lam, mu, rho)
    size = matrix.shape[1]
    flat = matrix.reshape(len(lam), -1)
    draw = rng.random(len(lam))[:, None]
    cell = (draw > flat.cumsum(axis=1)).sum(axis=1)
    frame["fthg"] = (cell // size).astype("int64")
    frame["ftag"] = (cell % size).astype("int64")
    frame["ftr"] = np.where(
        frame["fthg"] > frame["ftag"], "H",
        np.where(frame["fthg"] < frame["ftag"], "A", "D"),
    )

    # True 1X2, then a bookmaker's version of it: blurred, then marked up.
    rows_ = np.arange(size)
    truth_probs = np.stack(
        [
            (matrix * (rows_[:, None] > rows_[None, :])).sum(axis=(1, 2)),
            (matrix * (rows_[:, None] == rows_[None, :])).sum(axis=(1, 2)),
            (matrix * (rows_[:, None] < rows_[None, :])).sum(axis=(1, 2)),
        ],
        axis=1,
    )
    market = truth_probs
    if market_blur:
        noisy = np.log(market) + rng.normal(0.0, market_blur, market.shape)
        market = np.exp(noisy)
        market /= market.sum(axis=1, keepdims=True)
    prices = market * (1.0 + margin)
    with np.errstate(divide="ignore"):
        odds = 1.0 / prices
    frame["psch"], frame["pscd"], frame["psca"] = odds[:, 0], odds[:, 1], odds[:, 2]

    frame["div"] = "E0"
    frame["league_id"] = "eng_1"
    frame["season_label"] = frame["season"].map(lambda y: f"{y}-{(y + 1) % 100:02d}")
    frame["home_team"] = frame["home_id"]
    frame["away_team"] = frame["away_id"]
    frame["time"] = pd.NA
    frame["hthg"] = 0
    frame["htag"] = 0
    frame["htr"] = "D"
    frame["xg_home"] = np.nan
    frame["xg_away"] = np.nan
    frame["empty_crowd"] = False
    days = frame["date"].dt.weekday.astype("int64")
    frame["week_start"] = frame["date"] - pd.to_timedelta(days, unit="D")
    frame["match_id"] = (
        frame["div"] + "_" + frame["date"].dt.strftime("%Y%m%d")
        + "_" + frame["home_id"] + "_" + frame["away_id"]
    )

    frame = frame.sort_values(
        ["date", "home_id", "away_id"], kind="mergesort"
    ).reset_index(drop=True)

    truth = {
        "mu0": mu0,
        "gamma": gamma,
        "rho": rho,
        "attack": attack,
        "defence": defence,
        "teams": pool,
        "probs": truth_probs,
        "promoted_attack": promoted_attack,
        "promoted_defence": promoted_defence,
    }
    return frame, truth


def synth_odds(n: int = 200, *, seed: int = 5, margin: float = 0.05):
    """Random 3-way prices with a known overround, for the de-vig tests."""
    rng = np.random.default_rng(seed)
    p = rng.dirichlet([4.0, 3.0, 3.0], size=n)
    return 1.0 / (p * (1.0 + margin)), p
