"""Entry point. One command, runs to completion, writes its output and exits."""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from gaffer import config
from gaffer.ingest import FplClient
from gaffer.model import TeamStrength, project, team_fixture_runs
from gaffer.model.points import squad_minutes
from gaffer.lms import Rules as LmsRules
from gaffer.lms.advise import season_advice
from gaffer.league import (advise, advise_match, compare_squads, effective_ownership,
                           fixture_for, is_head_to_head, read_league, read_league_any,
                           read_matches, simulate_league, simulate_match)
from gaffer.optimise import best_lineup, evaluate_chips, evaluate_transfers, pick_squad
from gaffer.schedule import gameweeks_played, work_due
from gaffer.publish import write_json, write_lastman, write_report
from gaffer.review import review_gameweek, summarise
import requests

from gaffer.ingest.fpl import FplError, backfill_history
from gaffer.model.carryover import effective_player
from gaffer.rank import build_board
from gaffer.store import Store


# Shown on the page when the model has no usable evidence. It names the cause
# rather than the symptom: the previous wording blamed configuration for what
# was really an empty evidence base, which sent the reader looking in the wrong
# place entirely.
EVIDENCE_BROKEN_REASON = (
    "Projections could not be built this run. The Fantasy Premier League API "
    "clears every player's minutes and starts when a new season begins, and the "
    "model has not yet recovered last season's record to replace them — so it "
    "cannot tell who is expected to play. Your squad is shown as you picked it; "
    "squad, captain and transfer advice are withheld until the numbers mean "
    "something rather than published as a guess."
)


def _review_payload(result) -> dict:
    """Flatten the review for `latest.json`, which stays the page's contract."""
    return {
        "gameweek": result.gameweek,
        "provisional": result.provisional,
        "points": result.points,
        "verdict": result.verdict,
        "league_position": result.league_position,
        "league_size": result.league_size,
        "league_mean": round(result.league_mean, 1),
        "league_spread": round(result.league_spread, 1),
        "deviations": round(result.deviations_from_mean, 2),
        "within_normal_variation": result.within_normal_variation,
        "xi_points": result.xi_points,
        "xi_projected": round(result.xi_projected, 1),
        "points_on_bench": result.points_on_bench,
        "auto_subs": result.auto_subs,
        "captain": result.captain.name if result.captain else "",
        "captain_points": result.captain.points if result.captain else 0,
        "captain_agreed": result.captain_agreed,
        "captain_cost": result.captain_cost,
        "best_starter": result.best_starter.name if result.best_starter else "",
        "best_starter_points": result.best_starter.points if result.best_starter else 0,
        "differentials": [
            {"name": p.name, "ownership": round(p.ownership, 1), "points": p.points}
            for p in result.differentials
        ],
        "picks": [
            {"name": p.name, "position": p.position, "points": p.points,
             "multiplier": p.multiplier, "minutes": p.minutes,
             "ownership": round(p.ownership, 1),
             "projected": round(p.projected, 2) if p.projected is not None else None}
            for p in result.picks
        ],
    }


def run(
    *,
    horizon: int = config.HORIZON,
    refresh: bool = False,
    quiet: bool = False,
    entry_id: int | None = None,
    league_id: int | None = None,
    optimise: bool = True,
    trials: int = 2000,
    last_man_standing: bool = True,
    lms_used: str = "",
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

    # Last Man Standing needs nothing from the projection layer — only the
    # fixture list and the team ratings — so it is planned here, before the
    # expensive half of the run, and survives a run made with --no-optimise.
    lms = None
    if last_man_standing:
        log("  planning last man standing …")
        try:
            advice = season_advice(fixtures, bootstrap, strength, gameweek=gameweek,
                                   rules=LmsRules.from_env(), extra_used=lms_used)
            lms = advice.as_dict()
            if advice.status == "alive":
                log(f"    pick {advice.pick} — {advice.options[0].survival:.0%} to survive")
            else:
                log(f"    {advice.status}: {advice.reason}")
        except Exception as exc:
            # A different game sharing the same data should never be able to take
            # the fantasy run down with it.
            log(f"    ! could not plan the LMS route: {exc}")

    log("  projecting expected points …")
    runs = team_fixture_runs(fixtures, horizon)

    # Last season is the evidence base until this one has games in it. It has to
    # come from the store: bootstrap-static zeroes every player's minutes and
    # starts at the rollover, which is what silently emptied the model on the
    # morning of GW1 and left a goalkeeper captained on 0.2 expected points.
    games_played = gameweeks_played(events, fixtures)
    with Store() as store:
        backfill_history(client, store, [p["id"] for p in bootstrap["elements"]], log=log)
        history = store.latest_history()
    log(f"    {len(history)} player(s) with a prior season · "
        f"{games_played} gameweek(s) played this season")

    # One overlay, applied before anything reads a player. Everything
    # downstream — minutes, points, the board — then works unchanged.
    bootstrap = dict(bootstrap, elements=[
        effective_player(p, history.get(p["id"]), games_played)
        for p in bootstrap["elements"]
    ])

    # One minutes model, club-normalised, shared by the projection and the board
    # so the minutes printed are the minutes the points were built from.
    minutes_by_id = squad_minutes(bootstrap)
    projections = project(bootstrap, runs, strength, minutes_by_id)
    scores = build_board(bootstrap, projections, strength, minutes_by_id)

    # The failure this guards against was silent: every projection collapsed to
    # a floor, the run exited cleanly, and the page published an arbitrary
    # tie-break as a recommendation. An eleven nobody is expected to play in is
    # not a close call, it is a broken evidence base.
    peak_minutes = max((row.minutes for row in scores), default=0.0)
    evidence_broken = peak_minutes < config.MINIMUM_CREDIBLE_MINUTES
    if evidence_broken:
        log(f"  ! projections are not credible — the best-projected player in the "
            f"league is expected to play {peak_minutes:.0f} minutes. Withholding "
            f"squad, captain and transfer advice.")

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

    if optimise and not evidence_broken:
        log("  optimising squad …")
        squad = pick_squad(scores)
        first_gw_xp = {row.id: (row.xp[0] if row.xp else 0.0) for row in scores}
        lineup = best_lineup(squad.players, first_gw_xp, positions_by_id)
        log(f"    £{squad.cost:.1f}m, {lineup.formation}, {squad.status}")

    manager: dict | None = None
    if entry_id:
        manager = {"entry_id": entry_id, "squad_readable": False, "reason": ""}
        log(f"  reading entry {entry_id} …")
        try:
            picks = client.entry_picks(entry_id, gameweek - 1 if gameweek > 1 else 1)
            rows = picks["picks"]
            held = [p["element"] for p in rows]
            # What was actually fielded, not just who is owned. The board used to
            # drop all of this and show only what the model would do, which left
            # no way to see your own team next to its opinion.
            actual = {
                "captain": next((p["element"] for p in rows if p.get("is_captain")), None),
                "vice": next((p["element"] for p in rows if p.get("is_vice_captain")), None),
                # FPL numbers picks 1-15; 12-15 are the bench, in order.
                "starters": [p["element"] for p in sorted(rows, key=lambda r: r["position"])
                             if p["position"] <= 11],
                "bench": [p["element"] for p in sorted(rows, key=lambda r: r["position"])
                          if p["position"] > 11],
                "gameweek": gameweek - 1 if gameweek > 1 else 1,
            }
            entry = client.entry(entry_id)
            bank = (entry.get("last_deadline_bank") or 0) / 10.0
            manager.update({"squad_readable": True, "name": entry.get("name"),
                            "actual": actual})
            if evidence_broken:
                manager["reason"] = EVIDENCE_BROKEN_REASON
                log("    squad read, but advice withheld — projections not credible")
            else:
                transfers = evaluate_transfers(scores, held, bank=bank)
                lineup = best_lineup(
                    held, {row.id: (row.xp[0] if row.xp else 0.0) for row in scores},
                    positions_by_id)
                log(f"    {len(transfers)} option(s) priced")
        except (FplError, requests.RequestException, KeyError, ValueError) as exc:
            # Before the first deadline of a gameweek nobody's picks are public,
            # including your own. That is timing, not a configuration mistake,
            # and the page has to say which — otherwise it reads as "you set this
            # up wrong" when the only thing to do is wait.
            #
            # Deliberately not a bare `except Exception`: one swallowed a typo in
            # this very block and reported it as "squads are private" — a message
            # that would have stood for the rest of the season while the real
            # cause was a NameError two lines up. A bug here must surface as a
            # bug, not as a plausible-sounding empty state.
            manager["reason"] = (
                "Squads are private until the deadline passes. Yours appears here "
                "once it does, along with transfer pricing and your head-to-head "
                "opponent.")
            log(f"    squad not readable yet: {exc.__class__.__name__}")

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
            standings, kind = read_league_any(league_id, client)
            log(f"    {standings['league']['name']} ({kind})")

            if kind == "h2h":
                league = _head_to_head(
                    client, league_id, standings, entry_id, gameweek, horizon,
                    reference_squad, lineup, scores, projections, trials, log)
                raise _Handled

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
        except _Handled:
            pass
        except Exception as exc:
            log(f"    ! could not read league {league_id}: {exc}")

    review = None
    # Reviewable once the deadline has passed, which is when squads become
    # public — not once FPL marks the gameweek finished, which lags by days.
    # Events carry no `started` flag; the deadline is the signal.
    now = datetime.now(timezone.utc)
    last_finished = max(
        (e["id"] for e in events
         if e.get("deadline_time")
         and datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")) < now),
        default=0)
    if entry_id and last_finished:
        log(f"  reviewing GW{last_finished} …")
        try:
            event = next(e for e in events if e["id"] == last_finished)
            past_picks = client.entry_picks(entry_id, last_finished)
            live_stats = {e["id"]: e["stats"]
                          for e in client.event_live(last_finished)["elements"]}
            field = []
            if league_id:
                try:
                    past_standings, _ = read_league_any(league_id, client)
                    field = read_league(league_id, last_finished, client,
                                        exclude_entry=entry_id,
                                        standings=past_standings)
                except Exception:
                    # A league we cannot read costs the comparison, not the review.
                    field = []
            with Store() as store:
                logged = store.predictions_for(last_finished)
            held = [pk["element"] for pk in past_picks.get("picks", [])]
            model_captain = max(held, key=lambda i: logged.get(i, 0.0), default=None) \
                if logged else None
            result = review_gameweek(
                last_finished, past_picks, live_stats, bootstrap, rivals=field,
                projections=logged, model_captain=model_captain,
                # Bonus is not settled until FPL checks the data, and a
                # provisional number reported as final is a lie by omission.
                provisional=not event.get("data_checked"))
            review = _review_payload(result)
            for line in summarise(result):
                log(line)
        except (FplError, requests.RequestException, KeyError, ValueError) as exc:
            log(f"    could not review GW{last_finished}: {exc.__class__.__name__}")

    from gaffer.publish.render import build_payload

    payload = build_payload(scores=scores, bootstrap=bootstrap, fixture_runs=runs,
                            horizon=horizon, strength=strength, squad=squad,
                            lineup=lineup, transfers=transfers, chips=chips,
                            league=league, due=due, manager=manager, lms=lms,
                            review=review, games_played=games_played)
    json_path = write_json(payload)
    html_path = write_report(payload)
    lastman_path = write_lastman(payload)

    log(f"\n  gameweek {gameweek}, deadline {payload['meta']['deadline']}")
    log(f"  {counts['player']} players · {counts['fixture']} fixtures · {snapshots} snapshot(s) stored")
    log(f"  {payload['counts']['flagged']} fitness doubts · {payload['counts']['moved_club']} changed club")
    top = scores[0] if scores else None
    if top:
        log(f"  top projection: {top.name} {top.projected:.1f} xP over {horizon} GW "
            f"({top.projected / horizon:.2f}/gw)")
    log(f"\n  wrote {json_path.relative_to(config.ROOT)}")
    log(f"  wrote {html_path.relative_to(config.ROOT)}")
    log(f"  wrote {lastman_path.relative_to(config.ROOT)}")
    log(f"  done in {time.time() - started:.1f}s")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gaffer", description="Fantasy Premier League squad engine")
    parser.add_argument("--horizon", type=int, default=config.HORIZON,
                        help=f"gameweeks to look ahead (default {config.HORIZON})")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--top", type=int, default=0, help="also print the top N players to the terminal")
    parser.add_argument("--entry", type=int, default=config.ENTRY_ID,
                        help="your FPL team ID — prices transfers against your actual squad "
                             "(defaults to GAFFER_ENTRY, which .env can set)")
    parser.add_argument("--league", type=int, default=config.LEAGUE_ID,
                        help="your mini-league ID — simulates you against its actual squads "
                             "(defaults to GAFFER_LEAGUE, which .env can set)")
    parser.add_argument("--trials", type=int, default=2000,
                        help="simulation trials for the league (default 2000)")
    parser.add_argument("--no-optimise", action="store_true",
                        help="skip squad selection and only build the board")
    parser.add_argument("--no-lms", action="store_true",
                        help="skip the Last Man Standing route")
    parser.add_argument("--lms-used", default="",
                        help="clubs already spent in your Last Man Standing pool, "
                             "comma separated — added to the saved record for this "
                             "run without being written to it")
    args = parser.parse_args(argv)

    payload = run(horizon=args.horizon, refresh=args.refresh, quiet=args.quiet,
                  entry_id=args.entry, league_id=args.league,
                  optimise=not args.no_optimise, trials=args.trials,
                  last_man_standing=not args.no_lms, lms_used=args.lms_used)

    if not args.quiet:
        if payload.get("squad"):
            _print_squad(payload)
        for option in payload.get("transfers", []):
            _print_transfer(option, payload)
        _print_chips(payload)
        _print_league(payload)
        _print_lms(payload)

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


class _Handled(Exception):
    """Signals the head-to-head path already produced the league block."""


def _head_to_head(client, league_id, standings, entry_id, gameweek, horizon,
                  squad, lineup, scores, projections, trials, log):
    """Advice for a head-to-head league, where one opponent decides the week."""
    if not entry_id:
        log("    need --entry to work out who you are playing")
        return None

    matches = read_matches(league_id, client)
    if not matches:
        log("    no fixtures published yet — they appear once the league starts")
        return {"league_id": league_id, "kind": "h2h",
                "name": standings["league"]["name"], "waiting": True}

    match = fixture_for(matches, entry_id, gameweek)
    if not match:
        log(f"    no fixture found for GW{gameweek}")
        return {"league_id": league_id, "kind": "h2h",
                "name": standings["league"]["name"], "waiting": True}

    opponent_id, opponent_name = match.opponent_of(entry_id)
    picks = client.entry_picks(opponent_id, max(1, gameweek - 1))
    their_squad = [p["element"] for p in picks.get("picks", [])]
    if not their_squad:
        log("    opponent's squad is not public yet")
        return {"league_id": league_id, "kind": "h2h",
                "name": standings["league"]["name"], "waiting": True}

    positions = {row.id: row.position for row in scores}
    first_gw_xp = {row.id: (row.xp[0] if row.xp else 0.0) for row in scores}
    their_lineup = best_lineup(their_squad, first_gw_xp, positions)

    draws = {
        (row.id, gw): projections[row.id][gw].draws
        for row in scores if row.id in projections
        for gw in range(min(horizon, len(projections[row.id])))
    }
    p_win, p_draw, p_loss, my_mean, their_mean = simulate_match(
        squad, lineup.captain if lineup else 0,
        their_squad, their_lineup.captain, draws, trials=trials)

    shared, mine_only, theirs_only = compare_squads(squad, their_squad)
    stance, reason = advise_match(p_win, p_loss, shared, len(squad))
    log(f"    GW{gameweek} v {opponent_name}: win {p_win:.0%} · {stance}")

    return {
        "league_id": league_id,
        "kind": "h2h",
        "name": standings["league"]["name"],
        "match": {
            "gameweek": gameweek,
            "opponent": opponent_id,
            "opponent_name": opponent_name,
            "p_win": round(p_win, 4),
            "p_draw": round(p_draw, 4),
            "p_loss": round(p_loss, 4),
            "my_mean": round(my_mean, 1),
            "their_mean": round(their_mean, 1),
            "expected_league_points": round(p_win * 3 + p_draw, 2),
            "shared_players": shared,
            "my_differentials": mine_only,
            "their_differentials": theirs_only,
            "stance": stance,
            "reason": reason,
        },
    }


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

    if league.get("kind") == "h2h":
        print(f"\n  {league['name']} — head to head")
        match = league.get("match")
        if not match:
            print("    fixtures not published yet; they appear once the league starts")
            return
        print(f"    GW{match['gameweek']} v {match['opponent_name']}")
        print(f"    win {match['p_win']:.0%}  draw {match['p_draw']:.0%}  "
              f"loss {match['p_loss']:.0%}   "
              f"({match['expected_league_points']:.2f} of 3 league points)")
        print(f"    projected {match['my_mean']:.0f} v {match['their_mean']:.0f}")
        print(f"    {match['shared_players']} shared players — they cannot change the result")
        print(f"    stance: {match['stance'].upper()}")
        print(f"    {match['reason']}")
        return

    simulation, advice = league["simulation"], league["advice"]
    print(f"\n  mini-league — {league['rivals']} rivals")
    print(f"    my points over {simulation['gameweeks']} GW: {simulation['my_mean']:.0f} "
          f"(10th {simulation['my_p10']:.0f} / 90th {simulation['my_p90']:.0f})")
    print(f"    P(top of the league): {simulation['win_probability']:.1%}")
    print(f"    stance: {advice['stance'].upper()} — {advice['suggested']}")
    if advice["biggest_exposure"]:
        print(f"    biggest exposure: {', '.join(advice['biggest_exposure'])}")


def _print_lms(payload: dict) -> None:
    lms = payload.get("lms")
    if not lms:
        return
    print(f"\n  last man standing — GW{lms['gameweek']}")
    if lms.get("standing_pick"):
        print(f"    GW{lms['standing_gameweek']} already picked: {lms['standing_pick']}")
    if lms["status"] != "alive":
        print(f"    {lms['reason']}")
        return
    print(f"    PICK {lms['pick']}")
    print(f"    {lms['reason']}")
    route = lms.get("route") or {}
    trail = "  ".join(f"GW{p['gameweek']} {p['name']}" for p in route.get("picks", []))
    print(f"    route ({route.get('survival', 0):.1%} to survive all "
          f"{route.get('rounds', 0)}): {trail}")


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
