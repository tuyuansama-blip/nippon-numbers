"""The Dixon-Coles fitter (DESIGN.md 1).

`test_analytic_gradient_matches_*` is the single most important test in the
repository. A wrong gradient does not raise -- L-BFGS-B simply stops
somewhere else, the backtest still produces a number, and every conclusion
downstream is quietly about a model nobody fitted. DESIGN.md 1.2 calls the
analytic gradient the key performance decision; this is what makes it safe
to rely on.

`test_recovers_the_synthetic_truth` is the second: DESIGN.md 6.4 says that
when `gap_closed` disappoints, the first suspicion is the implementation, and
truth recovery on synthetic data is the way to rule it in or out.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.optimize import check_grad

from footy.model.baseline import Climatology, SimplePoisson
from footy.model.dixon_coles import (
    DixonColes,
    decay_rate,
    objective_and_grad,
    season_of,
)
from footy.model.scoreline import probs_1x2, score_matrix
from tests.synth import synth_league


def _problem(seed: int = 0, n_teams: int = 12, n_matches: int = 900,
             weighted: bool = False, prior: bool = False):
    """A small likelihood problem plus a random point to differentiate at."""
    rng = np.random.default_rng(seed)
    home = rng.integers(0, n_teams, n_matches)
    away = (home + 1 + rng.integers(0, n_teams - 1, n_matches)) % n_teams
    # Plenty of 0-0, 1-0, 0-1 and 1-1 so every tau branch is exercised.
    x = rng.poisson(1.5, n_matches).astype("float64")
    y = rng.poisson(1.2, n_matches).astype("float64")
    w = rng.uniform(0.2, 1.0, n_matches) if weighted else np.ones(n_matches)
    prior_a = rng.normal(0.0, 0.1, n_teams) if prior else np.zeros(n_teams)
    prior_d = rng.normal(0.0, 0.1, n_teams) if prior else np.zeros(n_teams)
    theta = np.concatenate(
        [
            [rng.normal(0.2, 0.1), rng.normal(0.25, 0.1)],
            rng.normal(0.0, 0.25, n_teams),
            rng.normal(0.0, 0.25, n_teams),
            [rng.uniform(-0.15, 0.15)],
        ]
    )
    args = (home, away, x, y, w, prior_a, prior_d, 0.35, n_teams)
    return theta, args


def _analytic(theta, args):
    return objective_and_grad(theta, *args)[1]


def _numeric(theta, args, h: float = 1e-5):
    """Central differences -- far more accurate than a forward difference."""
    grad = np.zeros_like(theta)
    for i in range(len(theta)):
        up, down = theta.copy(), theta.copy()
        up[i] += h
        down[i] -= h
        grad[i] = (
            objective_and_grad(up, *args)[0] - objective_and_grad(down, *args)[0]
        ) / (2 * h)
    return grad


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_analytic_gradient_matches_central_differences(seed):
    theta, args = _problem(seed=seed)
    analytic = _analytic(theta, args)
    numeric = _numeric(theta, args)
    scale = max(np.linalg.norm(analytic), 1.0)
    assert np.linalg.norm(analytic - numeric) / scale < 1e-6


@pytest.mark.parametrize("weighted,prior", [(True, False), (False, True), (True, True)])
def test_analytic_gradient_with_weights_and_shifted_priors(weighted, prior):
    """Time decay and a promoted side's prior centre must not break it."""
    theta, args = _problem(seed=9, weighted=weighted, prior=prior)
    analytic = _analytic(theta, args)
    numeric = _numeric(theta, args)
    scale = max(np.linalg.norm(analytic), 1.0)
    assert np.linalg.norm(analytic - numeric) / scale < 1e-6


def test_analytic_gradient_matches_scipy_check_grad():
    theta, args = _problem(seed=17)
    error = check_grad(
        lambda t: objective_and_grad(t, *args)[0],
        lambda t: objective_and_grad(t, *args)[1],
        theta,
    )
    norm = np.linalg.norm(_analytic(theta, args))
    assert error / norm < 1e-4          # forward differences are this coarse


def test_gradient_is_exact_for_each_tau_branch():
    """Each of the four corrected cells separately, with nothing else around."""
    for x_goal, y_goal in ((0, 0), (0, 1), (1, 0), (1, 1), (2, 3)):
        home = np.array([0, 1])
        away = np.array([1, 0])
        x = np.array([float(x_goal), 1.0])
        y = np.array([float(y_goal), 2.0])
        args = (home, away, x, y, np.ones(2), np.zeros(2), np.zeros(2), 0.5, 2)
        theta = np.array([0.15, 0.3, 0.1, -0.2, 0.05, -0.1, -0.12])
        analytic = _analytic(theta, args)
        numeric = _numeric(theta, args, h=1e-6)
        assert np.allclose(analytic, numeric, rtol=1e-5, atol=1e-6), (x_goal, y_goal)


def test_objective_penalty_pulls_towards_the_prior_centre():
    theta, args = _problem(seed=5)
    home, away, x, y, w, prior_a, prior_d, sigma, n = args
    loose = objective_and_grad(theta, home, away, x, y, w, prior_a, prior_d, 10.0, n)[0]
    tight = objective_and_grad(theta, home, away, x, y, w, prior_a, prior_d, 0.05, n)[0]
    assert tight > loose         # the same theta costs more under a tight prior


def test_recovers_the_synthetic_truth(stable_league):
    """Known a, d, gamma, rho must come back from data the model generated."""
    matches, truth = stable_league
    asof = matches["date"].max() + pd.Timedelta(days=1)
    model = DixonColes(
        half_life_days=float("inf"), sigma=2.0, window_seasons=99
    ).fit(matches, asof)
    assert model.meta.converged
    assert model.meta.n_matches == len(matches)

    fitted = model.strengths().set_index("team_id")
    true_a = np.array([truth["attack"][t] for t in fitted.index])
    true_d = np.array([truth["defence"][t] for t in fitted.index])
    got_a = fitted["attack"].to_numpy()
    got_d = fitted["defence"].to_numpy()

    # The ridge leaves the level free, so compare after centring both.
    got_a = got_a - got_a.mean()
    got_d = got_d - got_d.mean()
    true_a = true_a - true_a.mean()
    true_d = true_d - true_d.mean()

    assert np.corrcoef(true_a, got_a)[0, 1] > 0.95
    assert np.corrcoef(true_d, got_d)[0, 1] > 0.90
    assert np.max(np.abs(true_a - got_a)) < 0.12
    assert np.max(np.abs(true_d - got_d)) < 0.12
    assert model.gamma == pytest.approx(truth["gamma"], abs=0.05)
    assert model.rho == pytest.approx(truth["rho"], abs=0.05)


def test_recovered_rates_reproduce_the_true_goal_means(stable_league):
    matches, truth = stable_league
    asof = matches["date"].max() + pd.Timedelta(days=1)
    model = DixonColes(
        half_life_days=float("inf"), sigma=2.0, window_seasons=99
    ).fit(matches, asof)
    lam, mu = model.rates(matches)
    assert lam.mean() == pytest.approx(matches["fthg"].mean(), rel=0.05)
    assert mu.mean() == pytest.approx(matches["ftag"].mean(), rel=0.05)


def test_rho_is_clamped_to_its_bounds(league_matches):
    asof = pd.Timestamp("2005-01-10")
    model = DixonColes(rho_bounds=(-0.02, -0.01)).fit(league_matches, asof)
    assert -0.02 - 1e-9 <= model.rho <= -0.01 + 1e-9
    assert model.meta.rho_at_bound


def test_tau_clipping_is_counted_not_silent():
    """The floor exists so log(tau) cannot explode; the count is the alarm."""
    home = np.array([0, 1])
    away = np.array([1, 0])
    x = np.array([0.0, 0.0])
    y = np.array([0.0, 0.0])
    counter: dict = {}
    # rho far outside its bounds makes 1 - lam*mu*rho negative for a 0-0.
    theta = np.array([1.5, 0.4, 0.0, 0.0, 0.0, 0.0, 5.0])
    objective_and_grad(
        theta, home, away, x, y, np.ones(2), np.zeros(2), np.zeros(2), 1.0, 2,
        counter,
    )
    assert counter["tau_clipped"] == 2


def test_warm_start_reaches_the_same_optimum_with_fewer_iterations(league_matches):
    cold = DixonColes().fit(league_matches, pd.Timestamp("2005-01-10"))
    later = pd.Timestamp("2005-01-24")
    from_scratch = DixonColes().fit(league_matches, later)
    warmed = DixonColes().fit(league_matches, later, warm_start=cold)

    assert warmed.meta.warm_started and not from_scratch.meta.warm_started
    assert warmed.meta.iterations <= from_scratch.meta.iterations
    assert warmed.rho == pytest.approx(from_scratch.rho, abs=1e-3)
    fixtures = league_matches[league_matches["date"] == later].head(5)
    if len(fixtures):
        assert np.allclose(
            warmed.predict_1x2(fixtures),
            from_scratch.predict_1x2(fixtures),
            atol=2e-3,
        )


def test_unseen_team_falls_back_to_the_prior_centre(league_matches):
    """A side with no matches has no pull from the likelihood, so its
    parameters are exactly the prior centre (DESIGN.md 1.4)."""
    model = DixonColes(pi=(-0.3, -0.25)).fit(
        league_matches, pd.Timestamp("2005-01-10")
    )
    assert model.team_params("a_team_that_never_played") == (-0.3, -0.25)


def test_promoted_prior_moves_the_forecast(league_matches):
    """pi must actually do something, and in the direction it claims to."""
    asof = pd.Timestamp("2005-08-20")
    neutral = DixonColes(pi=(0.0, 0.0)).fit(league_matches, asof)
    pessimistic = DixonColes(pi=(-0.4, -0.35)).fit(league_matches, asof)
    assert neutral.promoted, "the synthetic league must promote somebody"

    promoted = sorted(neutral.promoted)[0]
    fixture = pd.DataFrame(
        {"home_id": ["team_00"], "away_id": [promoted]}
    )
    weak_away = pessimistic.predict_1x2(fixture)[0]
    even_away = neutral.predict_1x2(fixture)[0]
    assert weak_away[0] > even_away[0]      # home win likelier vs a weak side
    assert weak_away[2] < even_away[2]


def test_promoted_detection_uses_only_the_previous_season(league_matches):
    model = DixonColes()
    history = league_matches[league_matches["season"] <= 2004]
    promoted = model.promoted_teams(history, 2004)
    previous = league_matches[league_matches["season"] == 2003]
    seen = set(previous["home_id"]) | set(previous["away_id"])
    assert promoted and not (promoted & seen)


def test_window_truncation_drops_older_seasons(league_matches):
    model = DixonColes(window_seasons=3).fit(
        league_matches, pd.Timestamp("2006-01-10")
    )
    assert model.meta.window_from_season == 2003
    assert model.meta.n_matches < len(
        league_matches[league_matches["date"] < pd.Timestamp("2006-01-10")]
    )


def test_time_decay_weights_recent_matches_more():
    assert decay_rate(float("inf")) == 0.0
    assert decay_rate(365.0) == pytest.approx(np.log(2) / 365.0)
    with pytest.raises(ValueError):
        decay_rate(-5.0)


def test_season_of_handles_the_covid_tail():
    assert season_of(pd.Timestamp("2020-07-20")) == 2019
    assert season_of(pd.Timestamp("2020-09-12")) == 2020
    assert season_of(pd.Timestamp("2021-05-23")) == 2020


def test_predictions_are_a_distribution(league_matches):
    model = DixonColes().fit(league_matches, pd.Timestamp("2005-01-10"))
    fixtures = league_matches[league_matches["season"] == 2004].head(50)
    probs = model.predict_1x2(fixtures)
    assert probs.shape == (50, 3)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert np.all(probs > 0)


def test_empty_fixture_list_is_allowed(league_matches):
    model = DixonColes().fit(league_matches, pd.Timestamp("2005-01-10"))
    assert model.predict_1x2(league_matches.head(0)).shape == (0, 3)


# --- scoreline ---------------------------------------------------------------
def test_score_matrix_is_a_distribution():
    matrix = score_matrix(np.array([1.6, 0.9]), np.array([1.2, 2.4]), -0.13)
    assert matrix.shape == (2, 13, 13)
    assert np.allclose(matrix.sum(axis=(1, 2)), 1.0)
    assert np.all(matrix >= 0)


def test_rho_zero_is_the_independent_poisson_product():
    lam, mu = 1.6, 1.25
    matrix = score_matrix(lam, mu, 0.0)[0]
    from scipy.stats import poisson

    expected = np.outer(
        poisson.pmf(np.arange(13), lam), poisson.pmf(np.arange(13), mu)
    )
    expected /= expected.sum()
    assert np.allclose(matrix, expected, atol=1e-12)


def test_negative_rho_lifts_the_draw_probability():
    """The sign convention: rho < 0 boosts 0-0 and 1-1, i.e. draws."""
    plain = probs_1x2(1.5, 1.2, 0.0)[0]
    corrected = probs_1x2(1.5, 1.2, -0.13)[0]
    assert corrected[1] > plain[1]
    assert probs_1x2(1.5, 1.2, 0.13)[0][1] < plain[1]


def test_probs_1x2_orders_home_advantage_correctly():
    probs = probs_1x2(2.0, 1.0, -0.1)[0]
    assert probs[0] > probs[2]
    assert probs.sum() == pytest.approx(1.0)


# --- baselines ---------------------------------------------------------------
def test_climatology_is_constant_and_normalised(league_matches):
    model = Climatology().fit(league_matches, pd.Timestamp("2005-01-10"))
    probs = model.predict_1x2(league_matches.head(20))
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert np.allclose(probs, probs[0])
    assert probs[0][0] > probs[0][2]        # home advantage exists


def test_climatology_tracks_the_observed_rates(league_matches):
    asof = pd.Timestamp("2006-01-10")
    model = Climatology(half_life_days=float("inf"), window_seasons=99).fit(
        league_matches, asof
    )
    past = league_matches[league_matches["date"] < asof]
    observed = past["ftr"].value_counts(normalize=True)
    assert model.rates[0] == pytest.approx(observed["H"], abs=0.02)
    assert model.rates[1] == pytest.approx(observed["D"], abs=0.02)


def test_simple_poisson_has_no_decay_and_no_correction(league_matches):
    model = SimplePoisson().fit(league_matches, pd.Timestamp("2005-01-10"))
    assert model.rho == 0.0
    assert model.xi == 0.0
    probs = model.predict_1x2(league_matches.head(10))
    assert np.allclose(probs.sum(axis=1), 1.0)


def test_fit_is_fast_enough_for_the_tuning_grid(league_matches):
    """DESIGN.md 6.3: over 0.5 s per fit and the grid stops being runnable."""
    model = DixonColes().fit(league_matches, pd.Timestamp("2006-01-10"))
    assert model.meta.seconds < 0.5
