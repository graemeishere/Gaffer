"""`python -m gaffer.lms` — the Last Man Standing planner as its own command.

Kept separate from `gaffer.run` because it is a different game played by
different people. It shares the ingest, the cache and the team-strength model
and nothing else: no squad, no budget, no points. Running it costs one API call
that is almost certainly already cached.
"""
from __future__ import annotations

import argparse
import json
import sys

from gaffer import config
from gaffer.ingest import FplClient
from gaffer.lms.advise import season_advice
from gaffer.lms.rules import Rules
from gaffer.lms.state import (UnknownTeam, read_state, resolve_many, resolve_team,
                              write_state)
from gaffer.model import TeamStrength


def build(client: FplClient, rules: Rules, *, extra_used: str = "",
          state_path=None, gameweek: int | None = None):
    """Fetch, fit, settle the record, and produce the advice."""
    bootstrap = client.bootstrap()
    fixtures = client.fixtures()
    teams = bootstrap["teams"]
    names = {t["id"]: t["name"] for t in teams}

    events = bootstrap["events"]
    if gameweek is None:
        nxt = next((e for e in events if e.get("is_next")), None)
        cur = next((e for e in events if e.get("is_current")), None)
        gameweek = (nxt or cur or events[0])["id"]

    state = read_state(state_path)
    settled = state.settle(fixtures)
    if settled:
        write_state(state, state_path)

    strength = TeamStrength.fit(fixtures, bootstrap)
    result = season_advice(fixtures, bootstrap, strength, gameweek=gameweek,
                           rules=rules, extra_used=extra_used,
                           state_path=state_path)
    return result, state, teams, names, settled, strength


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gaffer.lms",
        description="Last Man Standing: which club to back, and which to keep back")
    parser.add_argument("--used", default="",
                        help="clubs already spent, comma separated — treated as used "
                             "for this run without being written to the record")
    parser.add_argument("--pick", default="",
                        help="record a club as this round's pick and save it")
    parser.add_argument("--gameweek", type=int, default=None,
                        help="plan from this round instead of the next one")
    parser.add_argument("--horizon", type=int, default=config.LMS_HORIZON,
                        help=f"rounds to plan ahead (default {config.LMS_HORIZON})")
    parser.add_argument("--lives", type=int, default=config.LMS_LIVES,
                        help=f"lives your pool gives you (default {config.LMS_LIVES})")
    parser.add_argument("--draw-survives", action="store_true",
                        default=config.LMS_DRAW_SURVIVES,
                        help="a draw keeps you in, as some pools play it")
    parser.add_argument("--forget", default="",
                        help="remove clubs from the saved record, comma separated")
    parser.add_argument("--reset", action="store_true",
                        help="clear the saved record and start the season again")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache")
    parser.add_argument("--json", action="store_true", help="print the advice as JSON")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args(argv)

    rules = Rules(draw_survives=args.draw_survives, lives=args.lives,
                  horizon=max(1, args.horizon))
    log = (lambda *a: None) if (args.quiet or args.json) else print
    client = FplClient(ttl=0 if args.refresh else config.CACHE_TTL)

    # The record is edited before anything is planned, so one command can both
    # correct the history and show what it now implies.
    if args.reset:
        from gaffer.lms.state import LmsState
        write_state(LmsState())
        log("  record cleared")

    if args.forget:
        try:
            teams = client.bootstrap()["teams"]
            drop = set(resolve_many(args.forget, teams))
        except UnknownTeam as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        saved = read_state()
        saved.picks = [p for p in saved.picks if p.team not in drop]
        write_state(saved)
        log(f"  forgot {len(drop)} club(s)")

    try:
        result, state, teams, names, settled, strength = build(
            client, rules, extra_used=args.used, gameweek=args.gameweek)
    except UnknownTeam as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if settled:
        log(f"  settled {settled} pick(s) against results")

    if args.pick:
        try:
            team = resolve_team(args.pick, teams)
        except UnknownTeam as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        target = result.standing_gameweek or result.gameweek
        saved = read_state()
        saved.settle(client.fixtures())
        saved.record(target, team, names[team])
        write_state(saved)
        log(f"  recorded {names[team]} for GW{target}")
        result, state, teams, names, settled, strength = build(
            client, rules, extra_used=args.used, gameweek=args.gameweek)

    if args.json:
        print(json.dumps(result.as_dict(), indent=1))
        return 0

    _print(result, rules, strength)
    return 0


def _print(result, rules: Rules, strength) -> None:
    print(f"\n  last man standing — GW{result.gameweek}")
    print(f"  {rules.summary}")
    print(f"  team ratings: {strength.source}, {strength.matches_fitted} match(es) of results")

    if result.used:
        print(f"\n  used ({len(result.used)}): {', '.join(result.used)}")
    else:
        print("\n  used: nothing yet")

    if result.standing_pick:
        print(f"  GW{result.standing_gameweek} is already picked: "
              f"{result.standing_pick}. Planning from the round after it.")

    if result.status != "alive":
        print(f"\n  {result.reason}")
        return

    lives = ("one life left" if result.lives_left == 1
             else f"{result.lives_left} lives left")
    print(f"  survived {result.rounds_survived} round(s), {lives}")
    print(f"\n  PICK: {result.pick}")
    print(f"  {result.reason}")

    print(f"\n  {'club':<18}{'fixture':<20}{'win':>6}{'draw':>6}{'survive':>9}"
          f"{'route':>8}{'cost':>8}{'field':>7}  holds for")
    print("  " + "-" * 96)
    for option in result.options:
        fixture = f"{'v ' if option.home else 'at '}{option.opponent}"
        holds = f"GW{option.reserved_for}" if option.reserved_for else ""
        print(f"  {option.name[:17]:<18}{fixture[:19]:<20}{option.win:>6.0%}"
              f"{option.draw:>6.0%}{option.survival:>9.0%}"
              f"{option.route_survival:>8.1%}{-option.cost:>8.0%}"
              f"{option.crowd:>7.0%}  {holds}")

    route = result.route or {}
    print(f"\n  the route — {route.get('survival', 0):.1%} to survive all "
          f"{route.get('rounds', 0)} rounds")
    for pick in route.get("picks", []):
        fixture = f"{'v ' if pick['home'] else 'at '}{pick['opponent']}"
        print(f"    GW{pick['gameweek']:<4}{pick['name'][:17]:<18}{fixture[:19]:<20}"
              f"{pick['survival']:>6.0%}")
    if route.get("note"):
        print(f"    ({route['note']})")

    greedy = result.greedy or {}
    if greedy.get("rounds") == route.get("rounds"):
        print(f"\n  best-team-every-week survives all {route.get('rounds', 0)} rounds "
              f"{greedy.get('survival', 0):.1%} of the time, against "
              f"{route.get('survival', 0):.1%} for the planned route.")

    print(f"\n  {result.crowd_note}")


if __name__ == "__main__":
    sys.exit(main())
