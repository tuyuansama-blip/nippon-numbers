"""Hyper-parameter search over the TUNE period, then freeze.

Why the TUNE/TEST boundary sits where it does: `PSCH/PSCD/PSCA` first appear
in 2012/13, so over 2000/01-2011/12 there is no market benchmark to peek at.
The search below *cannot* see the number it would later be judged against --
not by convention, but because the column does not exist (DESIGN.md 2.1).

Two consequences show up directly in the code here:

* the selection criterion is **mean RPS from a walk-forward run with the same
  protocol as the real one**, never in-sample likelihood. In-sample likelihood
  always picks xi -> 0, because no decay always fits the past better;
* the search is coarse-to-fine (half-life first at a fixed sigma, then sigma
  at the winning half-life) rather than a full product, which is what keeps
  the fit count in the low thousands.

`pi` is estimated separately: the average end-of-season (a, d) of sides in
their first season up, measured with the neutral prior so the estimate is not
circular.

This module is implemented but deliberately **not run** in the spike -- a full
grid is one to two hours. `footy tune` is the entry point when it is time.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import (
    DEFAULT_PI,
    DEFAULT_SIGMA,
    FROZEN_PARAMS_PATH,
    HALF_LIFE_GRID,
    RHO_BOUNDS,
    SCORE_K,
    SIGMA_GRID,
    TUNE_END,
    TUNE_START,
    WINDOW_SEASONS,
)
from footy.eval.metrics import rps_array
from footy.eval.walkforward import run_walkforward
from footy.model.dixon_coles import DixonColes


def _score_grid_point(
    matches: pd.DataFrame,
    *,
    season_from: int,
    season_to: int,
    refit: str,
    params: dict,
) -> dict:
    """One walk-forward pass; returns its mean RPS and log loss."""
    result = run_walkforward(
        matches,
        season_from=season_from,
        season_to=season_to,
        models=("dc",),
        refit=refit,
        params=params,
    )
    y = result.y
    probs = result.probs["dc"]
    seconds = [c for c in result.fits.columns if c.endswith("_seconds")]
    return {
        **params,
        "n": int(len(y)),
        "n_folds": int(len(result.fits)),
        "rps": float(np.mean(rps_array(probs, y))),
        "fit_seconds": float(result.fits[seconds].sum().sum()) if seconds else 0.0,
    }


def estimate_pi(
    matches: pd.DataFrame,
    *,
    season_from: int = TUNE_START,
    season_to: int = TUNE_END,
    half_life_days: float,
    sigma: float,
    window_seasons: int = WINDOW_SEASONS,
) -> tuple[tuple[float, float], pd.DataFrame]:
    """Mean end-of-season (a, d) of newly promoted sides over the TUNE period.

    Fitted with `pi = (0, 0)` on purpose: estimating the prior centre with the
    prior centre already in place would just reproduce whatever was assumed.
    """
    rows = []
    for season in range(int(season_from), int(season_to) + 1):
        season_matches = matches[matches["season"] == season]
        if season_matches.empty:
            continue
        asof = pd.Timestamp(season_matches["date"].max()) + pd.Timedelta(days=1)
        model = DixonColes(
            half_life_days=half_life_days,
            sigma=sigma,
            pi=(0.0, 0.0),
            window_seasons=window_seasons,
        )
        model.fit(matches, asof, season=season)
        strengths = model.strengths()
        strengths["season"] = season
        rows.append(strengths[strengths["promoted"]])
    if not rows:
        return DEFAULT_PI, pd.DataFrame()
    table = pd.concat(rows, ignore_index=True)
    return (
        (float(table["attack"].mean()), float(table["defence"].mean())),
        table,
    )


def tune(
    matches: pd.DataFrame,
    *,
    season_from: int = TUNE_START,
    season_to: int = TUNE_END,
    refit: str = "week",
    half_life_grid=HALF_LIFE_GRID,
    sigma_grid=SIGMA_GRID,
    base_sigma: float = DEFAULT_SIGMA,
    window_seasons: int = WINDOW_SEASONS,
    estimate_prior: bool = True,
    progress=None,
) -> dict:
    """Coarse-to-fine search. Returns the frozen-parameter payload."""
    if season_to > TUNE_END:
        raise ValueError(
            f"tuning may not reach past {TUNE_END} (the TUNE/TEST boundary); "
            f"got {season_to}"
        )

    stage1 = []
    for half_life in half_life_grid:
        point = _score_grid_point(
            matches,
            season_from=season_from,
            season_to=season_to,
            refit=refit,
            params={
                "half_life_days": float(half_life),
                "sigma": base_sigma,
                "pi": (0.0, 0.0),
                "window_seasons": window_seasons,
            },
        )
        stage1.append(point)
        if progress:
            progress("half_life", point)
    best_hl = min(stage1, key=lambda row: row["rps"])["half_life_days"]

    stage2 = []
    for sigma in sigma_grid:
        point = _score_grid_point(
            matches,
            season_from=season_from,
            season_to=season_to,
            refit=refit,
            params={
                "half_life_days": float(best_hl),
                "sigma": float(sigma),
                "pi": (0.0, 0.0),
                "window_seasons": window_seasons,
            },
        )
        stage2.append(point)
        if progress:
            progress("sigma", point)
    best_sigma = min(stage2, key=lambda row: row["rps"])["sigma"]

    pi = DEFAULT_PI
    pi_table = pd.DataFrame()
    if estimate_prior:
        pi, pi_table = estimate_pi(
            matches,
            season_from=season_from,
            season_to=season_to,
            half_life_days=best_hl,
            sigma=best_sigma,
            window_seasons=window_seasons,
        )

    total_fits = sum(row["n_folds"] for row in stage1 + stage2)
    return {
        "half_life_days": float(best_hl),
        "sigma": float(best_sigma),
        "pi": [float(pi[0]), float(pi[1])],
        "window_seasons": int(window_seasons),
        "rho_bounds": list(RHO_BOUNDS),
        "k_max": int(SCORE_K),
        "refit": refit,
        "tune_from": int(season_from),
        "tune_through": int(season_to),
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "total_fits": int(total_fits),
        "grid": {
            "half_life_days": [float(v) for v in half_life_grid],
            "sigma": [float(v) for v in sigma_grid],
            "base_sigma": float(base_sigma),
        },
        "stage1_half_life": stage1,
        "stage2_sigma": stage2,
        "pi_sample_size": int(len(pi_table)),
    }


def write_frozen_params(payload: dict, path: Path | str | None = None) -> Path:
    target = Path(path) if path else FROZEN_PARAMS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return target


def load_frozen_params(path: Path | str | None = None) -> dict | None:
    """The frozen payload, or None when tuning has not been run yet."""
    target = Path(path) if path else FROZEN_PARAMS_PATH
    if not target.exists():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if "pi" in payload:
        payload["pi"] = tuple(payload["pi"])
    return payload


def model_params(frozen: dict | None) -> dict:
    """The kwargs a model run should use, frozen ones if they exist."""
    from footy.config import DEFAULT_HALF_LIFE_DAYS

    if not frozen:
        return {
            "half_life_days": DEFAULT_HALF_LIFE_DAYS,
            "sigma": DEFAULT_SIGMA,
            "pi": tuple(DEFAULT_PI),
            "window_seasons": WINDOW_SEASONS,
            "source": "defaults (data/frozen_params.json absent -- tune not run)",
        }
    return {
        "half_life_days": float(frozen["half_life_days"]),
        "sigma": float(frozen["sigma"]),
        "pi": tuple(frozen.get("pi", DEFAULT_PI)),
        "window_seasons": int(frozen.get("window_seasons", WINDOW_SEASONS)),
        "source": (
            f"data/frozen_params.json (tuned {frozen.get('tune_from')}"
            f"-{frozen.get('tune_through')}, {frozen.get('created_at')})"
        ),
    }
