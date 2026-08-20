"""Scheduling from the deadline rather than the calendar.

FPL deadlines land on four weekdays at six clock times, so a fixed weekly cron
misses most of the season — including every midweek round, which is when good
advice is worth most. These pin the phase boundaries.
"""
from datetime import datetime, timedelta, timezone

import pytest

from gaffer.schedule import (
    FINAL_SOLVE, FULL_SOLVE, IDLE, SYNC, next_deadline, work_due,
)

D1 = datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc)
D2 = datetime(2026, 8, 28, 17, 30, tzinfo=timezone.utc)
EVENTS = [
    {"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "finished": False},
    {"id": 2, "deadline_time": "2026-08-28T17:30:00Z", "finished": False},
    {"id": 3, "deadline_time": "2026-09-02T18:30:00Z", "finished": False},  # midweek
]


class TestNextDeadline:
    def test_finds_the_next_open_gameweek(self):
        assert next_deadline(EVENTS, D1 - timedelta(days=1)) == (1, D1)

    def test_skips_deadlines_already_passed(self):
        assert next_deadline(EVENTS, D1 + timedelta(hours=1)) == (2, D2)

    def test_returns_nothing_when_the_season_is_over(self):
        assert next_deadline(EVENTS, D1 + timedelta(days=60)) is None


class TestPhases:
    @pytest.mark.parametrize("hours,expected", [
        (120, IDLE), (49, IDLE), (47, FULL_SOLVE), (4, FULL_SOLVE),
        (2, FINAL_SOLVE), (0.5, FINAL_SOLVE),
    ])
    def test_phase_by_hours_remaining(self, hours, expected):
        assert work_due(EVENTS, D1 - timedelta(hours=hours)).phase == expected

    def test_syncs_shortly_after_a_deadline(self):
        """Picks become public once the deadline passes, and everything else is
        advice about a squad we need to know first."""
        assert work_due(EVENTS, D1 + timedelta(hours=3)).phase == SYNC

    def test_stops_syncing_once_the_window_closes(self):
        assert work_due(EVENTS, D1 + timedelta(hours=20)).phase != SYNC

    def test_a_midweek_deadline_is_handled_like_any_other(self):
        """A fixed weekly cron would miss this entirely."""
        midweek = datetime(2026, 9, 2, 18, 30, tzinfo=timezone.utc)
        assert work_due(EVENTS, midweek - timedelta(hours=2)).phase == FINAL_SOLVE

    def test_season_over_is_idle(self):
        due = work_due(EVENTS, D1 + timedelta(days=60))
        assert due.phase == IDLE
        assert "complete" in due.reason

    def test_solve_and_sync_flags_agree_with_the_phase(self):
        assert work_due(EVENTS, D1 - timedelta(hours=2)).should_solve
        assert work_due(EVENTS, D1 + timedelta(hours=2)).should_sync
        assert not work_due(EVENTS, D1 - timedelta(hours=120)).should_solve
