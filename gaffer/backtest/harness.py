"""Running the comparison and reporting what actually happened.

Two questions, answered separately because they can disagree:

1. **Does the projection rank players well?** Rank correlation and average error
   against what each player really scored.
2. **Does a squad built from it score more?** Ranking well is not the same as
   winning — a strategy can rate players correctly and still assemble a worse
   fifteen once a budget is involved.

The second is the one that matters, so it is measured honestly: the squad and
the starting eleven are chosen using only what was known beforehand, then scored
on what happened. Picking the eleven with hindsight would flatter every strategy
equally and tell us nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from gaffer import config
from gaffer.backtest.dataset import (SeasonRow, input_coverage, previous_season,
                                     season_pairs)
from gaffer.backtest.strategies import STRATEGIES
from gaffer.optimise import best_lineup, pick_squad
from gaffer.rank import PlayerRow

# Which club a player turned out for in a past season is not exposed, so the
# three-per-club rule cannot be applied faithfully. It is lifted for every
# strategy alike, which keeps the comparison fair even though the squads are
# less constrained than a real one.
CLUB_LIMIT_OFF = config.SQUAD_SIZE


@dataclass
class StrategyResult:
    name: str
    rank_correlation: float
    mean_absolute_error: float
    squad_points: int
    squad_cost: float
    captain: str
    top_picks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "rank_correlation": round(self.rank_correlation, 3),
            "mean_absolute_error": round(self.mean_absolute_error, 1),
            "squad_points": self.squad_points,
            "squad_cost": round(self.squad_cost, 1),
            "captain": self.captain,
            "top_picks": self.top_picks,
        }


@dataclass
class BacktestResult:
    season: str
    players: int
    strategies: list[StrategyResult]
    prior_season: str = ""
    coverage: dict = field(default_factory=dict)

    @property
    def winner(self) -> StrategyResult:
        return max(self.strategies, key=lambda s: s.squad_points)

    def as_dict(self) -> dict:
        return {
            "season": self.season,
            "players": self.players,
            "prior_season": self.prior_season,
            "coverage": {k: round(v, 3) if isinstance(v, float) else v
                         for k, v in self.coverage.items()},
            "strategies": [s.as_dict() for s in self.strategies],
            "winner": self.winner.name,
        }


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, written out rather than pulled in from scipy.

    Ranks rather than raw values because we care whether the ordering is right —
    being wrong about everyone by the same amount costs nothing when you are
    choosing between players.
    """
    if len(xs) < 3:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mean_x, mean_y = sum(rx) / n, sum(ry) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    var_x = math.sqrt(sum((a - mean_x) ** 2 for a in rx))
    var_y = math.sqrt(sum((b - mean_y) ** 2 for b in ry))
    return cov / (var_x * var_y) if var_x and var_y else 0.0


def _ranks(values: list[float]) -> list[float]:
    """Average ranks, so ties do not distort the correlation."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _rows_for_optimiser(
    pairs: list[tuple[SeasonRow, SeasonRow]], projections: list[float]
) -> list[PlayerRow]:
    """Wrap projections as board rows so the real optimiser can pick from them.

    Prices are the test season's *starting* price — what you would actually have
    paid before a ball was kicked.
    """
    rows = []
    for (prior, actual), projected in zip(pairs, projections):
        price = actual.cost_start or prior.cost_end or 4.0
        rows.append(PlayerRow(
            id=prior.code, name=prior.name, team=prior.code, position=prior.position,
            price=price, owned=0.0, xp=[projected], var=[0.0], projected=projected,
            per_million=projected / price if price else 0.0, minutes=80.0,
            fixture_score=3.0, availability=1.0, confidence="high",
            moved_club=False, note="",
        ))
    return rows


def _score_squad(
    rows: list[PlayerRow], actual_points: dict[int, int], positions: dict[int, str]
) -> tuple[int, float, str, list[str]]:
    """Build a squad from projections alone, then score it on what happened."""
    squad = pick_squad(rows, max_per_club=CLUB_LIMIT_OFF, bench_weight=0.0, time_limit=25)
    projected = {row.id: row.projected for row in rows}
    names = {row.id: row.name for row in rows}

    lineup = best_lineup(squad.players, projected, positions)
    scored = sum(actual_points.get(pid, 0) for pid in lineup.starters)
    scored += actual_points.get(lineup.captain, 0)  # the armband doubles

    top = sorted(lineup.starters, key=lambda pid: -projected.get(pid, 0))[:3]
    return scored, squad.cost, names.get(lineup.captain, "?"), [names.get(p, "?") for p in top]


def run_backtest(
    rows: dict[tuple[int, str], SeasonRow],
    test_season: str,
    *,
    min_minutes: int = 900,
) -> BacktestResult:
    pairs = season_pairs(rows, test_season, min_minutes=min_minutes)
    if len(pairs) < 30:
        raise ValueError(f"only {len(pairs)} players have both seasons — too few to test")

    actual_points = {prior.code: actual.points for prior, actual in pairs}
    positions = {prior.code: prior.position for prior, _ in pairs}
    truth = [float(actual.points) for _, actual in pairs]

    results = []
    for name, strategy in STRATEGIES.items():
        projections = [strategy(prior) for prior, _ in pairs]
        board = _rows_for_optimiser(pairs, projections)
        points, cost, captain, top = _score_squad(board, actual_points, positions)

        # Error is measured on a common scale: strategies like "minutes played"
        # do not output points at all, so each is rescaled to the same mean
        # before differencing. Ranking is what they are really judged on.
        scale = (sum(truth) / sum(projections)) if sum(projections) else 0.0
        errors = [abs(p * scale - t) for p, t in zip(projections, truth)]

        results.append(StrategyResult(
            name=name,
            rank_correlation=spearman(projections, truth),
            mean_absolute_error=sum(errors) / len(errors),
            squad_points=points,
            squad_cost=cost,
            captain=captain,
            top_picks=top,
        ))

    results.sort(key=lambda r: -r.squad_points)
    prior = previous_season(test_season)
    return BacktestResult(
        season=test_season, players=len(pairs), strategies=results,
        prior_season=prior, coverage=input_coverage(rows, prior))
