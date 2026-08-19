"""Turning the fixture list into each club's upcoming run.

Shared by the strength model and the projection layer: both need to know who a
team plays next, at home or away, and in which gameweek.
"""
from __future__ import annotations

from gaffer import config


def team_fixture_runs(fixtures: list[dict], horizon: int = config.HORIZON) -> dict[int, list[dict]]:
    """For each team, the next `horizon` fixtures with the difficulty they face."""
    upcoming = sorted(
        (f for f in fixtures if f.get("event") and not f.get("finished")),
        key=lambda f: (f["event"], f.get("kickoff_time") or ""),
    )
    runs: dict[int, list[dict]] = {}
    for f in upcoming:
        for team_id, is_home in ((f["team_h"], True), (f["team_a"], False)):
            run = runs.setdefault(team_id, [])
            if len(run) >= horizon:
                continue
            run.append({
                "gameweek": f["event"],
                "opponent": f["team_a"] if is_home else f["team_h"],
                "home": is_home,
                "difficulty": f["team_h_difficulty"] if is_home else f["team_a_difficulty"],
            })
    return runs
