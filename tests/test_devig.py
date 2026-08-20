"""Margin removal (DESIGN.md 2.4).

The de-vig is the benchmark's definition, so an error here does not make the
model look worse -- it moves the target. These are the four properties from
DESIGN.md 4: the output is a distribution, it is monotone in the price, it is
the identity on a margin-free book, and `z` stays inside its bracket.
"""

from __future__ import annotations

import numpy as np
import pytest

from footy.config import SHIN_Z_BOUNDS
from footy.odds.devig import (
    devig,
    implied,
    multiplicative,
    overround,
    shin,
    shin_one,
)
from tests.synth import synth_odds


def test_shin_probabilities_sum_to_one():
    odds, _ = synth_odds(300, margin=0.06)
    probs, z = shin(odds)
    assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-10)
    assert np.all(probs > 0.0)


def test_multiplicative_probabilities_sum_to_one():
    odds, _ = synth_odds(300, margin=0.06)
    probs = multiplicative(odds)
    assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-12)


def test_z_stays_inside_its_bracket():
    lo, hi = SHIN_Z_BOUNDS
    for margin in (0.005, 0.02, 0.05, 0.12, 0.25):
        odds, _ = synth_odds(120, margin=margin, seed=int(margin * 1000))
        _, z = shin(odds)
        assert np.all(z >= lo)
        assert np.all(z < hi)


def test_zero_margin_book_is_the_identity():
    """A book that sums to exactly 1 must come back untouched, z == 0."""
    p = np.array([0.46, 0.25, 0.29])
    odds = 1.0 / p
    probs, z = shin_one(odds)
    assert z == pytest.approx(0.0, abs=1e-12)
    assert probs == pytest.approx(p, abs=1e-12)


def test_monotone_in_the_price():
    """Lengthen one price, keep the others: its probability must fall."""
    base = np.array([2.10, 3.40, 3.60])
    probs, _ = shin_one(base)
    for i in range(3):
        longer = base.copy()
        longer[i] *= 1.10
        moved, _ = shin_one(longer)
        assert moved[i] < probs[i]
        for j in range(3):
            if j != i:
                assert moved[j] > probs[j]


def test_shin_removes_more_from_the_longshot_than_normalisation():
    """The whole point of Shin: the margin is not spread evenly."""
    odds = np.array([1.30, 5.50, 11.0])
    shin_p, z = shin_one(odds)
    mult_p = multiplicative(odds)
    assert z > 0.0
    assert shin_p[0] > mult_p[0]          # favourite keeps more
    assert shin_p[2] < mult_p[2]          # longshot loses more


def test_shin_and_multiplicative_barely_differ_on_a_thin_book():
    """DESIGN.md 2.4: on a ~2.4% 1X2 book the choice does not move RPS.

    The measured claim is about the score, not about individual
    probabilities, so that is what is asserted: de-vig by either method and
    the resulting benchmark RPS differs in the fourth decimal.
    """
    from footy.eval.metrics import rps_array

    odds, true_p = synth_odds(2000, margin=0.024, seed=7)
    shin_p, _ = shin(odds)
    mult_p = multiplicative(odds)

    rng = np.random.default_rng(21)
    y = np.array([rng.choice(3, p=row) for row in true_p])
    rps_shin = float(np.mean(rps_array(shin_p, y)))
    rps_mult = float(np.mean(rps_array(mult_p, y)))

    assert abs(rps_shin - rps_mult) < 5e-4
    assert float(np.mean(np.abs(shin_p - mult_p))) < 3e-3


def test_overround_matches_the_injected_margin():
    odds, _ = synth_odds(200, margin=0.05)
    assert np.allclose(overround(odds), 1.05, atol=1e-10)


def test_missing_prices_propagate_as_nan():
    odds = np.array([[2.0, 3.5, 4.0], [np.nan, 3.5, 4.0], [2.0, 0.0, 4.0]])
    probs, z = shin(odds)
    assert np.isfinite(probs[0]).all()
    assert np.isnan(probs[1]).all()
    assert np.isnan(probs[2]).all()
    assert np.isnan(z[1]) and np.isnan(z[2])


def test_devig_rejects_an_unknown_method():
    odds, _ = synth_odds(5)
    with pytest.raises(ValueError):
        devig(odds, "power")


def test_implied_is_the_inverse_price():
    odds = np.array([[2.0, 4.0, 5.0]])
    assert np.allclose(implied(odds), [[0.5, 0.25, 0.2]])
