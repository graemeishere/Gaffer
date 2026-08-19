"""Entry point. One command, runs to completion, writes its output and exits."""
from __future__ import annotations

import argparse
import sys
import time

from gaffer import config
from gaffer.ingest import FplClient
from gaffer.model import TeamStrength, project, team_fixture_runs
from gaffer.publish import write_json, write_report
from gaffer.rank import build_board
from gaffer.store import Store


def run(*, horizon: int = config.HORIZON, refresh: bool = False, quiet: bool = False) -> dict:
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
        store.upsert_reference(bootstrap["teams"], bootstrap["elements"], positions)
        store.upsert_fixtures(fixtures)
        store.append_snapshot(bootstrap["elements"], gameweek)
        counts = store.row_counts()
        snapshots = store.snapshot_count()

    log("  fitting team strength …")
    strength = TeamStrength.fit(fixtures, bootstrap)
    log(f"    {strength.source}, {strength.matches_fitted} match(es) of results")

    log("  projecting expected points …")
    runs = team_fixture_runs(fixtures, horizon)
    projections = project(bootstrap, runs, strength)
    scores = build_board(bootstrap, projections, strength)

    from gaffer.publish.render import build_payload

    payload = build_payload(scores=scores, bootstrap=bootstrap, fixture_runs=runs,
                            horizon=horizon, strength=strength)
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
    args = parser.parse_args(argv)

    payload = run(horizon=args.horizon, refresh=args.refresh, quiet=args.quiet)

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


if __name__ == "__main__":
    sys.exit(main())
