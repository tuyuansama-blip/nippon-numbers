"""The Tb calibration layer (DESIGN_PHASE2.md 4)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.optimize import check_grad

from footy.model.calibrate import OnlineTb, _nll_sum_and_grad, apply_tb, fit_tb


def _problem(seed=0, n=400):
    rng = np.random.default_rng(seed)
    p = rng.dirichlet([3, 2, 2], size=n)
    log_p = np.log(np.clip(p, 1e-12, 1.0))
    y = rng.integers(0, 3, n)
    return log_p, y


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_gradient_matches_check_grad(seed):
    log_p, y = _problem(seed=seed)

    def f(phi):
        return _nll_sum_and_grad(phi, log_p, y)[0]

    def g(phi):
        return _nll_sum_and_grad(phi, log_p, y)[1]

    rng = np.random.default_rng(seed + 100)
    phi0 = rng.normal(0.0, 0.3, 3)
    err = check_grad(f, g, phi0)
    scale = max(np.linalg.norm(g(phi0)), 1.0)
    assert err / scale < 1e-4


def test_apply_tb_identity_is_the_identity_map():
    p = np.array([[0.5, 0.3, 0.2], [0.1, 0.1, 0.8]])
    out = apply_tb(p, np.zeros(3))
    assert np.allclose(out, p, atol=1e-10)


def test_apply_tb_output_is_a_distribution():
    rng = np.random.default_rng(4)
    p = rng.dirichlet([3, 2, 2], size=50)
    phi = np.array([0.3, -0.2, 0.15])
    out = apply_tb(p, phi)
    assert np.allclose(out.sum(axis=1), 1.0)
    assert np.all(out > 0)


def test_apply_tb_high_temperature_flattens_towards_uniform():
    """exp(phi0) >> 1 divides log-odds towards 0, which is uniform-ish."""
    p = np.array([[0.7, 0.2, 0.1]])
    flattened = apply_tb(p, np.array([3.0, 0.0, 0.0]))   # T = e^3 ~= 20
    spread = flattened.max() - flattened.min()
    assert spread < (p.max() - p.min())


def test_fit_tb_recovers_a_known_miscalibration():
    """Generate data from a *shifted* market (favourites over-priced), then
    check the fitted phi corrects for it in the right direction."""
    rng = np.random.default_rng(5)
    n = 6000
    base = rng.dirichlet([4, 3, 3], size=n)
    true_phi = np.array([0.35, -0.25, 0.10])       # cooler + biased away from home
    true_probs = apply_tb(base, true_phi)
    y = np.array([rng.choice(3, p=row) for row in true_probs])

    fitted = fit_tb(base, y, lam=0.0)
    # Same map, so applying it to `base` should land close to `true_probs`.
    recovered = apply_tb(base, fitted)
    assert np.mean(np.abs(recovered - true_probs)) < 0.02


def test_fit_tb_regulariser_pulls_towards_identity():
    log_p, y = _problem(seed=9, n=200)
    p = np.exp(log_p)
    loose = fit_tb(p, y, lam=0.0001)
    tight = fit_tb(p, y, lam=50.0)
    assert np.linalg.norm(tight) < np.linalg.norm(loose)


def test_online_tb_stays_identity_below_warmup():
    rng = np.random.default_rng(6)
    cal = OnlineTb(warmup=100, refit_days=1)
    p = rng.dirichlet([3, 2, 2], size=10)
    y = rng.integers(0, 3, 10)
    asof = pd.Timestamp("2020-01-01")
    out = cal.predict(asof, p)
    assert np.allclose(out, p)
    cal.observe(p, y)
    assert cal.n_history == 10
    assert np.array_equal(cal.phi, np.zeros(3))


def test_online_tb_activates_after_warmup_and_logs_phi():
    rng = np.random.default_rng(7)
    cal = OnlineTb(warmup=50, refit_days=1)
    asof = pd.Timestamp("2020-01-01")
    for i in range(80):
        p = rng.dirichlet([3, 2, 2], size=5)
        y = rng.integers(0, 3, 5)
        cal.predict(asof, p)
        cal.observe(p, y)
        asof = asof + pd.Timedelta(days=2)
    assert cal.n_history == 400
    log = cal.phi_frame()
    assert log["warm"].any()
    assert (log.loc[log["warm"], "refitted"]).any()


def test_online_tb_never_uses_a_folds_own_outcome_to_predict_that_fold():
    """`predict` must be pure given the calibrator's history so far; feeding
    a wildly different outcome via `observe` for the *same* call must not
    retroactively change the prediction already returned."""
    rng = np.random.default_rng(8)
    cal = OnlineTb(warmup=10, refit_days=1)
    asof = pd.Timestamp("2020-01-01")
    p_hist = rng.dirichlet([3, 2, 2], size=20)
    y_hist = rng.integers(0, 3, 20)
    cal.predict(asof, p_hist)
    cal.observe(p_hist, y_hist)

    asof2 = asof + pd.Timedelta(days=40)
    p_next = rng.dirichlet([3, 2, 2], size=3)
    before = cal.predict(asof2, p_next).copy()
    phi_before = cal.phi.copy()
    # Observing an extreme outcome for this same fold must not retroactively
    # change the prediction already returned, nor the phi that produced it.
    cal.observe(p_next, np.array([0, 0, 0]))
    assert np.array_equal(cal.phi, phi_before)
    assert np.allclose(before, apply_tb(p_next, phi_before))
