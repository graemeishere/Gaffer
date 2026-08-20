"""Reading the people you are actually playing against.

A classic league's table is public if you know its ID, and every manager's picks
are public once a deadline has passed. So the engine does not have to guess at
what the field owns from national ownership percentages — it can read the
fourteen squads that decide your table.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from gaffer.ingest import FplClient

FETCH_WORKERS = 6


@dataclass
class Rival:
    entry_id: int
    name: str
    manager: str
    rank: int
    total_points: int
    squad: list[int] = field(default_factory=list)
    captain: int = 0

    @property
    def has_squad(self) -> bool:
        return bool(self.squad)


def read_league(
    league_id: int,
    gameweek: int,
    client: FplClient | None = None,
    *,
    exclude_entry: int | None = None,
) -> list[Rival]:
    """Every rival in the league, with the fifteen they fielded in `gameweek`.

    Picks only become public after a deadline, so this reads a completed
    gameweek. Anyone whose squad cannot be read is returned without one rather
    than dropped — a missing rival should be visible, not silently absent from
    the field you are being measured against.
    """
    client = client or FplClient()
    standings = client.league_standings(league_id)
    rows = standings.get("standings", {}).get("results", [])

    rivals = [
        Rival(
            entry_id=row["entry"],
            name=row.get("entry_name") or "?",
            manager=row.get("player_name") or "?",
            rank=row.get("rank") or 0,
            total_points=row.get("total") or 0,
        )
        for row in rows
        if row.get("entry") != exclude_entry
    ]

    def load(rival: Rival) -> Rival:
        try:
            picks = client.entry_picks(rival.entry_id, gameweek)
        except Exception:
            return rival
        rival.squad = [p["element"] for p in picks.get("picks", [])]
        rival.captain = next(
            (p["element"] for p in picks.get("picks", []) if p.get("is_captain")), 0)
        return rival

    if not rivals:
        return []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        return list(pool.map(load, rivals))
