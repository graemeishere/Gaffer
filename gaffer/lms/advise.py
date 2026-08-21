"""This week's pick, what it costs to take a different one, and why.

The recommendation is not "the club most likely to win on Saturday". It is the
club whose use this week leaves the best route through the rest of the season,
which is a different answer often enough to be the point of the exercise. So
every candidate is priced the way transfers are priced elsewhere in this
engine — by re-solving everything downstream of it and reporting the whole-route
difference, rather than the difference in the one number you can see.

There is a second dimension the fixture list cannot show you, and it is the one
that separates a good LMS entry from a good forecast. The pool pays one person.
Backing the same club as everybody else means surviving together or going out
together, and going out together usually means the round is void and replayed —
so the crowd pick is far safer than its price suggests early on, and close to
worthless late, when what you need is for the field to be eliminated without
you. Nothing about a private pool is published, so the field here is modelled,
not measured, and it is labelled that way everywhere it appears.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from gaffer import config
from gaffer.lms.odds import MatchOdds
from gaffer.lms.plan import Route, candidates, greedy_route, plan_route
from gaffer.lms.rules import Rules
from gaffer.lms.state import LmsState

# How many alternatives to price against the recommendation. Each one is a
# re-solve, and past a handful they are all clubs nobody would consider.
OPTIONS = 6

# How tightly the field clusters on the favourite. Lower means a pool that all
# picks the same club; this is a judgement, not a fit, because no pool publishes
# its picks.
CROWD_TEMPERATURE = 0.07

# The rest of the field has its own used list, which spreads the picks out well
# beyond what the odds alone would produce. This mixes the model of the crowd
# toward flat to account for it.
CROWD_SPREAD = 0.35


@dataclass
class Option:
    team: int
    name: str
    opponent: str
    home: bool
    win: float
    draw: float
    survival: float          # chance this pick survives this round
    route_survival: float    # chance of surviving the whole horizon after it
    # The share of the recommendation's route this pick gives up. Relative
    # rather than in percentage points, because eight rounds of survival
    # multiply down to a couple of percent and a gap of 0.2 points there is a
    # tenth of the run, not a rounding error.
    cost: float
    crowd: float             # modelled share of the field on this club
    reserved_for: int | None = None   # the round the plan would rather use it in

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("win", "draw", "survival", "route_survival", "cost", "crowd"):
            data[key] = round(data[key], 4)
        return data


@dataclass
class LmsAdvice:
    gameweek: int
    status: str                        # alive | out | no-fixtures
    pick: str | None = None
    pick_team: int | None = None
    reason: str = ""
    rules: dict = field(default_factory=dict)
    used: list[str] = field(default_factory=list)
    rounds_survived: int = 0
    lives_left: int = 0
    options: list[Option] = field(default_factory=list)
    route: dict | None = None
    greedy: dict | None = None
    planning_gain: float = 0.0
    field_survival: float = 0.0
    crowd_note: str = ""
    # A pick already recorded for a round still to be played. Set by the caller,
    # which knows the record; the advice then plans from the round after it.
    standing_pick: str | None = None
    standing_gameweek: int | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["options"] = [o.as_dict() for o in self.options]
        data["planning_gain"] = round(self.planning_gain, 4)
        data["field_survival"] = round(self.field_survival, 4)
        return data


def crowd_shares(odds: list[MatchOdds], rules: Rules) -> dict[int, float]:
    """A model of where a typical pool's picks land this round.

    A softmax over survival probability, flattened toward even. Entrants chase
    the shortest price, but they are each locked out of different clubs, so the
    real spread is wider than price alone would give.
    """
    if not odds:
        return {}
    best = max(o.survival(rules.draw_survives) for o in odds)
    weights = {
        o.team: math.exp((o.survival(rules.draw_survives) - best) / CROWD_TEMPERATURE)
        for o in odds
    }
    total = sum(weights.values())
    flat = 1.0 / len(odds)
    return {
        team: (1 - CROWD_SPREAD) * (w / total) + CROWD_SPREAD * flat
        for team, w in weights.items()
    }


def advise(
    rounds: dict[int, list[MatchOdds]],
    names: dict[int, str],
    state: LmsState,
    rules: Rules,
    *,
    from_gameweek: int | None = None,
) -> LmsAdvice:
    """Everything worth saying about the coming round, in one object."""
    used_names = [names.get(t, str(t)) for t in state.used]
    lives_left = max(0, rules.lives - state.lives_used(rules.draw_survives))
    base = dict(
        rules=rules.as_dict(),
        used=used_names,
        rounds_survived=state.rounds_survived(rules.draw_survives),
        lives_left=lives_left,
    )

    available = candidates(rounds, used=state.used, rules=rules,
                           from_gameweek=from_gameweek)
    gameweek = min(available) if available else (from_gameweek or 0)

    if not state.alive(rules.draw_survives, rules.lives):
        lost = next((p for p in state.picks
                     if p.survived(rules.draw_survives) is False), None)
        where = f" — {lost.name} in GW{lost.gameweek}" if lost else ""
        return LmsAdvice(gameweek=gameweek, status="out", **base,
                         reason=f"Eliminated{where}. Nothing left to plan.")

    if not available:
        return LmsAdvice(
            gameweek=gameweek, status="no-fixtures", **base,
            reason=("No club you have left has a fixture in the horizon. Either the "
                    "season is over or the used list has run the pool dry — most "
                    "pools reset it at that point."))

    this_round = available[gameweek]
    crowd = crowd_shares(this_round, rules)
    field_survival = sum(
        crowd.get(o.team, 0.0) * o.survival(rules.draw_survives) for o in this_round)

    route = plan_route(available, names, rules)
    greedy = greedy_route(available, names, rules)
    reserved = {p.team: p.gameweek for p in route.picks}

    options = _price_options(available, names, rules, route, crowd, reserved)
    best = options[0] if options else None

    advice = LmsAdvice(
        gameweek=gameweek,
        status="alive",
        pick=best.name if best else None,
        pick_team=best.team if best else None,
        options=options,
        route=route.as_dict(),
        greedy=greedy.as_dict(),
        planning_gain=route.survival - greedy.survival,
        field_survival=field_survival,
        **base,
    )
    advice.reason = _reason(advice, route, greedy, options, rules)
    advice.crowd_note = _crowd_note(advice, best, field_survival, rules)
    return advice


def _price_options(available, names, rules, route, crowd, reserved) -> list[Option]:
    """Every plausible pick for this round, valued over the whole route.

    The number that matters is `cost`: how much of the season's survival
    probability you hand back by taking this club now instead of the one the
    plan wants. A candidate two points shorter this week that wrecks three later
    rounds is a worse pick, and only a re-solve can show it.
    """
    gameweek = min(available)
    shortlist = sorted(available[gameweek],
                       key=lambda o: -o.survival(rules.draw_survives))[:OPTIONS]

    priced: list[Option] = []
    for odds in shortlist:
        forced = (route if route.first and route.first.team == odds.team
                  else plan_route(available, names, rules, force=(gameweek, odds.team)))
        # A forced pick that leaves a later round unfillable produces a shorter
        # route, and a shorter route multiplies to a *higher* number — which
        # would rank the pick that breaks the season above the one that does
        # not. Compare only over the same rounds: a route that cannot reach the
        # end of the horizon does not survive it.
        survives_horizon = forced.rounds >= route.rounds
        priced.append(Option(
            team=odds.team,
            name=names.get(odds.team, str(odds.team)),
            opponent=names.get(odds.opponent, str(odds.opponent)),
            home=odds.home,
            win=odds.win,
            draw=odds.draw,
            survival=odds.survival(rules.draw_survives),
            route_survival=forced.survival if survives_horizon else 0.0,
            cost=0.0,
            crowd=crowd.get(odds.team, 0.0),
            reserved_for=reserved.get(odds.team),
        ))

    priced.sort(key=lambda o: (-o.route_survival, -o.survival))
    best = priced[0].route_survival if priced else 0.0
    for option in priced:
        option.cost = 1.0 - option.route_survival / best if best > 0 else 0.0
        if option.reserved_for == gameweek:
            option.reserved_for = None
    return priced


def _reason(advice: LmsAdvice, route: Route, greedy: Route,
            options: list[Option], rules: Rules) -> str:
    """The argument for the pick, in the terms someone would actually argue it."""
    if not options:
        return "No eligible club this round."

    best = options[0]
    fixture = f"{'home to' if best.home else 'away at'} {best.opponent}"
    head = (f"{best.name} {fixture}, {best.survival:.0%} to survive "
            f"({best.win:.0%} win, {best.draw:.0%} draw"
            f"{' — which also survives' if rules.draw_survives else ' — which does not'}).")

    shortest = max(options, key=lambda o: o.survival)
    if shortest.team != best.team:
        held = (f", which the plan holds for GW{shortest.reserved_for}"
                if shortest.reserved_for else ", but spending them now costs more later")
        head += (f" {shortest.name} is the shorter price this week "
                 f"({shortest.survival:.0%}){held}. Taking them instead gives up "
                 f"{shortest.cost:.0%} of the route.")
    else:
        head += " The strongest club on the board is also the right one to spend."

    if route.status == "truncated":
        head += f" {route.note.capitalize()} — the pool is close to running dry."

    if greedy.rounds == route.rounds and greedy.survival > 0:
        ratio = route.survival / greedy.survival - 1.0
        if ratio > 0.01:
            head += (f" Planning the whole route rather than taking the best team "
                     f"each week survives all {route.rounds} rounds {ratio:.0%} more "
                     f"often ({route.survival:.1%} against {greedy.survival:.1%}).")
    return head


def _crowd_note(advice: LmsAdvice, best: Option | None,
                field_survival: float, rules: Rules) -> str:
    """Where this pick sits against the field — modelled, and labelled as such."""
    if not best:
        return ""
    edge = best.survival - field_survival
    with_field = "with" if best.crowd >= 0.2 else "away from"
    return (
        f"Modelled — no pool publishes its picks. Roughly {best.crowd:.0%} of a "
        f"typical field lands on {best.name}, so this is a pick {with_field} the "
        f"crowd. About {1 - field_survival:.0%} of the field goes out this round, "
        f"against your {1 - best.survival:.0%} — {edge:+.0%}. Early on that "
        f"matters little, because a round that eliminates everybody is usually "
        f"replayed; it matters most when the pool is down to a handful and you "
        f"need the others to go out without you."
    )


def season_advice(
    fixtures: list[dict],
    bootstrap: dict,
    strength,
    *,
    gameweek: int,
    rules: Rules,
    extra_used: str = "",
    state_path=None,
) -> LmsAdvice:
    """Advice from a run's data, with the saved record folded in.

    The one place that knows how a season's state becomes a recommendation, so
    the standalone command and the main engine cannot drift into giving
    different answers from the same inputs.
    """
    from gaffer.lms.odds import fixture_odds
    from gaffer.lms.state import read_state, resolve_many

    names = {t["id"]: t["name"] for t in bootstrap["teams"]}
    state = read_state(state_path)
    if state.settle(fixtures) and state.picks:
        # Results are read from the fixture list rather than typed in, so the
        # record has to be written back or every run re-derives them and the
        # season's history never actually accumulates anywhere.
        from gaffer.lms.state import write_state
        write_state(state, state_path)

    # Clubs named on the command line or in the environment are spent for this
    # run only — a way to ask "what if" without editing a season's history.
    for team in resolve_many(extra_used or config.LMS_USED, bootstrap["teams"]):
        state.borrow(team, names.get(team, str(team)))

    # A pick already logged for a round still to come is a decision, not a
    # suggestion: plan around it rather than propose a replacement.
    standing = state.pick_for(gameweek)
    plan_from = gameweek + 1 if standing else gameweek

    result = advise(fixture_odds(fixtures, strength), names, state, rules,
                    from_gameweek=plan_from)
    if standing:
        result.standing_pick = standing.name
        result.standing_gameweek = standing.gameweek
    return result
