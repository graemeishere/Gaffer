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
LASTMAN_TEMPLATE = Path(__file__).parent / "lastman_template.html"
# One stylesheet behind both pages, injected rather than linked so each stays a
# single file that opens from disk with no server.
STYLESHEET = Path(__file__).parent / "style.css"


def _basis_warning(strength, games_played: int) -> str:
    """Say what the numbers are actually built on, this run.

    This sentence used to be hardcoded to "this season has not happened". It
    stopped being true the moment the season started, and the page went on
    asserting it while the model was in fact running on nothing at all — the
    API had cleared every rate overnight. A page that states its own basis
    wrongly is worse than one that stays quiet.
    """
    fitted = getattr(strength, "matches_fitted", 0)
    tail = (" Team strength is still last season's until this season has more "
            "results in.") if fitted <= 0 else ""

    if games_played <= 0:
        return ("The season hasn't kicked off yet, so every number here is built "
                "from last season's form." + tail)
    weeks = "gameweek" if games_played == 1 else "gameweeks"
    if games_played < 6:
        return (f"Only {games_played} {weeks} played so far — too early to lean on "
                f"this season, so the numbers still weigh last season heavily." + tail)
    if games_played < 20:
        return (f"These blend this season's {games_played} gameweeks with last "
                f"season, tilted toward what's actually happened.")
    return f"Built on this season's {games_played} gameweeks."


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
    manager=None,
    lms=None,
    review=None,
    games_played=None,
) -> dict:
    """Assemble the JSON contract. `meta`, `players` and `fixtures` fill first;
    `lineup`, `transfers` and `chips` arrive with the optimiser."""
    events = bootstrap["events"]
    nxt = next((e for e in events if e.get("is_next")), None)
    cur = next((e for e in events if e.get("is_current")), None)
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    if games_played is None:
        # Fallback for a caller without the fixtures to hand. The run passes the
        # authoritative count, from fixtures actually played rather than the
        # lagging event flag.
        games_played = sum(1 for e in events if e.get("finished"))

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
            "games_played": games_played,
            "warning": _basis_warning(strength, games_played),
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
        # A different game off the same fixture list and the same team ratings.
        # Null when the run skipped it, so the page can tell "not asked for"
        # from "asked for and there is nothing to say".
        "lms": lms,
        "schedule": {
            "phase": due.phase,
            "reason": due.reason,
            "hours_remaining": round(due.hours_remaining, 1),
        } if due else None,
        "manager": manager,
        # Last week's post-mortem. Null before any deadline has passed, which
        # the page distinguishes from "reviewed and there was nothing to say".
        "review": review,
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


def _review_block(payload: dict) -> str:
    """Last week's post-mortem.

    The spread carries the weight here. GW1 finished four points under the
    league mean and read like a model failure; the league's scores that week
    ranged 23 to 74, so it was a third of a standard deviation and meant
    nothing. A section that reported the gap without the spread would send the
    reader after a model that was working.
    """
    r = payload.get("review")
    if not r:
        return ("<p>No gameweek to review yet. This fills in once a deadline has "
                "passed and the squads that were actually fielded become public.</p>")

    flag = ("<div class='banner'>These are provisional numbers — the Fantasy "
            "Premier League has not settled this gameweek's bonus points yet, so "
            "the totals can still move.</div>") if r.get("provisional") else ""

    verdict = ("<div class='mock-h' style='margin-bottom:.5rem'>"
               f"GW{r['gameweek']} — {'an ordinary week' if r.get('within_normal_variation') else 'a real outlier'}"
               "</div>")

    stats = (
        "<div class='stats'>"
        f"<div class='stat'><b>{r['points']}</b><span>points scored</span></div>"
        f"<div class='stat'><b>{r['league_position']} of {r['league_size']}</b>"
        f"<span>in your league</span></div>"
        f"<div class='stat'><b>{r['league_mean']}</b><span>league average</span></div>"
        f"<div class='stat'><b>&plusmn;{r['league_spread']}</b>"
        f"<span>spread of scores &mdash; how far apart a normal week puts you</span></div>"
        "</div>"
    )

    rows = [f"<p>{r['verdict']}</p>"]

    if r.get("xi_projected"):
        rows.append(
            "<div class='opt'><div class='opt-l'><b>Your eleven</b>"
            f"<span>returned {r['xi_points']} against {r['xi_projected']} projected</span>"
            f"</div><div class='opt-r'>{r['xi_points']}</div></div>")

    if r.get("captain"):
        agreed = ("the model's own pick" if r.get("captain_agreed")
                  else "not what the model would have chosen")
        cost = (f" — {r['best_starter']} on {r['best_starter_points']} was the best in "
                f"your eleven" if r.get("captain_cost") else "")
        rows.append(
            f"<div class='opt'><div class='opt-l'><b>Captain: {r['captain']}</b>"
            f"<span>{r['captain_points']} points, {agreed}{cost}</span></div>"
            f"<div class='opt-r'>{r['captain_points'] * 2}</div></div>")

    rows.append(
        "<div class='opt'><div class='opt-l'><b>Bench</b>"
        f"<span>{r['points_on_bench']} points left on it"
        + (f", {r['auto_subs']} auto-sub(s) fired" if r.get("auto_subs") else "")
        + f"</span></div><div class='opt-r'>{r['points_on_bench']}</div></div>")

    diffs = r.get("differentials") or []
    if diffs:
        got = sum(d["points"] for d in diffs)
        names = ", ".join(f"{d['name']} ({d['ownership']}%, {d['points']})" for d in diffs)
        noun = "differential" if len(diffs) == 1 else "differentials"
        rows.append(
            f"<div class='opt'><div class='opt-l'><b>{len(diffs)} {noun}</b>"
            f"<span>{names}</span></div><div class='opt-r'>{got}</div></div>")

    return flag + verdict + stats + f"<div class='panel'>{''.join(rows)}</div>"


def _model_changes(payload: dict) -> dict | None:
    """What the model would change about the team you actually picked.

    Three kinds of change, kept together so the team sheet can point at them and
    the Squad changes tab can explain them from one source. Returns None when
    there is no picked team to compare against — before the deadline, when only
    the optimiser's suggested squad exists — so callers can stay quiet rather
    than dressing up the model's own picks as corrections to a team you have not
    entered.
    """
    manager = payload.get("manager") or {}
    actual = manager.get("actual") or {}
    model = payload.get("lineup") or {}
    if not (actual.get("starters") and model.get("starters")):
        return None
    a_start, m_start = actual["starters"], model["starters"]
    a_set, m_set = set(a_start), set(m_start)
    cap = ((model.get("captain"), actual.get("captain"))
           if model.get("captain") and actual.get("captain")
           and model["captain"] != actual["captain"] else None)
    vice = ((model.get("vice"), actual.get("vice"))
            if model.get("vice") and actual.get("vice")
            and model["vice"] != actual["vice"] else None)
    return {
        "subs_out": [p for p in a_start if p not in m_set],   # you start, model benches
        "subs_in": [p for p in m_start if p not in a_set],    # model starts, you bench
        "captain": cap,                                       # (model, yours)
        "vice": vice,                                         # (model, yours)
    }


def _has_changes(changes: dict | None) -> bool:
    return bool(changes and (changes["subs_out"] or changes["captain"] or changes["vice"]))


def _pitch(payload: dict) -> str:
    """The team sheet: your eleven as you picked it, with the model marked on it.

    Your team is the base and the model's opinion is an annotation, never a
    replacement. Showing only what the optimiser would field made the board
    impossible to act on — you could not see what to change, only what someone
    else would have done.
    """
    manager = payload.get("manager") or {}
    actual = manager.get("actual") or {}
    squad, lineup = payload.get("squad"), payload.get("lineup")

    if actual.get("starters"):
        starters, bench = list(actual["starters"]), list(actual["bench"])
        captain, vice = actual.get("captain"), actual.get("vice")
        gw = actual.get("gameweek")
        caption = (f"<p style='margin-bottom:.6rem'><strong>Your squad</strong>"
                   f"{' — ' + manager['name'] if manager.get('name') else ''}, "
                   f"as you picked it{f' in GW{gw}' if gw else ''}. "
                   f"Where the model disagrees it is noted in the margin.</p>")
    elif squad and lineup:
        starters, bench = list(lineup["starters"]), list(lineup["bench"])
        captain, vice = lineup.get("captain"), lineup.get("vice")
        caption = ("<p style='margin-bottom:.6rem'><strong>Suggested squad.</strong> "
                   "This is what the optimiser would buy, not your team — squads are "
                   "private until the deadline passes, after which yours replaces it.</p>")
    else:
        return "<p>No squad selected for this run.</p>"

    by_id = {p["id"]: p for p in payload["players"]}

    # The model's view, where there is one. It may be absent entirely: a run
    # that cannot build credible projections withholds its lineup rather than
    # publishing an arbitrary tie-break.
    model = lineup or {}
    has_model = bool(model.get("starters"))

    def card(pid: int, muted: bool = False) -> str:
        row = by_id.get(pid)
        if not row:
            return ""
        badge = ""
        if pid == captain:
            badge = "<span class='cap'>C</span>"
        elif pid == vice:
            badge = "<span class='cap vice'>V</span>"
        first = row["xp"][0] if row["xp"] else 0.0
        return (f"<div class='card{' muted' if muted else ''}'>{badge}"
                f"<b>{row['name']}</b><i>{row['team']} · £{row['price']:.1f}</i>"
                f"<u>{first:.1f}</u></div>")

    rows = []
    for position in ("GKP", "DEF", "MID", "FWD"):
        line = [pid for pid in starters if by_id.get(pid, {}).get("position") == position]
        line.sort(key=lambda pid: -(by_id[pid]["xp"][0] if by_id[pid]["xp"] else 0))
        if line:
            rows.append("<div class='row'>" + "".join(card(pid) for pid in line) + "</div>")

    # The team sheet stays a picture; the specifics of any change — and the
    # plain-English reason for each — live in the Squad changes tab, so they are
    # explained once rather than crammed into the margin here.
    changes = _model_changes(payload) if actual.get("starters") else None
    note = ""
    if _has_changes(changes):
        note = ("<div class='mock-h' style='margin-top:.6rem'>The model would "
                "make some changes — see the Squad changes tab.</div>")
    elif has_model and actual.get("starters"):
        note = ("<div class='mock-h' style='margin-top:.6rem'>The model would "
                "field this eleven, captain and vice too.</div>")

    bench_html = "".join(card(pid, muted=True) for pid in bench)
    bench_label = "Bench · your order" if actual.get("bench") else "Bench · auto-sub order"
    return (caption
            + f"<div class='pitch'>{''.join(rows)}</div>"
            + note
            + f"<div class='mock-h' style='margin-top:.8rem'>{bench_label}</div>"
            f"<div class='bench'>{bench_html}</div>")


def _transfer_rows(payload: dict) -> str:
    by_id = {p["id"]: p for p in payload["players"]}
    options = payload.get("transfers") or []
    horizon = payload["meta"]["horizon"]
    if not options:
        manager = payload.get("manager") or {}
        if manager.get("reason"):
            deadline = payload["meta"]["deadline"].replace("T", " ").replace(":00Z", " UTC")
            return (f"<div class='banner'><strong>Waiting on the deadline.</strong> "
                    f"{manager['reason']} Next one: {deadline}.</div>")
        return ("<p>No squad linked. Set <code>GAFFER_ENTRY</code> or pass "
                "<code>--entry YOUR_TEAM_ID</code> and this becomes transfer advice "
                "for your actual team.</p>")

    names = lambda ids: ", ".join(by_id[i]["name"] for i in ids if i in by_id)
    proj = lambda pid: (by_id.get(pid) or {}).get("projected", 0.0)
    out = []
    for i, option in enumerate(options):
        cls = " best" if i == 0 and option["net_gain"] > 0 else ""
        sign = "pos" if option["net_gain"] > 0.05 else ("neg" if option["net_gain"] < -0.05 else "neu")
        band = (f" <span class='band'>± {option['uncertainty']:.1f}</span>"
                if option["uncertainty"] else "")
        if not option["transfers"]:
            label = "Keep your transfer for next week"
            why = option["note"] or ("Nothing on the market beats your squad by enough "
                                     "to spend a transfer on this week.")
        else:
            label = f"{names(option['out'])} &rarr; {names(option['in'])}"
            in_proj = sum(proj(p) for p in option["in"])
            out_proj = sum(proj(p) for p in option["out"])
            in_phrase = (f"{names(option['in'])} combine for {in_proj:.1f}"
                         if len(option["in"]) > 1
                         else f"{names(option['in'])} is projected {in_proj:.1f}")
            out_phrase = (f"{names(option['out'])}'s combined {out_proj:.1f}"
                          if len(option["out"]) > 1
                          else f"{names(option['out'])}'s {out_proj:.1f}")
            cost = (f"after a {option['hit']}-point hit" if option.get("hit")
                    else "using a free transfer")
            # The stored note is only worth appending when it is not the generic
            # "costs a hit" / "uses a free transfer", which the cost clause
            # already says in full.
            extra = option.get("note", "")
            if extra.lower() in ("costs a hit", "uses a free transfer"):
                extra = ""
            why = (f"{in_phrase} over the next {horizon} gameweeks against "
                   f"{out_phrase}. The {option['net_gain']:+.2f} is the gain across "
                   f"your whole squad {cost}"
                   + (f" — {extra}" if extra else "") + ".")
        out.append(
            f"<div class='opt{cls}'><div class='opt-l'><b>{label}</b>"
            f"<span>{why}</span></div>"
            f"<div class='opt-r {sign}'>{option['net_gain']:+.2f}{band}</div></div>")
    return "".join(out)


def _squad_changes(payload: dict) -> str:
    """Everything the model would do to the team you picked, in three parts, each
    explained in plain words: who to start (substitutes), who to captain and vice,
    and which transfers to weigh. Built off `_model_changes`, so it says the same
    thing the team sheet points at."""
    by_id = {p["id"]: p for p in payload["players"]}
    horizon = payload["meta"]["horizon"]
    nm = lambda pid: (by_id.get(pid) or {}).get("name", "?")
    wk = lambda pid: (by_id.get(pid) or {}).get("xp", [0.0])[0] if by_id.get(pid) else 0.0
    changes = _model_changes(payload)

    def opt(label: str, why: str, right: str = "", sign: str = "neu") -> str:
        right_html = f"<div class='opt-r {sign}'>{right}</div>" if right else ""
        return (f"<div class='opt'><div class='opt-l'><b>{label}</b>"
                f"<span>{why}</span></div>{right_html}</div>")

    # --- Substitutes ---------------------------------------------------------
    subs: list[str] = []
    if changes and (changes["subs_out"] or changes["subs_in"]):
        pos = lambda pid: (by_id.get(pid) or {}).get("position", "?")
        outs: dict[str, list[int]] = {}
        ins: dict[str, list[int]] = {}
        for p in changes["subs_out"]:
            outs.setdefault(pos(p), []).append(p)
        for p in changes["subs_in"]:
            ins.setdefault(pos(p), []).append(p)
        pairs: list[tuple[int, int]] = []
        rem_out: list[int] = []
        rem_in: list[int] = []
        # Like-for-like first (a DEF for a DEF), which reads most clearly.
        for position in ("GKP", "DEF", "MID", "FWD"):
            o = sorted(outs.get(position, []), key=wk)               # weakest you start first
            n = sorted(ins.get(position, []), key=wk, reverse=True)  # strongest you bench first
            k = min(len(o), len(n))
            pairs += list(zip(o[:k], n[:k]))
            rem_out += o[k:]
            rem_in += n[k:]
        # Whatever is left is a change of shape — a defender in for a midfielder,
        # say. Pair the best coming in with the weakest going out so nothing is
        # silently dropped from the list.
        rem_out.sort(key=wk)
        rem_in.sort(key=wk, reverse=True)
        pairs += list(zip(rem_out, rem_in))
        for out_pid, in_pid in pairs:
            gain = wk(in_pid) - wk(out_pid)
            subs.append(opt(
                f"Start {nm(in_pid)}, bench {nm(out_pid)}",
                f"{nm(in_pid)} is projected {wk(in_pid):.1f} points this gameweek "
                f"against {nm(out_pid)}'s {wk(out_pid):.1f}.",
                f"{gain:+.1f}", "pos" if gain > 0.05 else "neu"))
    if not changes:
        subs_body = ("<p>Link your team and this shows which of your fifteen to "
                     "start each week.</p>")
    elif subs:
        subs_body = "".join(subs)
    else:
        subs_body = "<p>The model would field the same eleven you picked.</p>"

    # --- Captaincy -----------------------------------------------------------
    caps: list[str] = []
    if changes:
        if changes["captain"]:
            m_cap, a_cap = changes["captain"]
            caps.append(opt(
                f"Captain {nm(m_cap)}, not {nm(a_cap)}",
                f"The armband doubles one player's score, so it goes to {nm(m_cap)} — "
                f"the best return once both this week's fixture and his scoring over "
                f"the next {horizon} gameweeks are weighed, rather than chasing a single "
                f"good matchup.", "2&times;", "pos"))
        if changes["vice"]:
            m_vice, a_vice = changes["vice"]
            caps.append(opt(
                f"Make {nm(m_vice)} vice, not {nm(a_vice)}",
                f"Your vice only scores if your captain does not play, so it wants the "
                f"strongest sure starter of the rest — {nm(m_vice)}, projected "
                f"{wk(m_vice):.1f} this gameweek."))
    if changes and not caps:
        caps.append("<p>Your captain and vice already match the model's picks.</p>")
    caps_body = "".join(caps) if caps else (
        "<p>Link your team to see who to captain — squads are private until the "
        "deadline passes.</p>")

    # --- Transfers -----------------------------------------------------------
    transfers_body = _transfer_rows(payload)

    return (
        "<h3>Substitutes</h3>"
        "<p>Who to start and who to bench for this gameweek, from the fifteen you "
        "already own. No transfer needed — this is just the eleven that scores most.</p>"
        f"<div class='panel'>{subs_body}</div>"
        "<h3 style='margin-top:1.2rem'>Captain &amp; vice</h3>"
        "<p>The captain scores double; the vice takes over only if the captain does "
        "not play.</p>"
        f"<div class='panel'>{caps_body}</div>"
        "<h3 style='margin-top:1.2rem'>Transfers</h3>"
        "<p>Options priced against each other over the next "
        f"{horizon} gameweeks, after any points hit. Each gain is across your whole "
        "squad — which is why upgrading someone who was correctly going to sit on the "
        "bench comes out at nothing. The <b>&plusmn;</b> after a gain is how far it "
        "could swing either way.</p>"
        f"<div class='panel'>{transfers_body}</div>")


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


def _h2h_block(league: dict) -> str:
    """A head-to-head league turns on one opponent, not the whole field."""
    match = league.get("match")
    if not match:
        return (f"<div class='banner'><strong>{league['name']}</strong> is a "
                "head-to-head league, so one opponent decides each week rather than "
                "the whole table. Fixtures are published when the league starts. Once "
                "they are, this shows who you are drawn against, your chance of "
                "beating them, and whether to take risk on or squeeze it out — which "
                "in head-to-head depends on whether you are favourite, not on where "
                "you sit in the table.</div>")

    tone = {"protect": "go", "gamble": "stop"}.get(match["stance"], "neu")
    return (
        f"<div class='stats' style='margin-bottom:.5rem;'>"
        f"<div class='stat'><b>{match['p_win']:.0%}</b>"
        f"<span>chance of beating {match['opponent_name']}</span></div>"
        f"<div class='stat'><b>{match['my_mean']:.0f} v {match['their_mean']:.0f}</b>"
        f"<span>projected gameweek points</span></div>"
        f"<div class='stat'><b>{match['expected_league_points']:.2f}</b>"
        f"<span>of 3 league points expected</span></div>"
        f"<div class='stat'><b>{match['shared_players']}</b>"
        f"<span>shared players — cannot change the result</span></div></div>"
        f"<div class='panel' style='margin-bottom:.6rem;'>"
        f"<div class='opt'><div class='opt-l'><b>Win</b>"
        f"<span>3 league points</span></div>"
        f"<div class='opt-r pos'>{match['p_win']:.0%}</div></div>"
        f"<div class='opt'><div class='opt-l'><b>Draw</b>"
        f"<span>1 league point</span></div>"
        f"<div class='opt-r neu'>{match['p_draw']:.0%}</div></div>"
        f"<div class='opt'><div class='opt-l'><b>Loss</b>"
        f"<span>nothing</span></div>"
        f"<div class='opt-r neg'>{match['p_loss']:.0%}</div></div></div>"
        f"<div class='banner'><strong>{match['stance'].title()}.</strong> "
        f"{match['reason']}</div>"
    )


def _league_block(payload: dict) -> str:
    league = payload.get("league")
    if not league:
        return ("<p>No mini-league linked. Pass <code>--league YOUR_LEAGUE_ID</code> once "
                "the first deadline has passed and this fills with your rivals' actual "
                "squads, your win probability against them, and whether to be taking risk "
                "or squeezing it out. Classic and head-to-head leagues are detected "
                "automatically — they need entirely different advice.</p>")
    if league.get("kind") == "h2h":
        return _h2h_block(league)

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


def _lms_route(route: dict) -> str:
    """The planned route as a chain of rounds, so the reservations are visible.

    The reason a club is not this week's pick is almost always that the plan
    wants it later, and that argument only lands if you can see where.
    """
    picks = (route or {}).get("picks") or []
    if not picks:
        return ""
    cards = "".join(
        f"<div class='card'><b>{p['name']}</b>"
        f"<i>GW{p['gameweek']} · {'v' if p['home'] else 'at'} {p['opponent']}</i>"
        f"<u>{p['survival']:.0%}</u></div>"
        for p in picks
    )
    return (f"<div class='mock-h' style='margin-top:.8rem'>The route · "
            f"{route['survival']:.1%} chance of surviving all {route['rounds']} "
            f"rounds</div><div class='bench'>{cards}</div>")


def _lms_block(payload: dict) -> str:
    """Last Man Standing: one club a week, each usable once, a draw is a defeat."""
    lms = payload.get("lms")
    if not lms:
        return ("<p>Not planned on this run. It costs nothing extra — the route "
                "is built from the same fixture list and the same team ratings as "
                "the board above — so it is on by default and only <code>--no-lms</code> "
                "turns it off.</p>")

    rules = lms.get("rules") or {}
    used = lms.get("used") or []
    used_html = ("".join(f"<span class='fx d5'>{name}</span>" for name in used)
                 if used else "<span class='sub'>nothing yet</span>")
    standing = (f"<div class='banner' style='margin-bottom:.5rem'><strong>"
                f"GW{lms['standing_gameweek']} is already picked: {lms['standing_pick']}."
                f"</strong> The route below plans around it rather than proposing a "
                f"replacement for a club it can no longer use.</div>"
                if lms.get("standing_pick") else "")

    if lms["status"] != "alive":
        tone = "stop" if lms["status"] == "out" else "warn"
        return (standing + f"<div class='banner'><strong>"
                f"<span class='pill {tone}'>{lms['status']}</span></strong> "
                f"{lms['reason']}</div>"
                f"<div class='mock-h' style='margin-top:.8rem'>Clubs used</div>"
                f"<div>{used_html}</div>")

    options = lms.get("options") or []
    rows = []
    for i, option in enumerate(options):
        fixture = f"{'v' if option['home'] else 'at'} {option['opponent']}"
        held = (f"<span class='pill warn'>held for GW{option['reserved_for']}</span>"
                if option.get("reserved_for") else "")
        cost = ("<span class='sub'>—</span>" if i == 0
                else f"<span class='neg'>-{option['cost']:.0%}</span>")
        rows.append(
            f"<tr><td>{option['name']}</td><td class='sub'>{fixture}</td>"
            f"<td class='num'>{option['win']:.0%}</td>"
            f"<td class='num'>{option['draw']:.0%}</td>"
            f"<td class='num'>{option['survival']:.0%}</td>"
            f"<td class='num'>{option['route_survival']:.1%}</td>"
            f"<td class='num'>{cost}</td>"
            f"<td class='num'>{option['crowd']:.0%}</td>"
            f"<td>{held}</td></tr>")

    best = options[0] if options else {}
    return (
        standing
        + f"<div class='stats' style='margin-bottom:.5rem;'>"
        f"<div class='stat'><b>{lms['pick']}</b><span>the pick for GW{lms['gameweek']}</span></div>"
        f"<div class='stat'><b>{best.get('survival', 0):.0%}</b>"
        f"<span>chance it survives the round</span></div>"
        f"<div class='stat'><b>{(lms.get('route') or {}).get('survival', 0):.1%}</b>"
        f"<span>chance of surviving all {(lms.get('route') or {}).get('rounds', 0)} "
        f"planned rounds</span></div>"
        f"<div class='stat'><b>{len(used)}</b><span>clubs already spent</span></div></div>"
        f"<div class='banner'><strong>{lms['pick']}.</strong> {lms['reason']}</div>"
        f"<div class='mock-h' style='margin-top:.8rem'>This round, priced over the whole route</div>"
        f"<div class='scroll'><table>"
        f"<thead><tr><th>Club</th><th>Fixture</th><th class='num'>Win</th>"
        f"<th class='num'>Draw</th><th class='num'>Survive</th>"
        f"<th class='num'>Route</th><th class='num'>Cost</th>"
        f"<th class='num'>Field</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        + _lms_route(lms.get("route") or {})
        + f"<div class='mock-h' style='margin-top:.8rem'>Clubs used · "
        f"{'one life' if rules.get('lives', 1) == 1 else str(rules['lives']) + ' lives'}, "
        f"{'a draw survives' if rules.get('draw_survives') else 'a draw is out'}</div>"
        f"<div>{used_html}</div>"
        f"<div class='banner' style='margin-top:.6rem'><strong>Against the field.</strong> "
        f"{lms.get('crowd_note', '')}</div>"
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
        "{{STYLE}}": STYLESHEET.read_text(),
        "{{GAMEWEEK}}": str(meta["gameweek"]),
        # The board is the plan for the gameweek ahead; before a ball is kicked
        # that is the opener. Never "pre-season" once the season is under way.
        "{{HEADLINE_SUFFIX}}": ("the season opener" if meta.get("games_played", 0) <= 0
                                else "the week ahead"),
        # Name the gameweek that just finished when there is one; before the
        # first deadline there is nothing to review and it reads generically.
        "{{REVIEW_HEADING}}": (f"Gameweek {payload['review']['gameweek']} — how it went"
                               if payload.get("review") else "Your last gameweek"),
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
        "{{REVIEW}}": _review_block(payload),
        "{{CHIPS}}": _chip_rows(payload),
        "{{LEAGUE}}": _league_block(payload),
        "{{PHASE_REASON}}": (payload.get("schedule") or {}).get("reason", ""),
        "{{SQUAD_CHANGES}}": _squad_changes(payload),
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


def write_lastman(payload: dict, path: Path | None = None) -> Path:
    """The Last Man Standing page, standalone and readable from disk.

    Same data as the `lms` block in `latest.json` — the JSON stays the contract
    and this is one more reader of it, not a second source of truth.
    """
    path = path or config.LASTMAN_OUT
    path.parent.mkdir(parents=True, exist_ok=True)

    meta, lms = payload["meta"], payload.get("lms") or {}
    rules = lms.get("rules") or {}
    deadline = datetime.fromisoformat(meta["deadline"].replace("Z", "+00:00"))
    generated = datetime.fromisoformat(meta["generated"])
    hours_left = (deadline - generated).total_seconds() / 3600

    lives = rules.get("lives", 1)
    rule_summary = (
        f"{'DRAW SURVIVES' if rules.get('draw_survives') else 'DRAW IS OUT'} · "
        f"{'1 LIFE' if lives == 1 else f'{lives} LIVES'}"
    ) if rules else "—"

    html = LASTMAN_TEMPLATE.read_text()
    replacements = {
        "{{STYLE}}": STYLESHEET.read_text(),
        "{{GAMEWEEK}}": str(lms.get("gameweek") or meta["gameweek"]),
        "{{DEADLINE}}": deadline.strftime("%a %d %b, %H:%M UTC"),
        "{{COUNTDOWN}}": f"{hours_left:.0f}h" if hours_left > 0 else "passed",
        "{{GENERATED}}": generated.strftime("%d %b %Y, %H:%M UTC"),
        "{{RULES}}": rule_summary,
        # Plain English, not "PRIOR, 0 MATCHES". Last season until this one has
        # results, then how many games in.
        "{{STRENGTH}}": ("last season's form" if meta.get("matches_fitted", 0) <= 0
                         else f"{meta['matches_fitted']} matches in"),
        "{{LMS}}": _lms_block(payload),
        "{{PHASE_REASON}}": (payload.get("schedule") or {}).get("reason", ""),
    }
    for token, value in replacements.items():
        html = html.replace(token, value)
    path.write_text(html)
    return path
