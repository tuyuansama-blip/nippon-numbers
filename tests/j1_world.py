"""A synthetic, name-resolvable J1 world for the weekly-pipeline tests
(`footy/pipeline/predict.py`, `reconcile.py`, `publish.py`, `stamp.py`,
`weekly.py`).

Real J1 club names (`footy.data.teams.JPN_CANONICAL`) rather than
`tests.synth`'s generic `team_00`/`team_01` pool, because the pipeline under
test resolves team identity through the real crosswalks
(`teams_j1.resolve_odds_api_team`, `teams.team_id_j1`) and those are only
exercised honestly with names they actually know. Everything else about the
shape -- one warm-up season purely for training context, one "current"
season with a played prefix, one held-out matchday saved separately as
odds-snapshot events instead of matches -- mirrors `tests/synth.py`'s
`synth_league`, just built directly in the unified schema instead of
round-tripping through `source_fd_new.normalise_jpn`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from footy.config import JPN_DIV, JPN_LEAGUE_ID
from footy.data.teams import JPN_CANONICAL, team_id_j1

N_TEAMS = 20
WARMUP_MATCHDAYS = 2 * (N_TEAMS - 1)      # one full season, training-only
HISTORY_MATCHDAYS = 32                    # played, inside the "current" season
TEAMS = list(JPN_CANONICAL[:N_TEAMS])


def _round_robin(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Circle-method double round robin: `2(n-1)` rounds of `n/2` fixtures.
    Same construction as `tests/synth.py::_round_robin`, kept local so this
    module has no dependency beyond the team-identity table it is testing
    against."""
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


def _matchday_rows(rng, season, teams, start_date, n_matchdays, team_id):
    rounds = _round_robin(teams)[:n_matchdays]
    rows = []
    date = pd.Timestamp(start_date)
    for matchday in rounds:
        for home, away in matchday:
            hg = int(rng.poisson(1.4))
            ag = int(rng.poisson(1.1))
            ftr = "H" if hg > ag else ("A" if hg < ag else "D")
            margin = float(rng.uniform(1.03, 1.07))
            base = rng.dirichlet([4.0, 2.6, 3.2])
            price = 1.0 / (base * margin)
            rows.append({
                "match_id": f"JPN1_{date.strftime('%Y%m%d')}_{team_id[home]}_{team_id[away]}",
                "div": JPN_DIV, "league_id": JPN_LEAGUE_ID,
                "season": season, "season_label": str(season),
                "date": date, "time": "10:00",
                "week_start": date - pd.to_timedelta(int(date.weekday()), unit="D"),
                "home_team": home, "away_team": away,
                "home_id": team_id[home], "away_id": team_id[away],
                "fthg": float(hg), "ftag": float(ag), "ftr": ftr,
                "hthg": np.nan, "htag": np.nan, "htr": pd.NA,
                "psch": float(price[0]), "pscd": float(price[1]), "psca": float(price[2]),
                "bfech": np.nan, "bfecd": np.nan, "bfeca": np.nan,
                "xg_home": np.nan, "xg_away": np.nan, "empty_crowd": False,
            })
        date = date + pd.Timedelta(days=7)
    return rows, date


def build_world(*, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    team_id = {name: team_id_j1(name) for name in TEAMS}

    warmup_rows, _ = _matchday_rows(rng, 2013, TEAMS, "2013-03-01", WARMUP_MATCHDAYS, team_id)
    history_rows, next_date = _matchday_rows(
        rng, 2014, TEAMS, "2014-03-02", HISTORY_MATCHDAYS, team_id
    )
    matches_history = pd.DataFrame(warmup_rows + history_rows).sort_values(
        ["date", "home_team", "away_team"], kind="mergesort"
    ).reset_index(drop=True)

    # The held-out round: the next matchday in the same round-robin cycle,
    # kept out of `matches_history` and expressed as odds-snapshot events
    # instead -- exactly what `predict.next_round_fixtures` is meant to read.
    held_out = _round_robin(TEAMS)[HISTORY_MATCHDAYS]
    asof_utc = pd.Timestamp(next_date, tz="UTC") - pd.Timedelta(days=6)
    commence0 = pd.Timestamp(next_date, tz="UTC") + pd.Timedelta(hours=10)
    last_update = (asof_utc + pd.Timedelta(hours=1)).isoformat()

    events = []
    for i, (home, away) in enumerate(held_out):
        events.append({
            "id": f"evt{i:02d}",
            "sport_key": "soccer_japan_j_league",
            "commence_time": (commence0 + pd.Timedelta(minutes=30 * i)).isoformat(),
            "home_team": home, "away_team": away,
            "bookmakers": [{
                "key": "pinnacle", "last_update": last_update,
                "markets": [{
                    "key": "h2h", "last_update": last_update,
                    "outcomes": [
                        {"name": home, "price": 1.9},
                        {"name": "Draw", "price": 3.4},
                        {"name": away, "price": 4.1},
                    ],
                }],
            }],
        })

    return {
        "matches_history": matches_history,
        "teams": TEAMS,
        "team_id": team_id,
        "held_out_pairs": held_out,
        "events": events,
        "asof_utc": asof_utc,
        "next_round_commence0": commence0,
        "frozen": {
            "half_life_days": 200.0, "sigma": 0.35, "pi": [0.0, 0.0],
            "window_seasons": 6, "rho_bounds": [-0.2, 0.2], "k_max": 12,
        },
    }


def write_snapshot(events, path) -> None:
    import json
    from pathlib import Path

    Path(path).write_text(json.dumps(events), encoding="utf-8")
