"""Picking the eleven, the armband and the bench order from a fixed fifteen.

Small enough to solve exactly by walking every legal formation, which is faster
than setting up a solver and leaves nothing to a time limit.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from gaffer.optimise.squad import LINEUP_MAX, LINEUP_MIN, LINEUP_SIZE

# Every legal FPL shape: one keeper, then defenders/midfielders/forwards.
FORMATIONS = [
    (d, m, f)
    for d in range(LINEUP_MIN["DEF"], LINEUP_MAX["DEF"] + 1)
    for m in range(LINEUP_MIN["MID"], LINEUP_MAX["MID"] + 1)
    for f in range(LINEUP_MIN["FWD"], LINEUP_MAX["FWD"] + 1)
    if d + m + f == LINEUP_SIZE - 1
]


@dataclass
class Lineup:
    formation: str
    starters: list[int]
    bench: list[int]     # in auto-substitution order
    captain: int
    vice: int
    expected_points: float

    def as_dict(self) -> dict:
        return asdict(self)


def best_lineup(squad_ids: list[int], xp: dict[int, float], positions: dict[int, str]) -> Lineup:
    """The eleven that maximises expected points for one gameweek.

    The captain doubles, so it goes to the highest projection in the eleven. The
    vice is the next highest — it only pays out if the captain does not play, so
    picking a rotation risk there quietly wastes the insurance.
    """
    by_position: dict[str, list[int]] = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    for pid in squad_ids:
        by_position.setdefault(positions.get(pid, "MID"), []).append(pid)
    for position in by_position:
        by_position[position].sort(key=lambda pid: -xp.get(pid, 0.0))

    if not by_position["GKP"]:
        raise ValueError("squad has no goalkeeper")

    best: tuple[float, tuple[int, int, int], list[int]] | None = None
    for defenders, midfielders, forwards in FORMATIONS:
        if (len(by_position["DEF"]) < defenders
                or len(by_position["MID"]) < midfielders
                or len(by_position["FWD"]) < forwards):
            continue
        eleven = (
            by_position["GKP"][:1]
            + by_position["DEF"][:defenders]
            + by_position["MID"][:midfielders]
            + by_position["FWD"][:forwards]
        )
        total = sum(xp.get(pid, 0.0) for pid in eleven)
        if best is None or total > best[0]:
            best = (total, (defenders, midfielders, forwards), eleven)

    if best is None:
        raise ValueError("no legal formation available from this squad")

    total, shape, eleven = best
    ranked = sorted(eleven, key=lambda pid: -xp.get(pid, 0.0))
    captain, vice = ranked[0], (ranked[1] if len(ranked) > 1 else ranked[0])

    # Bench order decides who comes on when a starter does not play, so it runs
    # best-first — except the reserve keeper, who can only ever replace a keeper.
    bench = [pid for pid in squad_ids if pid not in set(eleven)]
    bench.sort(key=lambda pid: (positions.get(pid) == "GKP", -xp.get(pid, 0.0)))

    return Lineup(
        formation=f"{shape[0]}-{shape[1]}-{shape[2]}",
        starters=eleven,
        bench=bench,
        captain=captain,
        vice=vice,
        expected_points=round(total + xp.get(captain, 0.0), 2),
    )
