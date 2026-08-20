"""The model interface every predictor in this spike implements.

Deliberately narrow, and narrow in two specific directions:

* `fit(matches, asof)` is asof-scoped. A model never receives the frame it is
  supposed to filter -- filtering to `date < asof` happens here, once, so no
  individual model can forget to do it (DESIGN.md 2.3-1).
* `predict_1x2(fixtures)` takes no market argument. A Benter-style blend with
  the market is out of scope for the spike, and the way to keep it out of
  scope is to make it structurally impossible to write (DESIGN.md 5).
"""

from __future__ import annotations

import abc

import numpy as np
import pandas as pd


class BaseModel(abc.ABC):
    """Fit on the past, emit H/D/A for a set of fixtures."""

    name = "base"

    @abc.abstractmethod
    def fit(self, matches: pd.DataFrame, asof, **kwargs) -> "BaseModel":
        """Fit on matches strictly before `asof`. Returns self."""

    @abc.abstractmethod
    def predict_1x2(self, fixtures: pd.DataFrame) -> np.ndarray:
        """(n, 3) array of (p_home, p_draw, p_away), rows summing to 1."""

    # -- helpers shared by every implementation -------------------------------
    @staticmethod
    def training_frame(matches: pd.DataFrame, asof) -> pd.DataFrame:
        """`date < asof`, played matches only. The single leak gate.

        Strict `<`, never `<=`: `Time` only exists from 2019/20, so a same-day
        fixture can never be ordered safely and is always excluded.
        """
        cut = pd.Timestamp(asof)
        frame = matches[matches["date"] < cut]
        return frame[frame["fthg"].notna() & frame["ftag"].notna()]
