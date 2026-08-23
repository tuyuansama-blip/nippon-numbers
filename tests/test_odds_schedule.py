"""Kickoff-anchored odds scheduling (DESIGN_PHASE2.md 8.4): clustering,
the five offset points, the due-points filter and the degrade rule.

`run_schedule` itself touches the network and is excluded from the test
suite (DESIGN.md 4) -- everything tested here is the pure logic it is built
from.
"""

from __future__ import annotations

import pandas as pd

from footy.pipeline.odds_schedule import (
    LOOKAHEAD,
    LOW_BUDGET_DROP_LABELS,
    LOW_BUDGET_REMAINING,
    MAX_LATENESS,
    MAX_WAIT,
    OFFSETS,
    ScheduleState,
    cluster_kickoffs,
    due_points,
    plan_points,
    schedule_points,
)


def test_cluster_kickoffs_groups_matches_within_the_window():
    times = [
        "2026-08-22T09:00:00Z", "2026-08-22T09:20:00Z",   # 20 min apart -> one cluster
        "2026-08-23T05:00:00Z",                             # next day -> new cluster
    ]
    anchors = cluster_kickoffs(times)
    assert anchors == [pd.Timestamp("2026-08-22T09:00:00Z"), pd.Timestamp("2026-08-23T05:00:00Z")]


def test_cluster_kickoffs_chains_across_the_whole_window():
    """A cluster's boundary is 30 min from its *last* member, not its first
    -- three matches each 20 min apart span 40 min but stay one cluster."""
    times = [
        "2026-08-22T09:00:00Z", "2026-08-22T09:20:00Z", "2026-08-22T09:40:00Z",
    ]
    anchors = cluster_kickoffs(times)
    assert anchors == [pd.Timestamp("2026-08-22T09:00:00Z")]


def test_cluster_kickoffs_splits_when_the_gap_exceeds_the_window():
    times = ["2026-08-22T09:00:00Z", "2026-08-22T09:31:00Z"]
    anchors = cluster_kickoffs(times)
    assert len(anchors) == 2


def test_cluster_kickoffs_empty_input():
    assert cluster_kickoffs([]) == []


def test_schedule_points_are_before_the_anchor_in_the_documented_order():
    anchor = pd.Timestamp("2026-08-22T09:00:00Z")
    points = schedule_points(anchor)
    labels = [label for label, _ in points]
    assert labels == ["t72h", "t24h", "t6h", "t2h", "t25min"]
    for label, target in points:
        assert target < anchor
    # Monotonically closer to the anchor as the schedule progresses.
    times = [t for _, t in points]
    assert times == sorted(times)


def test_due_points_only_returns_points_at_or_before_now():
    anchor = pd.Timestamp("2026-08-22T09:00:00Z")
    state = ScheduleState()
    now = pd.Timestamp("2026-08-22T04:00:00Z")   # between t6h and t2h
    due = due_points([anchor], now=now, state=state)
    labels = {label for _, label, _ in due}
    assert labels == {"t72h", "t24h", "t6h"}


def test_due_points_skips_points_already_marked_done():
    anchor = pd.Timestamp("2026-08-22T09:00:00Z")
    state = ScheduleState()
    state.mark_done(anchor, "t72h")
    now = pd.Timestamp("2026-08-22T09:00:00Z")   # everything is technically due
    due = due_points([anchor], now=now, state=state)
    labels = {label for _, label, _ in due}
    assert "t72h" not in labels
    assert "t25min" in labels


def test_due_points_applies_the_low_budget_degrade_rule():
    """Below LOW_BUDGET_REMAINING credits, t72h/t24h are dropped so the
    near-kickoff points survive (DESIGN_PHASE2.md 8.4)."""
    anchor = pd.Timestamp("2026-08-22T09:00:00Z")
    state = ScheduleState()
    now = pd.Timestamp("2026-08-22T09:00:00Z")
    plenty = due_points([anchor], now=now, state=state, remaining_credits=LOW_BUDGET_REMAINING + 1)
    scarce = due_points([anchor], now=now, state=state, remaining_credits=LOW_BUDGET_REMAINING - 1)
    plenty_labels = {label for _, label, _ in plenty}
    scarce_labels = {label for _, label, _ in scarce}
    assert plenty_labels == {"t72h", "t24h", "t6h", "t2h", "t25min"}
    assert scarce_labels == plenty_labels - set(LOW_BUDGET_DROP_LABELS)


def test_schedule_state_round_trips_through_disk(tmp_path):
    anchor = pd.Timestamp("2026-08-22T09:00:00Z")
    path = tmp_path / "state.json"
    state = ScheduleState()
    state.mark_done(anchor, "t72h")
    state.save(path)

    reloaded = ScheduleState.load(path)
    assert reloaded.is_done(anchor, "t72h")
    assert not reloaded.is_done(anchor, "t24h")


def test_schedule_state_load_of_a_missing_file_is_empty(tmp_path):
    state = ScheduleState.load(tmp_path / "nope.json")
    assert state.done == {}


def test_offsets_are_a_strictly_decreasing_time_gap():
    gaps = [offset for _, offset in OFFSETS]
    assert gaps == sorted(gaps, reverse=True)


# --- plan_points: the GitHub Actions timing guards (docs/DESIGN_ACTIONS.md 4) --
ANCHOR = pd.Timestamp("2026-08-22T09:00:00Z")


def _labels(points):
    return [point.label for point in points]


def test_plan_points_refuses_a_point_that_has_drifted_past_its_lateness_cap():
    """A `schedule:` run can start well after its slot. Booking a 40-minute-
    late snapshot as `t25min` would mark the round's best pre-kickoff reading
    collected while actually recording a worse one -- and, past kickoff, an
    unusable one. Missing the point is the honest outcome."""
    state = ScheduleState()
    on_time = plan_points([ANCHOR], now=ANCHOR - pd.Timedelta(minutes=20),
                          state=state, max_lateness=MAX_LATENESS)
    too_late = plan_points([ANCHOR], now=ANCHOR - pd.Timedelta(minutes=2),
                           state=state, max_lateness=MAX_LATENESS)
    assert "t25min" in _labels(on_time)
    assert "t25min" not in _labels(too_late)


def test_plan_points_never_fires_once_the_cluster_has_kicked_off():
    planned = plan_points([ANCHOR], now=ANCHOR, state=ScheduleState(),
                          max_lateness=MAX_LATENESS)
    assert planned == []


def test_plan_points_waits_for_a_near_kickoff_point_instead_of_missing_it():
    """The other half of the same problem: a run that starts a few minutes
    early may sleep to the exact target rather than leave `t25min` to a next
    run that may itself be late."""
    now = ANCHOR - pd.Timedelta(minutes=33)      # t25min is 8 minutes away
    planned = plan_points([ANCHOR], now=now, state=ScheduleState(),
                          lookahead=LOOKAHEAD, max_lateness=MAX_LATENESS)
    waits = {point.label: point.wait_seconds for point in planned}
    assert waits == {"t25min": 8 * 60}
    # `t2h` is 87 minutes past its own target by now and is correctly left
    # behind rather than fired under a label it no longer describes.


def test_plan_points_reports_zero_wait_for_something_already_due():
    now = ANCHOR - pd.Timedelta(hours=1, minutes=30)   # t2h passed 30 min ago
    planned = plan_points([ANCHOR], now=now, state=ScheduleState(),
                          lookahead=LOOKAHEAD, max_lateness=MAX_LATENESS)
    assert {point.label: point.wait_seconds for point in planned} == {"t2h": 0}


def test_plan_points_does_not_wait_for_the_far_out_points():
    """`t72h`/`t24h` gain nothing from minute-level precision, so they are
    never a reason to hold the workflow's concurrency group."""
    now = ANCHOR - pd.Timedelta(hours=72, minutes=5)
    planned = plan_points([ANCHOR], now=now, state=ScheduleState(),
                          lookahead=LOOKAHEAD, max_lateness=MAX_LATENESS)
    assert planned == []


def test_plan_points_never_waits_longer_than_max_wait():
    long_lookahead = {label: pd.Timedelta(hours=2) for label, _ in OFFSETS}
    now = ANCHOR - pd.Timedelta(hours=2, minutes=30)   # t2h is 30 min away
    planned = plan_points([ANCHOR], now=now, state=ScheduleState(),
                          lookahead=long_lookahead, max_lateness=MAX_LATENESS)
    assert all(point.wait <= MAX_WAIT for point in planned)
    assert "t2h" not in _labels(planned)


def test_plan_points_returns_points_in_chronological_order():
    anchors = [ANCHOR, ANCHOR + pd.Timedelta(hours=20)]
    planned = plan_points(anchors, now=ANCHOR - pd.Timedelta(hours=1),
                          state=ScheduleState(), max_lateness=None)
    targets = [point.target for point in planned]
    assert targets == sorted(targets)


def test_due_points_is_plan_points_with_both_guards_off():
    """`due_points` keeps its old meaning exactly -- everything due, kickoff
    or not -- so nothing that already depends on it changes behaviour."""
    state = ScheduleState()
    due = due_points([ANCHOR], now=ANCHOR, state=state)
    assert {label for _, label, _ in due} == {label for label, _ in OFFSETS}
