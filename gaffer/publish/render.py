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
            "stage": "phase-1",
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
        # Reserved for later phases so the contract stays stable.
        "manager": None,
        "lineup": None,
        "transfers": [],
        "chips": [],
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
    }
    for position, token in (("GKP", "GKP"), ("DEF", "DEF"), ("MID", "MID"), ("FWD", "FWD")):
        replacements[f"{{{{{token}_VALUE}}}}"] = _rows(scores, position, 8, key=lambda s: -s.per_million)
        replacements[f"{{{{{token}_TOTAL}}}}"] = _rows(scores, position, 8, key=lambda s: -s.projected)

    for token, value in replacements.items():
        html = html.replace(token, value)
    path.write_text(html)
    return path
