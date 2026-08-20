"""Fold generation and the refit loop.

The expanding window is the honest one: at every fold the model sees every
match strictly before the fold's first kickoff and nothing else, truncated to
the last six seasons because beyond four half-lives the weights are under 6%
and only cost time (DESIGN.md 1.3, 2.2).

Two invariants are asserted rather than assumed, because both failure modes
are silent:

* a fold never splits a match day -- folds are keyed by date or by the Monday
  of the week, so every match on a day lands in the same fold;
* the last training date is strictly before the first test date.

All models in a run are fitted in the same loop on the same folds. That is
what makes the paired differences in `metrics.block_bootstrap` legitimate:
`dc`, `poisson` and `clim` are scored on an identical match set.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_PI,
    DEFAULT_SIGMA,
    TEST_END,
    TEST_START,
    WINDOW_SEASONS,
)
from footy.eval.metrics import encode_outcome
from footy.model.baseline import make_model
from footy.model.calibrate import OnlineTb
from footy.model.dixon_coles import DixonColes
from footy.odds.devig import market_probs, overround

REFIT_MODES = ("date", "week")
FRAME_COLUMNS = [
    "match_id", "div", "date", "season", "season_label", "week_start", "fold",
    "home_team", "away_team", "home_id", "away_id",
    "fthg", "ftag", "ftr", "y", "empty_crowd",
    "psch", "pscd", "psca", "overround",
    "market_shin_h", "market_shin_d", "market_shin_a",
    "market_mult_h", "market_mult_d", "market_mult_a",
]


@dataclass
class WalkforwardResult:
    frame: pd.DataFrame
    probs: dict[str, np.ndarray]
    fits: pd.DataFrame
    params: dict = field(default_factory=dict)

    @property
    def y(self) -> np.ndarray:
        return self.frame["y"].to_numpy(dtype="int64")

    @property
    def blocks(self) -> np.ndarray:
        """Bootstrap block label: (div, matchweek).

        Including `div` is a no-op for a single-division run (every block
        gets the same prefix, so the set of distinct blocks is unchanged)
        and is what DESIGN_PHASE2.md 5.2-[1] means by "ブロックは
        (div, week_start)" for the pooled OOS-LEAGUES run -- two divisions'
        matches in the same calendar week must never share a bootstrap
        block, or the resample would treat correlated-by-coincidence weeks
        from different countries as one draw.
        """
        if "div" in self.frame.columns:
            return (
                self.frame["div"].astype(str) + "|"
                + self.frame["week_start"].astype(str)
            ).to_numpy()
        return self.frame["week_start"].astype(str).to_numpy()

    def market(self, method: str = "shin") -> np.ndarray:
        prefix = "market_shin" if method == "shin" else "market_mult"
        return self.frame[[f"{prefix}_h", f"{prefix}_d", f"{prefix}_a"]].to_numpy(
            dtype="float64"
        )

    @property
    def has_market(self) -> np.ndarray:
        return np.isfinite(self.market("shin")).all(axis=1)


def save_run(result: WalkforwardResult, run_id: str, reports_dir=None) -> "Path":
    """Persist a run so `footy report <run_id>` can re-render it later."""
    from pathlib import Path

    from footy.config import REPORTS_DIR

    root = Path(reports_dir) if reports_dir else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    frame = result.frame.copy()
    for name, matrix in result.probs.items():
        for i, suffix in enumerate("hda"):
            frame[f"p_{name}_{suffix}"] = matrix[:, i]
    path = root / f"run_{run_id}.parquet"
    frame.to_parquet(path, index=False)
    result.fits.to_parquet(root / f"fits_{run_id}.parquet", index=False)
    (root / f"run_{run_id}.json").write_text(
        json.dumps(result.params, indent=2, default=str), encoding="utf-8"
    )
    return path


def load_run(run_id: str, reports_dir=None) -> WalkforwardResult:
    """Inverse of `save_run`."""
    from pathlib import Path

    from footy.config import REPORTS_DIR

    root = Path(reports_dir) if reports_dir else REPORTS_DIR
    path = root / f"run_{run_id}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no saved run at {path}")
    frame = pd.read_parquet(path)
    params = json.loads((root / f"run_{run_id}.json").read_text(encoding="utf-8"))
    fits_path = root / f"fits_{run_id}.parquet"
    fits = pd.read_parquet(fits_path) if fits_path.exists() else pd.DataFrame()

    probs = {}
    for column in frame.columns:
        if column.startswith("p_") and column.endswith("_h"):
            name = column[2:-2]
            probs[name] = frame[
                [f"p_{name}_h", f"p_{name}_d", f"p_{name}_a"]
            ].to_numpy(dtype="float64")
    keep = [c for c in frame.columns if not c.startswith("p_")]
    return WalkforwardResult(
        frame=frame[keep], probs=probs, fits=fits, params=params
    )


def make_folds(matches: pd.DataFrame, refit: str = "date") -> list[tuple]:
    """[(fold_key, sub_frame)] in chronological order.

    `date` gives one fold per match day, `week` one per Monday-anchored week.
    Either way a match day is wholly inside one fold.
    """
    if refit not in REFIT_MODES:
        raise ValueError(f"refit must be one of {REFIT_MODES}, got {refit!r}")
    key = "date" if refit == "date" else "week_start"
    folds = []
    for value, group in matches.groupby(key, sort=True):
        folds.append((pd.Timestamp(value), group.sort_values("date")))
    return folds


def check_fold_boundaries(folds) -> None:
    """A match day must belong to exactly one fold, and folds must advance."""
    seen: dict[_dt.date, object] = {}
    previous_end = None
    for key, group in folds:
        for day in group["date"].dt.date.unique():
            if day in seen and seen[day] != key:
                raise AssertionError(
                    f"match day {day} is split across folds {seen[day]} and {key}"
                )
            seen[day] = key
        start = group["date"].min()
        if previous_end is not None and start <= previous_end:
            raise AssertionError(
                f"fold {key} starts at {start.date()} but the previous fold "
                f"ended at {previous_end.date()}"
            )
        previous_end = group["date"].max()


def _build_models(names, params) -> dict:
    models = {}
    for name in names:
        models[name] = dict(
            half_life_days=params.get("half_life_days", DEFAULT_HALF_LIFE_DAYS),
            sigma=params.get("sigma", DEFAULT_SIGMA),
            pi=tuple(params.get("pi", DEFAULT_PI)),
            window_seasons=params.get("window_seasons", WINDOW_SEASONS),
        )
    return models


def run_walkforward(
    matches: pd.DataFrame,
    *,
    season_from: int = TEST_START,
    season_to: int = TEST_END,
    models=("dc", "clim"),
    refit: str = "date",
    params: dict | None = None,
    progress=None,
    calibrate: str | None = None,
    calibrate_target: str = "dc",
) -> WalkforwardResult:
    """Refit before every fold, predict the fold, accumulate.

    `calibrate="tb"` adds one more leg, `f"{calibrate_target}_cal"`
    (DESIGN_PHASE2.md 4): an `OnlineTb` instance sits outside the fold loop,
    predicts each fold with whatever `phi` its own history has produced so
    far, and only *afterwards* learns that fold's outcome -- the fold's
    result is already known at this point in the walk-forward (it is a
    backtest over played matches), but using it to fit before predicting
    would be exactly the leak the rest of this module exists to prevent, so
    the call order below is `predict` then `observe`, never the reverse.
    """
    params = dict(params or {})
    matches = matches.sort_values(["date", "home_team"], kind="mergesort")
    matches = matches.reset_index(drop=True)

    test = matches[
        (matches["season"] >= int(season_from))
        & (matches["season"] <= int(season_to))
        & matches["fthg"].notna()
    ]
    if test.empty:
        raise ValueError(
            f"no matches in seasons {season_from}..{season_to}"
        )

    folds = make_folds(test, refit)
    check_fold_boundaries(folds)

    kwargs = _build_models(models, params)
    warm: dict[str, DixonColes | None] = {name: None for name in models}
    collected: list[pd.DataFrame] = []
    leg_names = list(models)
    calibrator: OnlineTb | None = None
    if calibrate:
        if calibrate not in ("tb",):
            raise ValueError(f"unknown calibrate mode {calibrate!r}; only 'tb' exists")
        if calibrate_target not in models:
            raise ValueError(
                f"calibrate_target={calibrate_target!r} is not in models={models!r}"
            )
        calibrator = OnlineTb()
        leg_names = leg_names + [f"{calibrate_target}_cal"]
    predictions: dict[str, list[np.ndarray]] = {name: [] for name in leg_names}
    fit_rows: list[dict] = []

    for number, (key, fold) in enumerate(folds, start=1):
        asof = pd.Timestamp(fold["date"].min())
        train = matches[matches["date"] < asof]
        train = train[train["fthg"].notna()]
        if train.empty:
            raise ValueError(f"fold {key}: nothing to train on before {asof.date()}")
        assert train["date"].max() < asof, (
            f"fold {key}: training data reaches {train['date'].max()} "
            f"but the fold opens at {asof}"
        )

        row = {
            "fold": key,
            "n_test": int(len(fold)),
            "n_train": int(len(train)),
            "asof": asof,
        }
        for name in models:
            model = make_model(name, **kwargs[name])
            if isinstance(model, DixonColes):
                model.fit(matches, asof, warm_start=warm[name])
                warm[name] = model
                row[f"{name}_seconds"] = model.meta.seconds
                row[f"{name}_iters"] = model.meta.iterations
                row[f"{name}_evals"] = model.meta.func_evals
                row[f"{name}_rho"] = model.rho
                row[f"{name}_gamma"] = model.gamma
                row[f"{name}_mu0"] = model.mu0
                row[f"{name}_teams"] = model.meta.n_teams
                row[f"{name}_window"] = model.meta.n_matches
                row[f"{name}_tau_clipped"] = model.meta.tau_clipped
                row[f"{name}_converged"] = model.meta.converged
            else:
                model.fit(matches, asof)
            raw_pred = model.predict_1x2(fold)
            predictions[name].append(raw_pred)

            if calibrator is not None and name == calibrate_target:
                calibrated = calibrator.predict(asof, raw_pred)
                predictions[f"{name}_cal"].append(calibrated)
                fold_y = encode_outcome(fold["ftr"])
                calibrator.observe(raw_pred, fold_y)
                row["cal_n_history"] = calibrator.n_history
                row["cal_warm"] = calibrator.n_history >= calibrator.warmup
                row["cal_temperature"] = float(np.exp(calibrator.phi[0]))
                row["cal_phi_home"] = float(calibrator.phi[1])
                row["cal_phi_draw"] = float(calibrator.phi[2])

        fit_rows.append(row)
        collected.append(fold)
        if progress:
            progress(number, len(folds), row)

    frame = pd.concat(collected, ignore_index=True)
    fold_labels = np.concatenate(
        [np.full(len(fold), str(key.date())) for key, fold in folds]
    )
    frame["fold"] = fold_labels

    odds = frame[["psch", "pscd", "psca"]].to_numpy(dtype="float64")
    frame["overround"] = overround(odds)
    shin = market_probs(frame, "shin")
    mult = market_probs(frame, "multiplicative")
    for i, suffix in enumerate("hda"):
        frame[f"market_shin_{suffix}"] = shin[:, i]
        frame[f"market_mult_{suffix}"] = mult[:, i]

    frame["y"] = encode_outcome(frame["ftr"])
    frame = frame[[c for c in FRAME_COLUMNS if c in frame.columns]]

    probs = {name: np.vstack(values) for name, values in predictions.items()}
    for name, matrix in probs.items():
        assert len(matrix) == len(frame), f"{name}: prediction/frame length mismatch"

    return WalkforwardResult(
        frame=frame.reset_index(drop=True),
        probs=probs,
        fits=pd.DataFrame(fit_rows),
        params={
            "season_from": int(season_from),
            "season_to": int(season_to),
            "refit": refit,
            "models": list(models),
            **params,
        },
    )
