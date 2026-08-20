"""Shared fixtures, and a hard ban on network access during tests.

DESIGN.md 4 requires that no test makes an HTTP request. Rather than trusting
that, `socket.socket.connect` is replaced for the whole session: a test that
reaches for football-data.co.uk fails loudly instead of quietly depending on
a website being up and unchanged.
"""

from __future__ import annotations

import socket

import pytest

from tests.synth import synth_league


@pytest.fixture(scope="session", autouse=True)
def no_network():
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def blocked(self, address, *args, **kwargs):
        raise AssertionError(
            f"network access attempted during tests (connect to {address!r}); "
            "tests must run entirely on synthetic data"
        )

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


@pytest.fixture(scope="session")
def league():
    """Eight seasons with promotion and relegation, and a matchweek that
    spreads over Friday/Saturday/Sunday."""
    frame, truth = synth_league(seed=11, n_seasons=8, rotate=3)
    return frame, truth


@pytest.fixture(scope="session")
def league_matches(league):
    return league[0]


@pytest.fixture(scope="session")
def stable_league():
    """Twelve seasons, fixed membership -- for parameter recovery."""
    return synth_league(seed=23, n_seasons=12, rotate=0, market_blur=0.0)
