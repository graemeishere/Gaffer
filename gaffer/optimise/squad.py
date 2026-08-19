"""Choosing the best fifteen.

Not "the fifteen best players" — the best *combination* that fits the rules.
Those are different questions, and the second is where points are quietly lost.
A squad is bounded by £100.0m, by 2/5/5/3 across the positions, and by a limit
of three players from any one club, so the optimum reliably contains players
nobody would pick on purpose: cheap bench fodder whose only job is to free up
the money that turns a good forward into a great one.

Solved as a mixed-integer program rather than by picking greedily, because a
greedy pass takes the best player it can afford at each step and then cannot
afford the combination that would actually have scored more.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pulp

from gaffer import config
from gaffer.optimise.solver import build as build_solver
from gaffer.rank import PlayerRow

# Future gameweeks are discounted: the projection for six weeks out is a guess
# about a lineup nobody has picked yet, so it should not outweigh Saturday.
DEFAULT_DECAY = 0.86

# Bench players score nothing unless someone ahead of them does not play. Weight
# them at zero and the solver fills the bench with £4.0m players who can never
# cover an absence; weight them fully and it wastes budget on a bench that never
# plays. Somewhere around a tenth is the usual compromise.
DEFAULT_BENCH_WEIGHT = 0.12

SQUAD_QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
LINEUP_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
LINEUP_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
LINEUP_SIZE = 11


@dataclass
class Squad:
    players: list[int]
    starters_by_gameweek: dict[int, list[int]]
    captain_by_gameweek: dict[int, int]
    cost: float
    expected_points: float
    bench: list[int] = field(default_factory=list)
    status: str = "optimal"

    def as_dict(self) -> dict:
        return {
            "players": self.players,
            "bench": self.bench,
            "starters_by_gameweek": {str(k): v for k, v in self.starters_by_gameweek.items()},
            "captain_by_gameweek": {str(k): v for k, v in self.captain_by_gameweek.items()},
            "cost": round(self.cost, 1),
            "expected_points": round(self.expected_points, 2),
            "status": self.status,
        }


def prune_candidates(rows: list[PlayerRow], per_position: int = 55, cheap: int = 14) -> list[PlayerRow]:
    """Cut the pool to something a solver can chew through quickly.

    Keeps the best of each position by projection, and separately the cheapest of
    each — the enablers matter as much as the premiums, because they are what the
    premium is paid for. Dropping them would hide the optimum rather than find it.
    """
    keep: dict[int, PlayerRow] = {}
    for position in SQUAD_QUOTA:
        pool = [r for r in rows if r.position == position and r.availability > 0]
        for row in sorted(pool, key=lambda r: -r.projected)[:per_position]:
            keep[row.id] = row
        for row in sorted(pool, key=lambda r: (r.price, -r.projected))[:cheap]:
            keep[row.id] = row
    return list(keep.values())


def _weights(horizon: int, decay: float) -> list[float]:
    return [decay ** i for i in range(horizon)]


def pick_squad(
    rows: list[PlayerRow],
    *,
    budget: float = config.BUDGET / 10.0,
    bench_weight: float = DEFAULT_BENCH_WEIGHT,
    decay: float = DEFAULT_DECAY,
    max_per_club: int = config.MAX_PER_CLUB,
    locked: list[int] | None = None,
    banned: list[int] | None = None,
    time_limit: int = 40,
) -> Squad:
    """Pick the fifteen that maximise discounted expected points over the horizon.

    The starting eleven is chosen separately for every gameweek, so the squad is
    judged on how it can actually be used week to week rather than on one static
    lineup.
    """
    candidates = prune_candidates(rows)
    if locked:
        by_id = {r.id: r for r in rows}
        candidates = list({**{c.id: c for c in candidates},
                           **{i: by_id[i] for i in locked if i in by_id}}.values())
    banned_ids = set(banned or [])
    candidates = [c for c in candidates if c.id not in banned_ids]

    horizon = max(len(c.xp) for c in candidates)
    weights = _weights(horizon, decay)

    problem = pulp.LpProblem("squad", pulp.LpMaximize)
    in_squad = {c.id: problem.add_variable(f"squad_{c.id}", cat="Binary") for c in candidates}
    starts = {
        (c.id, gw): problem.add_variable(f"start_{c.id}_{gw}", cat="Binary")
        for c in candidates for gw in range(horizon)
    }
    captains = {
        (c.id, gw): problem.add_variable(f"cap_{c.id}_{gw}", cat="Binary")
        for c in candidates for gw in range(horizon)
    }

    def xp(c: PlayerRow, gw: int) -> float:
        return c.xp[gw] if gw < len(c.xp) else 0.0

    problem += pulp.lpSum(
        weights[gw] * (
            xp(c, gw) * starts[(c.id, gw)]
            + xp(c, gw) * bench_weight * (in_squad[c.id] - starts[(c.id, gw)])
            + xp(c, gw) * captains[(c.id, gw)]   # the armband doubles the score
        )
        for c in candidates for gw in range(horizon)
    )

    problem += pulp.lpSum(in_squad.values()) == config.SQUAD_SIZE
    problem += pulp.lpSum(c.price * in_squad[c.id] for c in candidates) <= budget

    for position, quota in SQUAD_QUOTA.items():
        problem += pulp.lpSum(
            in_squad[c.id] for c in candidates if c.position == position) == quota

    clubs = {c.team for c in candidates}
    for club in clubs:
        problem += pulp.lpSum(
            in_squad[c.id] for c in candidates if c.team == club) <= max_per_club

    for player_id in (locked or []):
        if player_id in in_squad:
            problem += in_squad[player_id] == 1

    for gw in range(horizon):
        problem += pulp.lpSum(starts[(c.id, gw)] for c in candidates) == LINEUP_SIZE
        problem += pulp.lpSum(captains[(c.id, gw)] for c in candidates) == 1
        for c in candidates:
            problem += starts[(c.id, gw)] <= in_squad[c.id]
            problem += captains[(c.id, gw)] <= starts[(c.id, gw)]
        for position in SQUAD_QUOTA:
            playing = pulp.lpSum(
                starts[(c.id, gw)] for c in candidates if c.position == position)
            problem += playing >= LINEUP_MIN[position]
            problem += playing <= LINEUP_MAX[position]

    problem.solve(build_solver(time_limit))
    status = pulp.LpStatus[problem.status]

    chosen = [c for c in candidates if in_squad[c.id].value() and in_squad[c.id].value() > 0.5]
    starters = {
        gw: [c.id for c in chosen if starts[(c.id, gw)].value() and starts[(c.id, gw)].value() > 0.5]
        for gw in range(horizon)
    }
    captain = {}
    for gw in range(horizon):
        picked = [c.id for c in chosen
                  if captains[(c.id, gw)].value() and captains[(c.id, gw)].value() > 0.5]
        captain[gw] = picked[0] if picked else 0

    bench = [c.id for c in chosen if c.id not in set(starters.get(0, []))]
    return Squad(
        players=[c.id for c in chosen],
        starters_by_gameweek=starters,
        captain_by_gameweek=captain,
        cost=sum(c.price for c in chosen),
        expected_points=pulp.value(problem.objective) or 0.0,
        bench=bench,
        status=status.lower(),
    )
