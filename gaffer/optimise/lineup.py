"""Picking the eleven, the armband and the bench order from a fixed fifteen.

Small enough to solve exactly by walking every legal formation, which is faster
than setting up a solver and leaves nothing to a time limit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

from gaffer.optimise.squad import LINEUP_MAX, LINEUP_MIN, LINEUP_SIZE

# How clear a one-week lead has to be before the armband comes off the squad's
# strongest asset over the whole horizon. A single gameweek's expected points is
# a noisy number — a good fixture and a defender's clean-sheet floor can put a
# steady player a point or two above the best forward in the game for one week —
# and the armband doubles whatever it lands on, so chasing that week-to-week
# wobble is how the captaincy ends up on a centre-back over the league's premier
# striker. The lead has to beat half the combined standard deviation of the two
# weekly estimates — a small-but-real edge — or the horizon's best player keeps
# it.
CAPTAIN_OVERRIDE_MARGIN = 0.5


def _pick_captain(eleven: list[int], xp: dict[int, float],
                  horizon: dict[int, float] | None,
                  sd: dict[int, float] | None) -> int:
    """Who wears the armband, given a robust view of a noisy one-week number.

    Without the horizon and spread this is just the highest weekly projection,
    which is what it always was. With them, the squad's strongest asset over the
    whole horizon keeps the armband unless another player leads it *this* week by
    more than the uncertainty on that lead — a within-a-standard-deviation edge
    on a one-week figure is noise, and doubling noise is a bad bet.
    """
    weekly_leader = max(eleven, key=lambda pid: xp.get(pid, 0.0))
    if horizon is None:
        return weekly_leader
    anchor = max(eleven, key=lambda pid: horizon.get(pid, 0.0))
    if anchor == weekly_leader:
        return weekly_leader
    lead = xp.get(weekly_leader, 0.0) - xp.get(anchor, 0.0)
    spread = sd or {}
    combined = math.hypot(spread.get(weekly_leader, 0.0), spread.get(anchor, 0.0))
    return weekly_leader if lead > CAPTAIN_OVERRIDE_MARGIN * combined else anchor

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


def best_lineup(squad_ids: list[int], xp: dict[int, float], positions: dict[int, str],
                horizon: dict[int, float] | None = None,
                sd: dict[int, float] | None = None) -> Lineup:
    """The eleven that maximises expected points for one gameweek.

    The captain doubles, so it goes to the highest projection in the eleven —
    tempered, when a horizon view and a spread are supplied, so a noisy one-week
    lead does not lift the armband off the squad's best asset for a marginal
    gain (see `_pick_captain`). The vice is the next highest of the week — it
    only pays out if the captain does not play, so picking a rotation risk there
    quietly wastes the insurance.
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
    captain = _pick_captain(eleven, xp, horizon, sd)
    # Vice is the best of the week among everyone else — it only pays out if the
    # captain does not play, so it wants the highest floor left, not the horizon
    # view that steadied the armband.
    others = [pid for pid in eleven if pid != captain]
    vice = max(others, key=lambda pid: xp.get(pid, 0.0)) if others else captain

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
