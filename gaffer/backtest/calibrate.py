"""Setting the model's free parameters from evidence instead of argument.

Several constants in the projection were chosen as plausible shapes rather than
measured values — the bonus-points curve most of all. This sweeps them against
completed seasons and reports which settings actually score, so they stop being
opinions.

Two numbers are reported because they can disagree, and the disagreement is
informative: rank correlation says whether the ordering of players is right,
squad points says whether a team built from that ordering wins. Squad points is
the one that pays out, so it breaks ties.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from gaffer.backtest.harness import run_backtest
from gaffer.model import minutes as minutes_module
from gaffer.model import points as points_module

# name -> (module, attribute, values to try)
SWEEPS = {
    "bonus_ceiling": (points_module, "_BONUS_CEILING", [1.1, 1.4, 1.7, 2.1, 2.5]),
    "bonus_midpoint": (points_module, "_BONUS_MIDPOINT", [24.0, 28.0, 32.0, 36.0]),
    "base_start_rate": (minutes_module, "BASE_START_RATE", [0.25, 0.35, 0.45, 0.55]),
    "start_prior_games": (minutes_module, "START_RATE_PRIOR_GAMES", [2.0, 4.0, 6.0, 10.0]),
}


@dataclass
class Setting:
    values: dict[str, float]
    squad_points: int
    rank_correlation: float
    beat_naive_by: int

    def as_dict(self) -> dict:
        return {
            "values": self.values,
            "squad_points": self.squad_points,
            "rank_correlation": round(self.rank_correlation, 4),
            "beat_naive_by": self.beat_naive_by,
        }


def _apply(values: dict[str, float]) -> dict[str, float]:
    previous = {}
    for name, value in values.items():
        module, attribute, _ = SWEEPS[name]
        previous[name] = getattr(module, attribute)
        setattr(module, attribute, value)
    return previous


def _score(rows, seasons: list[str]) -> tuple[int, float, int]:
    """Total squad points for the model, its mean rank correlation, and how far
    it finished ahead of the naive benchmark."""
    total = naive_total = 0
    correlations = []
    for season in seasons:
        result = run_backtest(rows, season)
        by_name = {s.name: s for s in result.strategies}
        model = by_name["model"]
        total += model.squad_points
        naive_total += by_name["last season's points"].squad_points
        correlations.append(model.rank_correlation)
    mean_correlation = sum(correlations) / len(correlations) if correlations else 0.0
    return total, mean_correlation, total - naive_total


def sweep_one(rows, seasons: list[str], name: str) -> list[Setting]:
    """Vary a single parameter, holding the rest where they are."""
    module, attribute, candidates = SWEEPS[name]
    original = getattr(module, attribute)
    results = []
    try:
        for value in candidates:
            setattr(module, attribute, value)
            points, correlation, margin = _score(rows, seasons)
            results.append(Setting({name: value}, points, correlation, margin))
    finally:
        setattr(module, attribute, original)
    return results


def sweep_grid(rows, seasons: list[str], names: list[str]) -> list[Setting]:
    """Every combination of the named parameters, best first."""
    grids = [SWEEPS[name][2] for name in names]
    results = []
    originals = {n: getattr(SWEEPS[n][0], SWEEPS[n][1]) for n in names}
    try:
        for combination in itertools.product(*grids):
            values = dict(zip(names, combination))
            _apply(values)
            points, correlation, margin = _score(rows, seasons)
            results.append(Setting(values, points, correlation, margin))
    finally:
        for name, value in originals.items():
            setattr(SWEEPS[name][0], SWEEPS[name][1], value)
    results.sort(key=lambda s: (-s.squad_points, -s.rank_correlation))
    return results


def leave_one_out(rows, seasons: list[str], names: list[str]) -> list[dict]:
    """Tune on every season but one, then score on the season held back.

    With only a handful of completed seasons it is trivially easy to pick the
    settings that happened to suit them and call it calibration. Holding a season
    back and never letting the tuning see it is the difference between measuring
    the model and flattering it. If the held-out margins are not consistently
    positive, the gains from tuning are noise and should not be shipped.
    """
    report = []
    for held_out in seasons:
        tuning = [s for s in seasons if s != held_out]
        best = sweep_grid(rows, tuning, names)[0]
        previous = _apply(best.values)
        try:
            points, correlation, margin = _score(rows, [held_out])
        finally:
            _apply(previous)
        report.append({
            "held_out": held_out,
            "tuned_on": tuning,
            "chosen": best.values,
            "held_out_points": points,
            "held_out_rank_correlation": round(correlation, 4),
            "held_out_margin_vs_naive": margin,
        })
    return report
