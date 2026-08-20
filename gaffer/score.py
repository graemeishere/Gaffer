"""Marking the model's homework, week by week.

Phase 3 tested the model on completed seasons and found it level with picking
last year's top scorers. That test could only reach half the model — past team
assignments are not exposed, so fixtures never entered it. This is the other
half, and the only one that settles anything: what the engine said before a
deadline, against what actually happened after it.

Every number here is out of sample by construction. Predictions are written
before the gameweek and results are read after it, so there is no way to score
against anything the model could have seen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

from gaffer.backtest.harness import spearman
from gaffer.store import Store

# Below this many minutes a player is mostly measuring his own absence, which
# tells us about the minutes model rather than the points model. Both matter, so
# the summary reports the full field and the players who actually appeared.
PLAYED_MINUTES = 1


@dataclass
class GameweekScore:
    gameweek: int
    players: int
    played: int
    mean_absolute_error: float
    rmse: float
    rank_correlation: float
    rank_correlation_played: float
    predicted_total: float
    actual_total: int
    bias: float               # predicted minus actual, per player
    top_quintile_bias: float  # where squad points are actually won

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}


def score_gameweek(store: Store, gameweek: int, *, deadline: str | None = None) -> GameweekScore | None:
    """Compare the last pre-deadline prediction with what happened."""
    rows = store.prediction_vs_actual(gameweek, before=deadline)
    if len(rows) < 10:
        return None

    predicted = [r[1] for r in rows]
    actual = [float(r[2]) for r in rows]
    minutes = [r[3] or 0 for r in rows]

    errors = [p - a for p, a in zip(predicted, actual)]
    played = [(p, a) for p, a, m in zip(predicted, actual, minutes) if m >= PLAYED_MINUTES]

    ranked = sorted(zip(actual, predicted), key=lambda pair: -pair[0])
    cut = max(1, len(ranked) // 5)
    top = ranked[:cut]

    return GameweekScore(
        gameweek=gameweek,
        players=len(rows),
        played=len(played),
        mean_absolute_error=sum(abs(e) for e in errors) / len(errors),
        rmse=math.sqrt(sum(e * e for e in errors) / len(errors)),
        rank_correlation=spearman(predicted, actual),
        rank_correlation_played=(spearman([p for p, _ in played], [a for _, a in played])
                                 if len(played) >= 3 else 0.0),
        predicted_total=sum(predicted),
        actual_total=int(sum(actual)),
        bias=sum(errors) / len(errors),
        top_quintile_bias=sum(p - a for a, p in top) / len(top),
    )


def score_all(store: Store) -> list[GameweekScore]:
    scores = []
    for gameweek in store.scored_gameweeks():
        result = score_gameweek(store, gameweek)
        if result:
            scores.append(result)
    return scores


def summarise(scores: list[GameweekScore]) -> dict:
    if not scores:
        return {
            "gameweeks": 0,
            "note": ("No gameweek has been both predicted and played yet. Predictions "
                     "are recorded on every run, so this fills in from the first "
                     "completed gameweek onward."),
        }
    return {
        "gameweeks": len(scores),
        "mean_absolute_error": round(sum(s.mean_absolute_error for s in scores) / len(scores), 3),
        "rank_correlation": round(sum(s.rank_correlation for s in scores) / len(scores), 3),
        "rank_correlation_played": round(
            sum(s.rank_correlation_played for s in scores) / len(scores), 3),
        "bias": round(sum(s.bias for s in scores) / len(scores), 3),
        "top_quintile_bias": round(sum(s.top_quintile_bias for s in scores) / len(scores), 3),
        "best_gameweek": max(scores, key=lambda s: s.rank_correlation).gameweek,
        "worst_gameweek": min(scores, key=lambda s: s.rank_correlation).gameweek,
    }


def _print(scores: list[GameweekScore], summary: dict) -> None:
    if not scores:
        print(f"\n  {summary['note']}\n")
        return
    print(f"\n  {'GW':<5}{'players':>9}{'played':>8}{'mean err':>10}{'rmse':>8}"
          f"{'rank corr':>11}{'bias':>8}{'top-20% bias':>14}")
    print("  " + "-" * 73)
    for s in scores:
        print(f"  {s.gameweek:<5}{s.players:>9}{s.played:>8}{s.mean_absolute_error:>10.2f}"
              f"{s.rmse:>8.2f}{s.rank_correlation:>11.3f}{s.bias:>+8.2f}"
              f"{s.top_quintile_bias:>+14.2f}")
    print(f"\n  across {summary['gameweeks']} gameweek(s)")
    print(f"    mean error       {summary['mean_absolute_error']:.2f} points per player")
    print(f"    rank correlation {summary['rank_correlation']:.3f} "
          f"(players who appeared: {summary['rank_correlation_played']:.3f})")
    print(f"    bias             {summary['bias']:+.2f} per player "
          f"({'over' if summary['bias'] > 0 else 'under'}-predicting)")
    print(f"    top-quintile     {summary['top_quintile_bias']:+.2f} — where squad points are won")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="gaffer.score",
        description="Score what the model predicted against what actually happened")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    with Store() as store:
        from gaffer import config as _config
        store.import_predictions(_config.PREDICTIONS_CSV)
        store.import_actuals(_config.ACTUALS_CSV)
        scores = score_all(store)
        summary = summarise(scores)

    if args.json:
        print(_json.dumps({"gameweeks": [s.as_dict() for s in scores],
                           "summary": summary}, indent=1))
    else:
        _print(scores, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
