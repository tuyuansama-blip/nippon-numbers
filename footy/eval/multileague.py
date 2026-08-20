"""Pool independent per-division walk-forward runs into one evaluation
(DESIGN_PHASE2.md 5.2-[1], 5.4).

OOS-LEAGUES is not one Dixon-Coles fit across 15 countries' clubs -- a
Belgian side and a Turkish side sharing one strength scale would be
meaningless. Each division gets its own model, its own `pi` (re-estimated
from that division's own pre-2012 history, `frozen_params.json`'s xi/sigma
kept exactly as tuned) and its own walk-forward loop; only the *scoring* is
pooled, into one set of paired differences and one CAL-1..4 calculation, with
the bootstrap resampling `(div, week_start)` blocks so two countries' matches
in the same calendar week are never treated as one correlated draw
(`walkforward.WalkforwardResult.blocks`).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from footy.config import WINDOW_SEASONS
from footy.eval.tune import estimate_pi
from footy.eval.walkforward import WalkforwardResult, run_walkforward


def run_multileague_walkforward(
    matches: pd.DataFrame,
    divs,
    *,
    season_from: int,
    season_to: int,
    pi_from: int,
    pi_to: int,
    frozen_params: dict,
    refit: str = "week",
    calibrate: str | None = "tb",
    progress=None,
) -> WalkforwardResult:
    """One `run_walkforward` per division in `divs`, pi re-estimated from
    each division's own `pi_from..pi_to` history, then concatenated.

    `progress(div, number, total, row)` is called once per fold per
    division if given; `div` lets a caller distinguish which league is
    currently running in a long OOS-LEAGUES pass.
    """
    half_life = float(frozen_params["half_life_days"])
    sigma = float(frozen_params["sigma"])
    window = int(frozen_params.get("window_seasons", WINDOW_SEASONS))

    results: list = []
    pis: dict[str, tuple[float, float]] = {}
    for div in divs:
        sub = matches[matches["div"] == div]
        if sub.empty:
            raise ValueError(f"no matches for div={div!r}")
        pi, pi_table = estimate_pi(
            sub, season_from=pi_from, season_to=pi_to,
            half_life_days=half_life, sigma=sigma, window_seasons=window,
        )
        pis[div] = pi
        params = {
            "half_life_days": half_life, "sigma": sigma, "pi": pi,
            "window_seasons": window,
        }

        def wrapped_progress(number, total, row, _div=div):
            if progress:
                progress(_div, number, total, row)

        result = run_walkforward(
            sub, season_from=season_from, season_to=season_to,
            models=("dc", "clim"), refit=refit, params=params,
            calibrate=calibrate, progress=wrapped_progress if progress else None,
        )
        results.append(result)

    frame = pd.concat([r.frame for r in results], ignore_index=True)
    leg_names = results[0].probs.keys()
    probs = {
        name: np.concatenate([r.probs[name] for r in results], axis=0)
        for name in leg_names
    }
    fits = pd.concat(
        [r.fits.assign(div=div) for div, r in zip(divs, results)],
        ignore_index=True,
    )
    combined_params = {
        "season_from": int(season_from), "season_to": int(season_to),
        "refit": refit, "models": ["dc", "clim"], "divs": list(divs),
        "calibrate": calibrate, "half_life_days": half_life, "sigma": sigma,
        "window_seasons": window, "pi_from": int(pi_from), "pi_to": int(pi_to),
        "pi_by_div": {d: list(p) for d, p in pis.items()},
    }
    return WalkforwardResult(
        frame=frame.reset_index(drop=True), probs=probs,
        fits=fits.reset_index(drop=True), params=combined_params,
    )
