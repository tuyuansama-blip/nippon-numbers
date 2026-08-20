"""Kickoff-anchored odds scheduling (DESIGN_PHASE2.md 8.4): clustering,
the five offset points, the due-points filter and the degrade rule.

`run_schedule` itself touches the network and is excluded from the test
suite (DESIGN.md 4) -- everything tested here is the pure logic it is built
from.
"""

from __future__ import annotations

import pandas as pd

from footy.pipeline.odds_schedule import (
    LOW_BUDGET_DROP_LABELS,
    LOW_BUDGET_REMAINING,
    OFFSETS,
    ScheduleState,
    cluster_kickoffs,
    due_points,
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
