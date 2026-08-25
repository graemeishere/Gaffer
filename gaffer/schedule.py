"""Deciding what work is due, from the deadline rather than the calendar.

The obvious design is a weekly cron. It would be wrong for most of the season:
Premier League deadlines land on four different weekdays at six different clock
times, so a fixed weekly schedule misses the majority of gameweeks — including
every midweek round, which is exactly when good advice is worth most.

Instead a small job wakes hourly, reads the next deadline out of the data, and
decides for itself what is due. One cron line, correct all season, and it
re-times itself when a fixture moves for television.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

IDLE = "idle"
FULL_SOLVE = "full-solve"
FINAL_SOLVE = "final-solve"
SYNC = "sync"

# Hours before the deadline at which each phase begins.
FULL_SOLVE_AT = 48
FINAL_SOLVE_AT = 3
# How long after a deadline picks become readable and worth syncing.
SYNC_WINDOW = 6


@dataclass
class Due:
    phase: str
    gameweek: int
    deadline: datetime
    hours_remaining: float
    reason: str

    @property
    def should_solve(self) -> bool:
        return self.phase in (FULL_SOLVE, FINAL_SOLVE)

    @property
    def should_sync(self) -> bool:
        return self.phase == SYNC


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_played(fixture: dict) -> bool:
    """Whether a fixture's football is over.

    FPL sets `finished_provisional` the moment a match ends; the `finished`
    flag lags by a day or two while bonus points are confirmed. Waiting for the
    slow flag meant the model treated a fully-played gameweek as not-yet-played
    — it ran GW2's projections on nothing but last season while GW1 sat on the
    same page under the Review tab.
    """
    return bool(fixture.get("finished") or fixture.get("finished_provisional"))


def gameweeks_played(events: list[dict], fixtures: list[dict]) -> int:
    """How many gameweeks are done, by the football rather than the flag.

    A gameweek counts once every one of its fixtures has been played. Counting
    part-played weeks would fold half a round of results into the model, so a
    gameweek in progress does not count until its last match is over.
    """
    by_gameweek: dict[int, list[dict]] = {}
    for fixture in fixtures:
        gw = fixture.get("event")
        if gw is not None:
            by_gameweek.setdefault(gw, []).append(fixture)
    return sum(1 for group in by_gameweek.values()
               if group and all(_is_played(f) for f in group))


def next_deadline(events: list[dict], now: datetime | None = None) -> tuple[int, datetime] | None:
    """The next gameweek still open, as (gameweek, deadline)."""
    now = now or datetime.now(timezone.utc)
    upcoming = sorted(
        ((e["id"], _parse(e["deadline_time"])) for e in events if e.get("deadline_time")),
        key=lambda pair: pair[1],
    )
    for gameweek, deadline in upcoming:
        if deadline > now:
            return gameweek, deadline
    return None


def work_due(events: list[dict], now: datetime | None = None) -> Due:
    """What this wake-up should actually do."""
    now = now or datetime.now(timezone.utc)
    upcoming = next_deadline(events, now)

    if upcoming is None:
        last = max((_parse(e["deadline_time"]) for e in events if e.get("deadline_time")),
                   default=now)
        return Due(IDLE, 0, last, 0.0, "season complete — nothing left to solve")

    gameweek, deadline = upcoming
    hours = (deadline - now).total_seconds() / 3600.0

    # Just after the previous deadline, picks are public and worth reading before
    # anything else — everything downstream is advice about a squad we must know.
    previous = [
        _parse(e["deadline_time"]) for e in events
        if e.get("deadline_time") and _parse(e["deadline_time"]) <= now
    ]
    if previous:
        since = (now - max(previous)).total_seconds() / 3600.0
        if since <= SYNC_WINDOW:
            return Due(SYNC, gameweek, deadline, hours,
                       f"deadline passed {since:.0f}h ago — read the squads that were actually picked")

    if hours <= FINAL_SOLVE_AT:
        return Due(FINAL_SOLVE, gameweek, deadline, hours,
                   f"{hours:.1f}h to the GW{gameweek} deadline — re-solve on the latest team news")
    if hours <= FULL_SOLVE_AT:
        return Due(FULL_SOLVE, gameweek, deadline, hours,
                   f"{hours:.0f}h to the GW{gameweek} deadline — full solve")
    return Due(IDLE, gameweek, deadline, hours,
               f"{hours:.0f}h to the GW{gameweek} deadline — refresh prices and injuries only")
