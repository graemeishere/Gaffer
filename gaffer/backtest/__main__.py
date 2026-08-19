"""`python -m gaffer.backtest` — does any of this beat the obvious alternatives?"""
from __future__ import annotations

import argparse
import json
import sys

from gaffer.backtest.dataset import (build_dataset, input_coverage,
                                     previous_season, testable_seasons)
from gaffer.backtest.harness import run_backtest
from gaffer.ingest import FplClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gaffer.backtest",
        description="Score the model against naive strategies on completed seasons")
    parser.add_argument("--seasons", nargs="*", default=None,
                        help="seasons to test, e.g. 2024/25 (default: every season with enough data)")
    parser.add_argument("--min-minutes", type=int, default=900,
                        help="minutes required in the PRIOR season (default 900)")
    parser.add_argument("--limit", type=int, default=None,
                        help="only fetch this many players (for a quick run)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    client = FplClient()
    bootstrap = client.bootstrap()

    def progress(done: int, total: int) -> None:
        print(f"  fetched {done}/{total} player histories …", file=sys.stderr)

    print("building the history …", file=sys.stderr)
    rows = build_dataset(bootstrap, client, limit=args.limit, progress=progress)
    print(f"  {len(rows)} player-seasons\n", file=sys.stderr)

    seasons = args.seasons or testable_seasons(rows)
    if not args.seasons:
        print(f"testing {len(seasons)} season(s) whose prior season carries the "
              f"model's inputs: {', '.join(seasons)}\n", file=sys.stderr)
    payload = []
    for season in seasons:
        try:
            result = run_backtest(rows, season, min_minutes=args.min_minutes)
        except ValueError as exc:
            print(f"skipping {season}: {exc}", file=sys.stderr)
            continue
        payload.append(result.as_dict())
        if not args.json:
            _print(result)

    if args.json:
        print(json.dumps(payload, indent=1))
    elif payload:
        _print_summary(payload)
    return 0


def _print(result) -> None:
    coverage = result.coverage
    print(f"\n  {result.season}  ·  {result.players} players with both seasons"
          f"  ·  inputs from {result.prior_season}: "
          f"xG {coverage['expected_goals']:.0%}, defcon {coverage['defensive_contribution']:.0%}")
    print(f"  {'strategy':<22}{'squad pts':>10}{'cost':>8}{'rank corr':>11}{'mean err':>10}  captain")
    print("  " + "-" * 74)
    for s in result.strategies:
        print(f"  {s.name:<22}{s.squad_points:>10}{s.squad_cost:>8.1f}"
              f"{s.rank_correlation:>11.3f}{s.mean_absolute_error:>10.1f}  {s.captain}")


def _print_summary(payload: list[dict]) -> None:
    print("\n  across all seasons tested")
    print(f"  {'strategy':<22}{'total squad pts':>17}{'wins':>7}{'mean rank corr':>17}")
    print("  " + "-" * 63)
    totals: dict[str, list] = {}
    for season in payload:
        for s in season["strategies"]:
            entry = totals.setdefault(s["name"], [0, 0, []])
            entry[0] += s["squad_points"]
            entry[2].append(s["rank_correlation"])
        totals[season["winner"]][1] += 1
    for name, (points, wins, correlations) in sorted(totals.items(), key=lambda kv: -kv[1][0]):
        print(f"  {name:<22}{points:>17}{wins:>7}"
              f"{sum(correlations) / len(correlations):>17.3f}")


if __name__ == "__main__":
    sys.exit(main())
