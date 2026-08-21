"""Which team, in which round — solved as a route, not as a weekly choice.

This is the whole problem, and it is not the one people think they are solving.
Asked to pick a team for Saturday, almost everyone takes the shortest-priced
home favourite available. That is locally right and globally wrong: it spends
the best clubs on the weeks where the second-best club would have survived
anyway, and leaves you in November holding nothing but the sides you were
avoiding in August.

The correct question is which *assignment* of clubs to rounds maximises the
chance of surviving all of them. Each club may be used once, each round needs
exactly one pick, and every pairing has a known survival probability — an
assignment problem, and the same integer solver that builds the fantasy squad
handles it in milliseconds.

Maximising the product of the weekly survival probabilities is the same as
maximising the sum of their logarithms, which is what makes it linear and
therefore exactly solvable. It is also the right objective rather than a
convenient one: you are out the first time a pick fails, so the run is worth the
chance that every one of them holds, not the average of them.

The greedy route is kept alongside it, not as a fallback but as the benchmark.
Its margin is the only honest measure of what the planning is worth, and this
repository's habit is to score itself against the obvious alternative rather
than assume it beats it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pulp

from gaffer.lms.odds import MatchOdds
from gaffer.lms.rules import Rules
from gaffer.optimise.solver import build as build_solver

SOLVER_TIME_LIMIT = 20

# A pick with no realistic chance still has to have a finite log, or the solver
# sees an infinity rather than a very bad idea.
MIN_SURVIVAL = 1e-4


@dataclass
class RoutePick:
    gameweek: int
    team: int
    name: str
    opponent: str
    home: bool
    win: float
    draw: float
    survival: float
    doubled: bool = False

    def as_dict(self) -> dict:
        return {
            "gameweek": self.gameweek,
            "team": self.team,
            "name": self.name,
            "opponent": self.opponent,
            "home": self.home,
            "win": round(self.win, 4),
            "draw": round(self.draw, 4),
            "survival": round(self.survival, 4),
            "doubled": self.doubled,
        }


@dataclass
class Route:
    picks: list[RoutePick] = field(default_factory=list)
    status: str = "optimal"
    method: str = "planned"
    note: str = ""

    @property
    def survival(self) -> float:
        """The chance of surviving every round on the route."""
        total = 1.0
        for pick in self.picks:
            total *= pick.survival
        return total

    @property
    def rounds(self) -> int:
        return len(self.picks)

    @property
    def first(self) -> RoutePick | None:
        return self.picks[0] if self.picks else None

    def as_dict(self) -> dict:
        return {
            "picks": [p.as_dict() for p in self.picks],
            "survival": round(self.survival, 4),
            "rounds": self.rounds,
            "status": self.status,
            "method": self.method,
            "note": self.note,
        }


def candidates(
    rounds: dict[int, list[MatchOdds]],
    *,
    used: set[int] | list[int],
    rules: Rules,
    from_gameweek: int | None = None,
) -> dict[int, list[MatchOdds]]:
    """The picks still legally available, round by round.

    Trimmed to the horizon here rather than in the solver, so both the planned
    and the greedy route see exactly the same choices and their margin means
    something.
    """
    used = set(used)
    gameweeks = sorted(gw for gw in rounds
                       if from_gameweek is None or gw >= from_gameweek)
    available: dict[int, list[MatchOdds]] = {}
    for gameweek in gameweeks[: rules.horizon]:
        rows = [o for o in rounds[gameweek] if o.team not in used]
        if rows:
            available[gameweek] = rows
    return available


def _pick(odds: MatchOdds, names: dict[int, str], rules: Rules) -> RoutePick:
    return RoutePick(
        gameweek=odds.gameweek,
        team=odds.team,
        name=names.get(odds.team, str(odds.team)),
        opponent=names.get(odds.opponent, str(odds.opponent)),
        home=odds.home,
        win=odds.win,
        draw=odds.draw,
        survival=odds.survival(rules.draw_survives),
        doubled=odds.doubled,
    )


def greedy_route(
    available: dict[int, list[MatchOdds]],
    names: dict[int, str],
    rules: Rules,
) -> Route:
    """Take the best team on the board every week and never look further.

    What the pool does. Included so the planner has something to beat.
    """
    spent: set[int] = set()
    picks: list[RoutePick] = []
    for gameweek in sorted(available):
        pool = [o for o in available[gameweek] if o.team not in spent]
        if not pool:
            return Route(picks=picks, status="exhausted", method="greedy",
                         note=f"nothing left to pick in GW{gameweek}")
        best = max(pool, key=lambda o: o.survival(rules.draw_survives))
        spent.add(best.team)
        picks.append(_pick(best, names, rules))
    return Route(picks=picks, method="greedy")


def plan_route(
    available: dict[int, list[MatchOdds]],
    names: dict[int, str],
    rules: Rules,
    *,
    force: tuple[int, int] | None = None,
) -> Route:
    """The assignment of clubs to rounds with the best chance of surviving them all.

    `force` pins one (gameweek, team) pairing and plans optimally around it,
    which is how a candidate other than the recommendation gets priced: the cost
    of taking it is what the rest of the route is worth afterwards.
    """
    gameweeks = sorted(available)
    if not gameweeks:
        return Route(status="no-fixtures", note="no eligible fixtures in the horizon")

    # If the horizon reaches further than the clubs left can cover, the plan is
    # truncated from the far end rather than declared impossible. A route that
    # runs out in GW34 is still the right thing to do in GW28.
    for depth in range(len(gameweeks), 0, -1):
        route = _solve(gameweeks[:depth], available, names, rules, force)
        if route is not None:
            if depth < len(gameweeks):
                route.status = "truncated"
                route.note = (f"the clubs left cover {depth} of the next "
                              f"{len(gameweeks)} rounds")
            return route

    return Route(status="infeasible", note="no legal route through the next round")


def _solve(gameweeks, available, names, rules, force) -> Route | None:
    problem = pulp.LpProblem("lms_route", pulp.LpMaximize)
    rows = {gw: available[gw] for gw in gameweeks}

    choose = {
        (gw, o.team): pulp.LpVariable(f"x_{gw}_{o.team}", cat="Binary")
        for gw in gameweeks for o in rows[gw]
    }

    problem += pulp.lpSum(
        choose[(gw, o.team)] * math.log(max(o.survival(rules.draw_survives), MIN_SURVIVAL))
        for gw in gameweeks for o in rows[gw]
    )

    # Exactly one pick a round: skipping a week is not a thing you may do.
    for gw in gameweeks:
        problem += pulp.lpSum(choose[(gw, o.team)] for o in rows[gw]) == 1

    # And each club at most once across the whole route — the constraint the
    # entire format turns on.
    teams = {o.team for gw in gameweeks for o in rows[gw]}
    for team in teams:
        used_in = [choose[(gw, team)] for gw in gameweeks if (gw, team) in choose]
        if len(used_in) > 1:
            problem += pulp.lpSum(used_in) <= 1

    if force:
        gameweek, team = force
        if (gameweek, team) not in choose:
            return None
        problem += choose[(gameweek, team)] == 1

    problem.solve(build_solver(SOLVER_TIME_LIMIT))
    if pulp.LpStatus[problem.status] != "Optimal":
        return None

    picks = []
    for gw in gameweeks:
        chosen = next((o for o in rows[gw] if choose[(gw, o.team)].value() > 0.5), None)
        if chosen is None:
            return None
        picks.append(_pick(chosen, names, rules))
    return Route(picks=picks)
