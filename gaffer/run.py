"""Entry point. One command, runs to completion, writes its output and exits."""
from __future__ import annotations

import argparse
import sys
import time

from gaffer import config
from gaffer.ingest import FplClient
from gaffer.model import TeamStrength, project, team_fixture_runs
from gaffer.league import advise, effective_ownership, read_league, simulate_league
from gaffer.optimise import best_lineup, evaluate_chips, evaluate_transfers, pick_squad
from gaffer.schedule import work_due
from gaffer.publish import write_json, write_report
from gaffer.rank import build_board
from gaffer.store import Store


def run(
    *,
    horizon: int = config.HORIZON,
    refresh: bool = False,
    quiet: bool = False,
    entry_id: int | None = None,
    league_id: int | None = None,
    optimise: bool = True,
    trials: int = 2000,
) -> dict:
    started = time.time()// 1
    log = (lambda *a: None) if quiet else print

    log("gaffer — phase 1")
    client = FplClient(ttl=0 if refresh else config.CACHE_TTL)

    log("  fetching bootstrap …")
    bootstrap = client.bootstrap()
    log("  fetching fixtures …")
    fixtures = client.fixtures()

    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    events = bootstrap["events"]
    nxt = next((e for e in events if e.get("is_next")), None)
    cur = next((e for e in events if e.get("is_current")), None)
    gameweek = (nxt or cur or events[0])["id"]

    log("  storing snapshot …")
    with Store() as store:
        # Restore the durable log first — this container has no memory of
        # previous runs, and the record is the whole point.
        restored = store.import_predictions(config.PREDICTIONS_CSV)
        restored_actuals = store.import_actuals(config.ACTUALS_CSV)
        if restored or restored_actuals:
            log(f"    restored {restored} predictions, {restored_actuals} results from the record")
        store.upsert_reference(bootstrap["teams"], bootstrap["elements"], positions)
        store.upsert_fixtures(fixtures)
        store.append_snapshot(bootstrap["elements"], gameweek)

        # Pull results for any finished gameweek we have not scored yet. One call
        # per gameweek, and only ever for gameweeks that are already over.
        for event in events:
            if event.get("finished") and event["id"] not in set(store.scored_gameweeks()):
                try:
                    added = store.record_actuals(event["id"], client.event_live(event["id"]))
                    if added:
                        log(f"    recorded {added} results for GW{event['id']}")
                except Exception as exc:
                    log(f"    ! could not read results for GW{event['id']}: {exc}")

        counts = store.row_counts()
        snapshots = store.snapshot_count()

    log("  fitting team strength …")
    strength = TeamStrength.fit(fixtures, bootstrap)
    log(f"    {strength.source}, {strength.matches_fitted} match(es) of results")

    log("  projecting expected points …")
    runs = team_fixture_runs(fixtures, horizon)
    projections = project(bootstrap, runs, strength)
    scores = build_board(bootstrap, projections, strength)

    with Store() as store:
        made_at = store.record_predictions(scores, gameweek, stage="phase-4")
        deadlines = {e["id"]: e["deadline_time"] for e in events if e.get("deadline_time")}
        pruned = store.prune_predictions(deadlines)
        if pruned:
            log(f"    pruned {pruned} superseded or post-deadline predictions")
        store.export_predictions(config.PREDICTIONS_CSV)
        store.export_actuals(config.ACTUALS_CSV)
    log(f"  logged {sum(len(r.xp) for r in scores)} predictions at {made_at}")

    squad = lineup = None
    transfers: list = []
    positions_by_id = {
        p["id"]: positions[p["element_type"]] for p in bootstrap["elements"]
    }

    if optimise:
        log("  optimising squad …")
        squad = pick_squad(scores)
        first_gw_xp = {row.id: (row.xp[0] if row.xp else 0.0) for row in scores}
        lineup = best_lineup(squad.players, first_gw_xp, positions_by_id)
        log(f"    £{squad.cost:.1f}m, {lineup.formation}, {squad.status}")

    if entry_id:
        log(f"  reading entry {entry_id} …")
        try:
            picks = client.entry_picks(entry_id, gameweek - 1 if gameweek > 1 else 1)
            held = [p["element"] for p in picks["picks"]]
            entry = client.entry(entry_id)
            bank = (entry.get("last_deadline_bank") or 0) / 10.0
            transfers = evaluate_transfers(scores, held, bank=bank)
            lineup = best_lineup(
                held, {row.id: (row.xp[0] if row.xp else 0.0) for row in scores},
                positions_by_id)
            log(f"    {len(transfers)} option(s) priced")
        except Exception as exc:  # the picks endpoint 404s before the first deadline
            log(f"    ! could not read entry {entry_id}: {exc}")

    due = work_due(events)
    log(f"  schedule: {due.phase} — {due.reason}")

    chips: list = []
    league: dict | None = None
    reference_squad = None
    if squad:
        reference_squad = squad.players
    if entry_id and lineup:
        reference_squad = lineup.starters + lineup.bench

    if optimise and reference_squad:
        log("  timing the chips …")
        remaining = sum(1 for e in events if not e.get("finished"))
        chips = evaluate_chips(reference_squad, scores, positions_by_id,
                               first_gameweek=gameweek, horizon=horizon,
                               gameweeks_remaining=remaining)

    if league_id and reference_squad:
        log(f"  reading league {league_id} …")
        try:
            rivals = read_league(league_id, max(1, gameweek - 1), client,
                                 exclude_entry=entry_id)
            with_squads = {r.entry_id: (r.squad, r.captain) for r in rivals if r.has_squad}
            if with_squads:
                names = {row.id: row.name for row in scores}
                draws = {
                    (row.id, gw): projections[row.id][gw].draws
                    for row in scores if row.id in projections
                    for gw in range(min(horizon, len(projections[row.id])))
                }
                captain = lineup.captain if lineup else 0
                simulation = simulate_league(reference_squad, captain, with_squads,
                                             draws, gameweeks=horizon, trials=trials)
                ownership = effective_ownership(
                    reference_squad, {k: v[0] for k, v in with_squads.items()}, names)
                strategy = advise(ownership,
                                  win_probability=simulation.win_probability,
                                  gameweeks_left=sum(1 for e in events if not e.get("finished")),
                                  rivals=len(with_squads))
                league = {
                    "league_id": league_id,
                    "rivals": len(with_squads),
                    "simulation": simulation.as_dict(),
                    "ownership": [row.as_dict() for row in ownership if row.mine
                                  or row.kind == "exposure"],
                    "advice": strategy.as_dict(),
                }
                log(f"    {len(with_squads)} rivals · P(top) {simulation.win_probability:.1%} "
                    f"· stance {strategy.stance}")
            else:
                log("    no rival squads readable yet — picks are public only after a deadline")
        except Exception as exc:
            log(f"    ! could not read league {league_id}: {exc}")

    from gaffer.publish.render import build_payload

    payload = build_payload(scores=scores, bootstrap=bootstrap, fixture_runs=runs,
                            horizon=horizon, strength=strength, squad=squad,
                            lineup=lineup, transfers=transfers, chips=chips,
                            league=league, due=due)
    json_path = write_json(payload)
    html_path = write_report(payload)

    log(f"\n  gameweek {gameweek}, deadline {payload['meta']['deadline']}")
    log(f"  {counts['player']} players · {counts['fixture']} fixtures · {snapshots} snapshot(s) stored")
    log(f"  {payload['counts']['flagged']} fitness doubts · {payload['counts']['moved_club']} changed club")
    top = scores[0] if scores else None
    if top:
        log(f"  top projection: {top.name} {top.projected:.1f} xP over {horizon} GW "
            f"({top.projected / horizon:.2f}/gw)")
    log(f"\n  wrote {json_path.relative_to(config.ROOT)}")
    log(f"  wrote {html_path.relative_to(config.ROOT)}")
    log(f"  done in {time.time() - started:.1f}s")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gaffer", description="Fantasy Premier League squad engine")
    parser.add_argument("--horizon", type=int, default=config.HORIZON,
                        help=f"gameweeks to look ahead (default {config.HORIZON})")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--top", type=int, default=0, help="also print the top N players to the terminal")
    parser.add_argument("--entry", type=int, default=None,
                        help="your FPL team ID — prices transfers against your actual squad")
    parser.add_argument("--league", type=int, default=None,
                        help="your mini-league ID — simulates you against its actual squads")
    parser.add_argument("--trials", type=int, default=2000,
                        help="simulation trials for the league (default 2000)")
    parser.add_argument("--no-optimise", action="store_true",
                        help="skip squad selection and only build the board")
    args = parser.parse_args(argv)

    payload = run(horizon=args.horizon, refresh=args.refresh, quiet=args.quiet,
                  entry_id=args.entry, league_id=args.league,
                  optimise=not args.no_optimise, trials=args.trials)

    if not args.quiet:
        if payload.get("squad"):
            _print_squad(payload)
        for option in payload.get("transfers", []):
            _print_transfer(option, payload)
        _print_chips(payload)
        _print_league(payload)

    if args.top:
        print(f"\n  top {args.top} by projected points ({payload['meta']['horizon']} GW):\n")
        header = (f"  {'player':<16}{'pos':<5}{'team':<6}{'£m':>6}{'xP':>7}"
                  f"{'/gw':>6}{'per £m':>8}{'mins':>6}  flags")
        print(header)
        print("  " + "-" * (len(header) - 2))
        for p in payload["players"][:args.top]:
            flags = []
            if p["moved_club"]:
                flags.append("new club")
            if p["availability"] < 1:
                flags.append(f"{p['availability']:.0%} fit")
            if p["confidence"] == "low":
                flags.append("thin data")
            print(f"  {p['name'][:15]:<16}{p['position']:<5}{p['team']:<6}"
                  f"{p['price']:>6.1f}{p['projected']:>7.1f}"
                  f"{p['projected'] / payload['meta']['horizon']:>6.2f}"
                  f"{p['per_million']:>8.2f}{p['minutes']:>6.0f}  {', '.join(flags)}")
    return 0


def _print_squad(payload: dict) -> None:
    by_id = {p["id"]: p for p in payload["players"]}
    squad, lineup = payload["squad"], payload["lineup"]
    starters = set(lineup["starters"])
    order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}

    print(f"\n  optimal squad — £{squad['cost']:.1f}m, {lineup['formation']}, "
          f"{lineup['expected_points']:.1f} xP in GW{payload['meta']['gameweek']}\n")
    print(f"  {'player':<16}{'pos':<5}{'team':<6}{'£m':>6}{'xP6':>7}  role")
    print("  " + "-" * 52)
    for pid in sorted(squad["players"], key=lambda p: (order[by_id[p]["position"]],
                                                       -by_id[p]["projected"])):
        row = by_id[pid]
        if pid == lineup["captain"]:
            role = "XI (C)"
        elif pid == lineup["vice"]:
            role = "XI (V)"
        elif pid in starters:
            role = "XI"
        else:
            role = "bench"
        print(f"  {row['name'][:15]:<16}{row['position']:<5}{row['team']:<6}"
              f"{row['price']:>6.1f}{row['projected']:>7.1f}  {role}")


def _print_chips(payload: dict) -> None:
    chips = payload.get("chips") or []
    if not chips:
        return
    print("\n  chips")
    for chip in chips:
        print(f"    {chip['chip']:<16}{chip['action'].upper():<6}  {chip['reason']}")


def _print_league(payload: dict) -> None:
    league = payload.get("league")
    if not league:
        return
    simulation, advice = league["simulation"], league["advice"]
    print(f"\n  mini-league — {league['rivals']} rivals")
    print(f"    my points over {simulation['gameweeks']} GW: {simulation['my_mean']:.0f} "
          f"(10th {simulation['my_p10']:.0f} / 90th {simulation['my_p90']:.0f})")
    print(f"    P(top of the league): {simulation['win_probability']:.1%}")
    print(f"    stance: {advice['stance'].upper()} — {advice['suggested']}")
    if advice["biggest_exposure"]:
        print(f"    biggest exposure: {', '.join(advice['biggest_exposure'])}")


def _print_transfer(option: dict, payload: dict) -> None:
    by_id = {p["id"]: p for p in payload["players"]}
    names = lambda ids: ", ".join(by_id[i]["name"] for i in ids if i in by_id)
    if not option["transfers"]:
        print(f"\n  roll the transfer        net  0.00   {option['note']}")
        return
    print(f"\n  {names(option['out'])} -> {names(option['in'])}")
    print(f"    gross {option['gross_gain']:+.2f}  hit -{option['hit']}  "
          f"net {option['net_gain']:+.2f}  (± {option['uncertainty']:.1f})")


if __name__ == "__main__":
    sys.exit(main())
