"""Simulating the rest of the run rather than averaging it.

Expected points answer "how many will I score". A fifteen-player league asks a
different question: "how often do I finish above these particular people". Those
have different answers, because beating fourteen colleagues is about the spread
of outcomes, not the centre of them. A steady squad and a volatile one can share
an average and win at very different rates.

So each gameweek is drawn rather than averaged: goals and assists as counts,
clean sheets and appearances as coin flips, with the rest carried at its mean
because it is steady enough not to matter. Sampling conditionally on the player
actually appearing keeps the marginal expectations exactly where the projection
put them, while making a blank a real blank instead of a fraction of one.

**Read the win probability carefully.** Every squad here is scored with the same
projections that chose yours, so the number answers "if this model is right, how
often do I finish top" — not "is this model right". Those are very different
questions, and the backtest in `gaffer.backtest` is unflattering about the
second: on completed seasons the model only draws with picking last season's
highest scorers. A high win probability from this simulation is therefore a
statement about the squad's *shape* — how concentrated its risk is, how much it
diverges from the field — and not evidence that the projections are accurate.
Treat it as a comparison between squads under a shared assumption, which is what
it is, and never as a forecast of the table.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict


@dataclass
class SimulationResult:
    trials: int
    gameweeks: int
    my_mean: float
    my_p10: float
    my_p90: float
    win_probability: float          # finishing above every rival over the horizon
    beat_average_probability: float
    rival_means: dict[int, float] = field(default_factory=dict)
    beat_each: dict[int, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["rival_means"] = {str(k): round(v, 1) for k, v in self.rival_means.items()}
        data["beat_each"] = {str(k): round(v, 3) for k, v in self.beat_each.items()}
        for key in ("my_mean", "my_p10", "my_p90"):
            data[key] = round(data[key], 1)
        for key in ("win_probability", "beat_average_probability"):
            data[key] = round(data[key], 4)
        return data


def _poisson(rate: float, rng: random.Random) -> int:
    """Knuth's method. The counts here are small, so this is cheap and exact."""
    if rate <= 0:
        return 0
    if rate > 30:  # never reached by a footballer, but keeps the loop bounded
        return int(rate)
    target = pow(2.718281828459045, -rate)
    count, product = 0, rng.random()
    while product > target:
        count += 1
        product *= rng.random()
    return count


def sample_gameweek(draws: dict, rng: random.Random) -> float:
    """One player's points in one gameweek, drawn rather than averaged.

    Returned as a float, not rounded to a whole point. Real FPL scores are
    integers, but the steady component folded in here — bonus, saves, defensive
    contribution, the conceded deduction — is an expectation rather than an
    event. Rounding each draw to an integer shifts the mean by up to half a point
    per player per gameweek, which across fifteen players and six gameweeks is
    tens of points of bias in a simulation whose whole job is comparing totals.
    """
    p_appear = draws.get("p_appear", 0.0)
    if p_appear <= 0 or rng.random() >= p_appear:
        return 0

    # Rates are unconditional, so dividing by the chance of appearing gives the
    # rate for a player who did appear. Expectations come out unchanged.
    points = draws.get("steady", 0.0) / p_appear

    p_60 = draws.get("p_60", 0.0)
    reached_60 = rng.random() < (p_60 / p_appear if p_appear else 0.0)
    points += 2 if reached_60 else 1

    points += _poisson(draws.get("goal_rate", 0.0) / p_appear, rng) * draws.get("goal_value", 4)
    points += _poisson(draws.get("assist_rate", 0.0) / p_appear, rng) * draws.get("assist_value", 3)

    clean_sheet_value = draws.get("clean_sheet_value", 0)
    if clean_sheet_value and reached_60 and p_60 > 0:
        if rng.random() < draws.get("clean_sheet_chance", 0.0) / p_60:
            points += clean_sheet_value

    return points


def _squad_draw(squad: list[int], captain: int, draws_by_gw: dict, gameweeks: int,
                rng: random.Random) -> float:
    """One simulated run for one manager's squad.

    The eleven is not re-picked per draw — that would give every manager perfect
    hindsight about their own bench. Everyone fields their squad as it stands,
    which is the same simplification for all of them.
    """
    total = 0.0
    for gw in range(gameweeks):
        for pid in squad:
            draws = draws_by_gw.get((pid, gw))
            if not draws:
                continue
            scored = sample_gameweek(draws, rng)
            total += scored * 2 if pid == captain else scored
    return total


def simulate_league(
    my_squad: list[int],
    my_captain: int,
    rival_squads: dict[int, tuple[list[int], int]],
    draws_by_gw: dict,
    *,
    gameweeks: int,
    trials: int = 2000,
    seed: int | None = 7,
) -> SimulationResult:
    """How often does this squad finish above these particular squads?"""
    rng = random.Random(seed)
    my_totals: list[float] = []
    rival_sums: dict[int, float] = {rid: 0.0 for rid in rival_squads}
    # Head-to-head has to be counted inside the trial, comparing the two scores
    # from the *same* simulated season. Comparing sorted distributions afterwards
    # would pair my tenth percentile against their tenth percentile, which is a
    # different and meaningless quantity.
    beat_counts: dict[int, int] = {rid: 0 for rid in rival_squads}
    wins = beat_average = 0

    for _ in range(trials):
        mine = _squad_draw(my_squad, my_captain, draws_by_gw, gameweeks, rng)
        my_totals.append(mine)

        theirs = {}
        for rid, (squad, captain) in rival_squads.items():
            score = _squad_draw(squad, captain, draws_by_gw, gameweeks, rng)
            theirs[rid] = score
            rival_sums[rid] += score
            if mine > score:
                beat_counts[rid] += 1

        if theirs:
            if mine > max(theirs.values()):
                wins += 1
            if mine > sum(theirs.values()) / len(theirs):
                beat_average += 1

    ordered = sorted(my_totals)
    return SimulationResult(
        trials=trials,
        gameweeks=gameweeks,
        my_mean=sum(my_totals) / len(my_totals),
        my_p10=ordered[int(0.10 * len(ordered))],
        my_p90=ordered[int(0.90 * len(ordered))],
        win_probability=wins / trials,
        beat_average_probability=beat_average / trials,
        rival_means={rid: rival_sums[rid] / trials for rid in rival_squads},
        beat_each={rid: beat_counts[rid] / trials for rid in rival_squads},
    )
