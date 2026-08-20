"""The two things Dixon-Coles has to beat before anything else matters.

`Climatology` knows nothing except how often home sides win. It is the zero
point of `gap_closed`: a model that cannot beat it has learned nothing at all
about football, and the whole scale in DESIGN.md 2.5 is anchored on it.

`SimplePoisson` is the ablation that isolates what the two extras buy --
it is the same model with the time decay removed and rho pinned at zero, so
the difference between it and `dc` is exactly "decay + low-score correction".
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from footy.config import CLIMATOLOGY_PRIOR, DEFAULT_HALF_LIFE_DAYS, WINDOW_SEASONS
from footy.model.base import BaseModel
from footy.model.dixon_coles import DixonColes, decay_rate, season_of


class Climatology(BaseModel):
    """Time-decayed rolling H/D/A base rates, refit honestly at every fold."""

    name = "clim"

    def __init__(
        self,
        *,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        window_seasons: int = WINDOW_SEASONS,
        prior: tuple[float, float, float] = CLIMATOLOGY_PRIOR,
        prior_weight: float = 20.0,
    ):
        self.half_life_days = float(half_life_days)
        self.xi = decay_rate(half_life_days)
        self.window_seasons = int(window_seasons)
        self.prior = np.asarray(prior, dtype="float64")
        self.prior_weight = float(prior_weight)
        self.rates = self.prior.copy()
        self.meta = {}

    def fit(self, matches: pd.DataFrame, asof, **kwargs) -> "Climatology":
        stamp = pd.Timestamp(asof)
        history = self.training_frame(matches, stamp)
        first_season = season_of(stamp) - self.window_seasons + 1
        window = history[history["season"] >= first_season]
        if window.empty:
            self.rates = self.prior.copy()
            return self

        age = (stamp - window["date"]).dt.days.to_numpy(dtype="float64")
        w = np.exp(-self.xi * age)
        outcome = window["ftr"].astype(str).to_numpy()
        counts = np.array(
            [w[outcome == label].sum() for label in ("H", "D", "A")],
            dtype="float64",
        )
        # A small Dirichlet-style prior keeps the very first folds sane.
        counts = counts + self.prior * self.prior_weight
        self.rates = counts / counts.sum()
        self.meta = {"n_matches": int(len(window)), "effective_n": float(w.sum())}
        return self

    def predict_1x2(self, fixtures: pd.DataFrame) -> np.ndarray:
        return np.tile(self.rates, (len(fixtures), 1))


class SimplePoisson(DixonColes):
    """Dixon-Coles minus the two extras: no time decay, rho == 0."""

    name = "poisson"

    def __init__(self, **kwargs):
        kwargs.pop("half_life_days", None)
        kwargs.pop("fit_rho", None)
        super().__init__(
            half_life_days=float("inf"), fit_rho=False, **kwargs
        )


MODELS = {
    "dc": DixonColes,
    "poisson": SimplePoisson,
    "clim": Climatology,
}


def make_model(name: str, **kwargs) -> BaseModel:
    """Build one of `dc` / `poisson` / `clim`, ignoring irrelevant kwargs."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; pick one of {sorted(MODELS)}")
    cls = MODELS[name]
    if cls is Climatology:
        allowed = {"half_life_days", "window_seasons", "prior", "prior_weight"}
        return cls(**{k: v for k, v in kwargs.items() if k in allowed})
    return cls(**kwargs)
