"""What to do with this week's transfer.

The engine's job here is to price the options, not to issue an order. "Transfer
Shaw to Porro" is something you either obey or ignore; "Porro gains 4.2 over six
weeks, rolling gains nothing, the second transfer nets 1.1 after the hit" is a
set of prices you can disagree with one of and still use the rest. It also
exposes the weeks where the options are within noise of each other, which is
most of them — and saying so is more useful than sounding certain.

Every option is measured the same way: re-solve the whole squad under that
constraint and difference the totals. So a gain is always a *whole-squad*
number, which is why upgrading a player who was going to sit on the bench
correctly comes out at nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import pulp

from gaffer import config
from gaffer.optimise.solver import build as build_solver
from gaffer.optimise.squad import (
    DEFAULT_BENCH_WEIGHT,
    DEFAULT_DECAY,
    LINEUP_MAX,
    LINEUP_MIN,
    LINEUP_SIZE,
    SQUAD_QUOTA,
    prune_candidates,
    _weights,
)
from gaffer.rank import PlayerRow

HIT_COST = 4


@dataclass
class TransferOption:
    transfers: int
    out: list[int]
    in_: list[int]
    hit: int
    gross_gain: float
    net_gain: float
    uncertainty: float   # one standard deviation on the gain
    note: str = ""

    def as_dict(self) -> dict:
        data = asdict(self)
        data["in"] = data.pop("in_")
        return data


def _solve_with_transfer_limit(
    rows: list[PlayerRow],
    current: list[int],
    limit: int,
    *,
    budget: float,
    bench_weight: float,
    decay: float,
    max_per_club: int,
    time_limit: int,
) -> tuple[float, list[int]]:
    """Best squad reachable from `current` making at most `limit` changes."""
    by_id = {r.id: r for r in rows}
    candidates = {c.id: c for c in prune_candidates(rows)}
    for pid in current:
        if pid in by_id:
            candidates[pid] = by_id[pid]
    pool = list(candidates.values())

    horizon = max(len(c.xp) for c in pool)
    weights = _weights(horizon, decay)
    held = set(current)

    problem = pulp.LpProblem("transfers", pulp.LpMaximize)
    in_squad = {c.id: problem.add_variable(f"s_{c.id}", cat="Binary") for c in pool}
    starts = {(c.id, gw): problem.add_variable(f"p_{c.id}_{gw}", cat="Binary")
              for c in pool for gw in range(horizon)}
    captains = {(c.id, gw): problem.add_variable(f"c_{c.id}_{gw}", cat="Binary")
                for c in pool for gw in range(horizon)}

    def xp(c: PlayerRow, gw: int) -> float:
        return c.xp[gw] if gw < len(c.xp) else 0.0

    problem += pulp.lpSum(
        weights[gw] * (
            xp(c, gw) * starts[(c.id, gw)]
            + xp(c, gw) * bench_weight * (in_squad[c.id] - starts[(c.id, gw)])
            + xp(c, gw) * captains[(c.id, gw)]
        )
        for c in pool for gw in range(horizon)
    )

    problem += pulp.lpSum(in_squad.values()) == config.SQUAD_SIZE
    problem += pulp.lpSum(c.price * in_squad[c.id] for c in pool) <= budget
    # Squad size is fixed, so transfers in and out are the same number.
    problem += pulp.lpSum(in_squad[c.id] for c in pool if c.id not in held) <= limit

    for position, quota in SQUAD_QUOTA.items():
        problem += pulp.lpSum(in_squad[c.id] for c in pool if c.position == position) == quota
    for club in {c.team for c in pool}:
        problem += pulp.lpSum(in_squad[c.id] for c in pool if c.team == club) <= max_per_club

    for gw in range(horizon):
        problem += pulp.lpSum(starts[(c.id, gw)] for c in pool) == LINEUP_SIZE
        problem += pulp.lpSum(captains[(c.id, gw)] for c in pool) == 1
        for c in pool:
            problem += starts[(c.id, gw)] <= in_squad[c.id]
            problem += captains[(c.id, gw)] <= starts[(c.id, gw)]
        for position in SQUAD_QUOTA:
            playing = pulp.lpSum(starts[(c.id, gw)] for c in pool if c.position == position)
            problem += playing >= LINEUP_MIN[position]
            problem += playing <= LINEUP_MAX[position]

    problem.solve(build_solver(time_limit))
    chosen = [c.id for c in pool if in_squad[c.id].value() and in_squad[c.id].value() > 0.5]
    return (pulp.value(problem.objective) or 0.0), chosen


def evaluate_transfers(
    rows: list[PlayerRow],
    current: list[int],
    *,
    bank: float = 0.0,
    free_transfers: int = 1,
    max_transfers: int = 2,
    bench_weight: float = DEFAULT_BENCH_WEIGHT,
    decay: float = DEFAULT_DECAY,
    max_per_club: int = config.MAX_PER_CLUB,
    time_limit: int = 30,
) -> list[TransferOption]:
    """Price doing nothing, and each number of transfers up to `max_transfers`."""
    by_id = {r.id: r for r in rows}
    held = [pid for pid in current if pid in by_id]
    if len(held) != config.SQUAD_SIZE:
        raise ValueError(
            f"expected a squad of {config.SQUAD_SIZE}, got {len(held)} known players")

    budget = sum(by_id[pid].price for pid in held) + bank

    baseline, _ = _solve_with_transfer_limit(
        rows, held, 0, budget=budget, bench_weight=bench_weight, decay=decay,
        max_per_club=max_per_club, time_limit=time_limit)

    options = [TransferOption(
        transfers=0, out=[], in_=[], hit=0, gross_gain=0.0, net_gain=0.0,
        uncertainty=0.0,
        note=f"rolls the transfer — {min(free_transfers + 1, 5)} free next week",
    )]

    for count in range(1, max_transfers + 1):
        total, squad = _solve_with_transfer_limit(
            rows, held, count, budget=budget, bench_weight=bench_weight, decay=decay,
            max_per_club=max_per_club, time_limit=time_limit)
        out = [pid for pid in held if pid not in set(squad)]
        incoming = [pid for pid in squad if pid not in set(held)]
        if not incoming:
            continue  # the solver declined to use the allowance; nothing to price

        hit = max(0, len(incoming) - free_transfers) * HIT_COST
        gross = total - baseline

        # Uncertainty on the difference: the two squads share most of their
        # players, so only the swapped players contribute variance.
        changed = [by_id[pid] for pid in out + incoming if pid in by_id]
        variance = sum(_row_variance(r) for r in changed)

        options.append(TransferOption(
            transfers=len(incoming),
            out=out,
            in_=incoming,
            hit=hit,
            gross_gain=round(gross, 2),
            net_gain=round(gross - hit, 2),
            uncertainty=round(math.sqrt(variance), 2),
            note="costs a hit" if hit else "uses a free transfer",
        ))

    options.sort(key=lambda o: -o.net_gain)
    return options


def _row_variance(row: PlayerRow) -> float:
    """Rough spread on a player's contribution over the horizon.

    Scaled off the projection: a player expected to score more has more to be
    wrong about. Phase 3's backtest is what should replace this with a measured
    number rather than an assumed shape.
    """
    return max(0.5, row.projected * 0.9)
