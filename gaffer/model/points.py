"""Expected points: the number everything else in the engine compares.

Built the way the scoring table is built, one component at a time — appearance,
goals, assists, clean sheet, goals conceded, saves, defensive contribution,
bonus, cards — each scaled by expected minutes and by how the fixture looks for
the player's team. Adding them up is the projection.

Keeping the components separate rather than fitting one number to past points
matters: it means a defender at a newly solid club is re-rated as soon as the
team ratings move, without waiting for his own points to catch up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from gaffer.model.minutes import (MinutesModel, estimate as estimate_minutes,
                                  normalise_team)
from gaffer.model.scoring import SCORING
from gaffer.model.strength import LEAGUE_GOALS_PER_GAME, TeamStrength

# Expected bonus is the crudest component here. BPS-per-90 maps onto a share of
# the six bonus points each match distributes; these constants are a shape, not a
# calibration, and Phase 3's backtest is what should set them.
_BONUS_MIDPOINT = 32.0
_BONUS_SPREAD = 7.0
_BONUS_CEILING = 1.7


@dataclass
class ExpectedPoints:
    player_id: int
    gameweek: int
    opponent: str
    at_home: bool
    total: float
    minutes: float
    variance: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    # Parameters a simulation can draw from. An average cannot express that a
    # forward's six points are mostly a blank-or-haul lottery while a defender's
    # six are close to a certainty, and in a fifteen-player league that
    # difference decides who wins.
    draws: dict[str, float] = field(default_factory=dict)

    @property
    def sd(self) -> float:
        return math.sqrt(max(0.0, self.variance))

    def as_dict(self) -> dict:
        return asdict(self)


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _poisson_at_least(threshold: int, lam: float) -> float:
    """P(X >= threshold). Used for the defensive-contribution bonus, which is a
    threshold reward rather than a rate."""
    if lam <= 0:
        return 0.0
    if threshold <= 0:
        return 1.0
    below = sum(_poisson_pmf(k, lam) for k in range(threshold))
    return max(0.0, 1.0 - below)


def _conceded_deduction(lam: float) -> float:
    """Expected points lost to goals conceded: one per two, so the deduction is
    the expectation of floor(goals / 2) over a Poisson."""
    return -sum(_poisson_pmf(k, lam) * (k // SCORING.conceded_per_deduction) for k in range(0, 9))


def _expected_bonus(bps_per_90: float, share_of_match: float) -> float:
    if bps_per_90 <= 0:
        return 0.0
    logistic = 1.0 / (1.0 + math.exp(-(bps_per_90 - _BONUS_MIDPOINT) / _BONUS_SPREAD))
    return logistic * _BONUS_CEILING * share_of_match


def project_fixture(
    player: dict,
    position: str,
    fixture: dict,
    strength: TeamStrength,
    minutes: MinutesModel,
    team_short: dict[int, str],
) -> ExpectedPoints:
    """Expected points for one player in one fixture."""
    team_id = player["team"]
    opponent_id = fixture["opponent"]
    at_home = fixture["home"]

    if at_home:
        goals_for, goals_against = strength.expected_goals(team_id, opponent_id)
    else:
        goals_against, goals_for = strength.expected_goals(opponent_id, team_id)

    share = minutes.share_of_match
    # How much better or worse than a neutral fixture this is for his team.
    attack_context = goals_for / LEAGUE_GOALS_PER_GAME

    components: dict[str, float] = {}

    # Appearance: one point for playing, two for reaching the hour.
    components["appearance"] = (
        minutes.p_60 * SCORING.playing_60_plus
        + max(0.0, minutes.p_appear - minutes.p_60) * SCORING.playing_under_60
    )

    # Attacking returns, from his own Opta rates scaled by the fixture.
    xg90 = _as_float(player.get("expected_goals_per_90"))
    xa90 = _as_float(player.get("expected_assists_per_90"))
    expected_goals = xg90 * share * attack_context
    expected_assists = xa90 * share * attack_context
    components["goals"] = expected_goals * SCORING.goal_value(position)
    components["assists"] = expected_assists * SCORING.assist

    # Clean sheet — only counts if he is still on the pitch at the hour.
    clean_sheet_value = SCORING.clean_sheet_value(position)
    if clean_sheet_value:
        components["clean_sheet"] = math.exp(-goals_against) * minutes.p_60 * clean_sheet_value

    # Goals conceded, goalkeepers and defenders only.
    if position in SCORING.conceded_positions:
        components["conceded"] = _conceded_deduction(goals_against) * minutes.p_60

    # Saves. A goalkeeper facing more shots saves more, so scale by the fixture.
    if position == "GKP":
        saves90 = _as_float(player.get("saves_per_90"))
        defensive_context = goals_against / LEAGUE_GOALS_PER_GAME
        components["saves"] = saves90 * share * defensive_context / SCORING.saves_per_point

    # Defensive contribution: a threshold, so it needs a probability not a rate.
    threshold = SCORING.defcon_threshold.get(position, 999)
    if threshold < 999:
        defcon90 = _as_float(player.get("defensive_contribution_per_90"))
        expected_actions = defcon90 * share
        components["defcon"] = _poisson_at_least(threshold, expected_actions) * SCORING.defcon_points

    components["bonus"] = _expected_bonus(_rate_per_90(player, "bps"), share)

    # Discipline, from last season's rates.
    components["cards"] = (
        _rate_per_90(player, "yellow_cards") * share * SCORING.yellow_card
        + _rate_per_90(player, "red_cards") * share * SCORING.red_card
    )

    total = sum(components.values())

    # Spread, not just the average. A striker on 6 expected points is mostly a
    # blank-or-haul lottery; a defender on 6 is closer to a certainty. Anything
    # choosing between them for a small league needs to see the difference, so
    # each random component contributes its own variance: goals and assists are
    # counts (Poisson, variance equals the mean), clean sheets are a coin flip.
    goal_value = SCORING.goal_value(position)
    variance = expected_goals * goal_value ** 2 + expected_assists * SCORING.assist ** 2
    if clean_sheet_value:
        p_cs = math.exp(-goals_against) * minutes.p_60
        variance += p_cs * (1 - p_cs) * clean_sheet_value ** 2
    if minutes.p_appear < 1.0:
        variance += minutes.p_appear * (1 - minutes.p_appear) * SCORING.playing_60_plus ** 2
    variance += components.get("bonus", 0.0)  # bonus is lumpy; treat as count-like

    return ExpectedPoints(
        player_id=player["id"],
        gameweek=fixture["gameweek"],
        opponent=team_short.get(opponent_id, "?"),
        at_home=at_home,
        total=round(total, 3),
        minutes=minutes.expected_minutes,
        variance=round(variance, 3),
        components={k: round(v, 3) for k, v in components.items()},
        draws={
            "goal_rate": round(expected_goals, 4),
            "goal_value": goal_value,
            "assist_rate": round(expected_assists, 4),
            "assist_value": SCORING.assist,
            "clean_sheet_chance": round(math.exp(-goals_against) * minutes.p_60, 4),
            "clean_sheet_value": clean_sheet_value,
            "p_appear": round(minutes.p_appear, 4),
            "p_60": round(minutes.p_60, 4),
            # Everything else — bonus, saves, defensive contribution, cards, the
            # conceded deduction — is steady enough to carry at its average.
            "steady": round(
                total
                - components.get("goals", 0.0)
                - components.get("assists", 0.0)
                - components.get("clean_sheet", 0.0)
                - components.get("appearance", 0.0),
                4),
        },
    )


def squad_minutes(bootstrap: dict) -> dict[int, "MinutesModel"]:
    """Every player's minutes model, normalised so each club adds up to a match.

    Done club by club rather than player by player because the total is fixed:
    eleven players, ninety minutes. Modelling in isolation let a club's total
    drift to 118 minutes in GW1 while it went out and played 985.
    """
    by_team: dict[int, dict[int, MinutesModel]] = {}
    price: dict[int, float] = {}
    for player in bootstrap["elements"]:
        by_team.setdefault(player["team"], {})[player["id"]] = estimate_minutes(player)
        # The club's own valuation is the only read on who is first choice when
        # a promoted squad has no record to go on.
        price[player["id"]] = (player.get("now_cost") or 0) / 10.0

    out: dict[int, MinutesModel] = {}
    for models in by_team.values():
        weights = {pid: price.get(pid, 0.0) for pid in models}
        out.update(normalise_team(models, fallback_weight=weights))
    return out


# Three points, two and one, in every fixture. Ties can push a match slightly
# above this, but six is what the rules hand out and it does not depend on who
# earns it.
BONUS_PER_FIXTURE = 6.0


def normalise_bonus(bootstrap: dict,
                    projections: dict[int, list[ExpectedPoints]]) -> None:
    """Share out each fixture's six bonus points, in place.

    `_expected_bonus` scores a player against an absolute bps scale with no idea
    who else is on the pitch, so the totals do not add up: GW1 modelled 30 bonus
    points across the round when the rules awarded 64. Half the bonus in the
    game was going to nobody, and bonus turns up in almost every large
    under-prediction — a 17-point return is rarely 17 without it.

    Like the minutes normalisation this redistributes a fixed quantity rather
    than estimating one, so it cannot be fitted to a result. Relative order is
    untouched: whoever the logistic liked most still gets the most.
    """
    team_of = {p["id"]: p["team"] for p in bootstrap["elements"]}
    short = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

    fixtures: dict[tuple, list[ExpectedPoints]] = {}
    for player_id, runs in projections.items():
        team = short.get(team_of.get(player_id), "?")
        for run in runs:
            key = (run.gameweek, frozenset((team, run.opponent)))
            fixtures.setdefault(key, []).append(run)

    for runs in fixtures.values():
        total = sum(r.components.get("bonus", 0.0) for r in runs)
        if total <= 0:
            continue
        factor = BONUS_PER_FIXTURE / total
        for run in runs:
            before = run.components.get("bonus", 0.0)
            after = before * factor
            run.components["bonus"] = round(after, 3)
            run.total = round(run.total - before + after, 3)
            # Bonus is lumpy, and the variance term treats it as count-like.
            run.variance = round(max(0.0, run.variance - before + after), 3)


def project(
    bootstrap: dict,
    fixture_runs: dict[int, list[dict]],
    strength: TeamStrength,
    minutes_by_id: dict[int, "MinutesModel"] | None = None,
) -> dict[int, list[ExpectedPoints]]:
    """Expected points for every player across their team's upcoming fixtures."""
    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    team_short = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    if minutes_by_id is None:
        minutes_by_id = squad_minutes(bootstrap)

    projections: dict[int, list[ExpectedPoints]] = {}
    for player in bootstrap["elements"]:
        run = fixture_runs.get(player["team"], [])
        if not run:
            continue
        minutes = minutes_by_id.get(player["id"]) or estimate_minutes(player)
        position = positions[player["element_type"]]
        projections[player["id"]] = [
            project_fixture(player, position, fixture, strength, minutes, team_short)
            for fixture in run
        ]
    normalise_bonus(bootstrap, projections)
    return projections


def _rate_per_90(player: dict, field_name: str) -> float:
    minutes = player.get("minutes") or 0
    if minutes <= 0:
        return 0.0
    return (_as_float(player.get(field_name)) / minutes) * 90.0


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
