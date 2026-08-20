"""Turn a run's results into the two things anything downstream consumes.

`latest.json` is the contract: the engine's only output, and the only thing the
web page reads. `report.html` is the same data baked into a standalone page, so
a run can be published and looked at without a server.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from gaffer import config
from gaffer.rank import PlayerRow

TEMPLATE = Path(__file__).parent / "report_template.html"


def build_payload(
    *,
    scores: list[PlayerRow],
    bootstrap: dict,
    fixture_runs: dict[int, list[dict]],
    horizon: int,
    strength=None,
    squad=None,
    lineup=None,
    transfers=None,
    chips=None,
    league=None,
    due=None,
) -> dict:
    """Assemble the JSON contract. Phase 0 fills `meta`, `players` and `fixtures`;
    `lineup`, `transfers` and `chips` arrive with the optimiser in Phase 2."""
    events = bootstrap["events"]
    nxt = next((e for e in events if e.get("is_next")), None)
    cur = next((e for e in events if e.get("is_current")), None)
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

    return {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gameweek": (nxt or cur or events[0])["id"],
            "deadline": (nxt or cur or events[0])["deadline_time"],
            "horizon": horizon,
            "stage": "phase-4",
            "method": "expected-points",
            "strength_source": getattr(strength, "source", "unknown"),
            "matches_fitted": getattr(strength, "matches_fitted", 0),
            "warning": (
                "Expected points are built from last season's rates until this season has "
                "results to fit to. Team ratings are currently "
                f"{getattr(strength, 'source', 'unknown')} "
                f"({getattr(strength, 'matches_fitted', 0)} matches of results)."
            ),
        },
        "counts": {
            "players_ranked": len(scores),
            "teams": len(bootstrap["teams"]),
            "flagged": sum(1 for s in scores if s.availability < 1.0),
            "moved_club": sum(1 for s in scores if s.moved_club),
        },
        "players": [s.as_dict() for s in scores],
        "fixtures": {
            teams[team_id]: [
                {**f, "opponent": teams[f["opponent"]]} for f in run
            ]
            for team_id, run in fixture_runs.items()
        },
        "squad": squad.as_dict() if squad else None,
        "lineup": lineup.as_dict() if lineup else None,
        "transfers": [t.as_dict() for t in (transfers or [])],
        "chips": [c.as_dict() for c in (chips or [])],
        "league": league,
        "schedule": {
            "phase": due.phase,
            "reason": due.reason,
            "hours_remaining": round(due.hours_remaining, 1),
        } if due else None,
        "manager": None,
    }


def write_json(payload: dict, path: Path | None = None) -> Path:
    path = path or config.JSON_OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))
    return path


# Bars share one scale across every row, so a weak player's run cannot look like
# a strong one's. Anything above this is clipped rather than rescaling the board.
SPARK_CEILING = 9.0
SPARK_HEIGHT = 20


def _spark(xp: list[float]) -> str:
    """Expected points per gameweek. One series, so the column head is the legend."""
    bars = []
    for value in xp:
        height = max(2, round(min(value, SPARK_CEILING) / SPARK_CEILING * SPARK_HEIGHT))
        cls = " class='z'" if value <= 0.05 else ""
        bars.append(f"<span{cls} style='height:{height}px'></span>")
    label = ", ".join(f"{v:.1f}" for v in xp)
    return f"<span class='spark' title='{label}'>{''.join(bars)}</span>"


def _rows(scores: list[PlayerRow], position: str, limit: int, key) -> str:
    picks = [s for s in scores if s.position == position and s.availability > 0]
    picks.sort(key=key)
    out = []
    for s in picks[:limit]:
        flag = ""
        if s.moved_club:
            flag = '<span class="pill warn">new club</span>'
        elif s.confidence == "low":
            flag = '<span class="pill low">thin data</span>'
        elif s.availability < 1.0:
            flag = '<span class="pill stop">doubt</span>'
        out.append(
            f"<tr><td>{s.name}</td><td class='sub'>{s.team}</td>"
            f"<td class='num'>£{s.price:.1f}</td>"
            f"<td class='num'>{s.projected:.1f}</td>"
            f"<td class='num'>{s.projected / max(len(s.xp), 1):.2f}</td>"
            f"<td>{_spark(s.xp)}</td>"
            f"<td class='num'>{s.per_million:.2f}</td>"
            f"<td class='num'>{s.minutes:.0f}</td>"
            f"<td>{flag}</td></tr>"
        )
    return "\n".join(out)


def _fixture_table(payload: dict, reverse: bool, limit: int = 5) -> str:
    runs = payload["fixtures"]
    ranked = sorted(
        runs.items(),
        key=lambda kv: sum(f["difficulty"] for f in kv[1]) / max(len(kv[1]), 1),
        reverse=reverse,
    )
    out = []
    for team, run in ranked[:limit]:
        mean = sum(f["difficulty"] for f in run) / max(len(run), 1)
        chips = " ".join(
            f"<span class='fx d{f['difficulty']}'>{f['opponent']}"
            f"{'' if f['home'] else '<i>a</i>'}</span>"
            for f in run
        )
        out.append(f"<tr><td>{team}</td><td class='num'>{mean:.2f}</td><td>{chips}</td></tr>")
    return "\n".join(out)


def _pitch(payload: dict) -> str:
    """The chosen eleven laid out by position, with the bench beneath it."""
    squad, lineup = payload.get("squad"), payload.get("lineup")
    if not squad or not lineup:
        return "<p>No squad selected for this run.</p>"

    by_id = {p["id"]: p for p in payload["players"]}
    starters, bench = set(lineup["starters"]), lineup["bench"]

    def card(pid: int, muted: bool = False) -> str:
        row = by_id.get(pid)
        if not row:
            return ""
        badge = ""
        if pid == lineup["captain"]:
            badge = "<span class='cap'>C</span>"
        elif pid == lineup["vice"]:
            badge = "<span class='cap vice'>V</span>"
        first = row["xp"][0] if row["xp"] else 0.0
        return (f"<div class='card{' muted' if muted else ''}'>{badge}"
                f"<b>{row['name']}</b><i>{row['team']} · £{row['price']:.1f}</i>"
                f"<u>{first:.1f}</u></div>")

    rows = []
    for position in ("GKP", "DEF", "MID", "FWD"):
        line = [pid for pid in lineup["starters"] if by_id.get(pid, {}).get("position") == position]
        line.sort(key=lambda pid: -(by_id[pid]["xp"][0] if by_id[pid]["xp"] else 0))
        if line:
            rows.append("<div class='row'>" + "".join(card(pid) for pid in line) + "</div>")

    bench_html = "".join(card(pid, muted=True) for pid in bench)
    return (f"<div class='pitch'>{''.join(rows)}</div>"
            f"<div class='mock-h' style='margin-top:.8rem'>Bench · auto-sub order</div>"
            f"<div class='bench'>{bench_html}</div>")


def _transfer_rows(payload: dict) -> str:
    by_id = {p["id"]: p for p in payload["players"]}
    options = payload.get("transfers") or []
    if not options:
        return ("<p>No squad linked yet. Pass <code>--entry YOUR_TEAM_ID</code> once the "
                "season is under way and this becomes transfer advice for your actual team.</p>")

    names = lambda ids: ", ".join(by_id[i]["name"] for i in ids if i in by_id)
    out = []
    for i, option in enumerate(options):
        label = "Roll the transfer" if not option["transfers"] else (
            f"{names(option['out'])} &rarr; {names(option['in'])}")
        cls = " best" if i == 0 and option["net_gain"] > 0 else ""
        sign = "pos" if option["net_gain"] > 0.05 else ("neg" if option["net_gain"] < -0.05 else "neu")
        band = (f" <span class='band'>± {option['uncertainty']:.1f}</span>"
                if option["uncertainty"] else "")
        out.append(
            f"<div class='opt{cls}'><div class='opt-l'><b>{label}</b>"
            f"<span>{option['note']}</span></div>"
            f"<div class='opt-r {sign}'>{option['net_gain']:+.2f}{band}</div></div>")
    return "".join(out)


def _chip_rows(payload: dict) -> str:
    chips = payload.get("chips") or []
    if not chips:
        return "<p>No squad to time chips against yet.</p>"
    out = []
    for chip in chips:
        tone = "pos" if chip["action"] == "play" else "neu"
        out.append(
            f"<div class='opt'><div class='opt-l'><b>{chip['chip'].title()}</b>"
            f"<span>{chip['reason']}</span></div>"
            f"<div class='opt-r {tone}'>{chip['action'].upper()}</div></div>")
    return "".join(out)


def _league_block(payload: dict) -> str:
    league = payload.get("league")
    if not league:
        return ("<p>No mini-league linked. Pass <code>--league YOUR_LEAGUE_ID</code> once "
                "the first deadline has passed and this fills with your rivals' actual "
                "squads, your win probability against them, and whether to be taking risk "
                "or squeezing it out.</p>")
    simulation, advice = league["simulation"], league["advice"]
    exposure = "".join(f"<li>{name}</li>" for name in advice["biggest_exposure"])
    return (
        f"<div class='stats' style='margin-bottom:.5rem;'>"
        f"<div class='stat'><b>{simulation['win_probability']:.0%}</b>"
        f"<span>chance of finishing top of {league['rivals'] + 1}</span></div>"
        f"<div class='stat'><b>{simulation['my_mean']:.0f}</b>"
        f"<span>my expected points, {simulation['gameweeks']} GW</span></div>"
        f"<div class='stat'><b>{advice['differential_count']}</b>"
        f"<span>players the field mostly lacks</span></div>"
        f"<div class='stat'><b>{advice['stance'].title()}</b>"
        f"<span>recommended stance</span></div></div>"
        f"<div class='banner'><strong>{advice['stance'].title()}.</strong> {advice['reason']} "
        f"{advice['suggested']}</div>"
        + (f"<h3 style='margin-top:.6rem'>Biggest exposure</h3><ul>{exposure}</ul>" if exposure else "")
    )


def write_report(payload: dict, path: Path | None = None) -> Path:
    path = path or config.HTML_OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    scores = [PlayerRow(**p) for p in payload["players"]]

    meta, counts = payload["meta"], payload["counts"]
    deadline = datetime.fromisoformat(meta["deadline"].replace("Z", "+00:00"))
    generated = datetime.fromisoformat(meta["generated"])
    hours_left = (deadline - generated).total_seconds() / 3600

    html = TEMPLATE.read_text()
    replacements = {
        "{{GAMEWEEK}}": str(meta["gameweek"]),
        "{{DEADLINE}}": deadline.strftime("%a %d %b, %H:%M UTC"),
        "{{COUNTDOWN}}": f"{hours_left:.0f}h" if hours_left > 0 else "passed",
        "{{GENERATED}}": generated.strftime("%d %b %Y, %H:%M UTC"),
        "{{HORIZON}}": str(meta["horizon"]),
        "{{N_PLAYERS}}": str(counts["players_ranked"]),
        "{{N_FLAGGED}}": str(counts["flagged"]),
        "{{N_MOVED}}": str(counts["moved_club"]),
        "{{WARNING}}": meta["warning"],
        "{{EASY_FIXTURES}}": _fixture_table(payload, reverse=False),
        "{{HARD_FIXTURES}}": _fixture_table(payload, reverse=True),
        "{{PITCH}}": _pitch(payload),
        "{{CHIPS}}": _chip_rows(payload),
        "{{LEAGUE}}": _league_block(payload),
        "{{PHASE}}": (payload.get("schedule") or {}).get("phase", "—"),
        "{{PHASE_REASON}}": (payload.get("schedule") or {}).get("reason", ""),
        "{{TRANSFERS}}": _transfer_rows(payload),
        "{{SQUAD_COST}}": f"{payload['squad']['cost']:.1f}" if payload.get("squad") else "—",
        "{{FORMATION}}": payload["lineup"]["formation"] if payload.get("lineup") else "—",
        "{{SQUAD_XP}}": (f"{payload['lineup']['expected_points']:.1f}"
                         if payload.get("lineup") else "—"),
    }
    for position, token in (("GKP", "GKP"), ("DEF", "DEF"), ("MID", "MID"), ("FWD", "FWD")):
        replacements[f"{{{{{token}_VALUE}}}}"] = _rows(scores, position, 8, key=lambda s: -s.per_million)
        replacements[f"{{{{{token}_TOTAL}}}}"] = _rows(scores, position, 8, key=lambda s: -s.projected)

    for token, value in replacements.items():
        html = html.replace(token, value)
    path.write_text(html)
    return path
