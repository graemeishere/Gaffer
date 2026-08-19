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

from gaffer.model.minutes import MinutesModel, estimate as estimate_minutes
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
    components: dict[str, float] = field(default_factory=dict)

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
    return ExpectedPoints(
        player_id=player["id"],
        gameweek=fixture["gameweek"],
        opponent=team_short.get(opponent_id, "?"),
        at_home=at_home,
        total=round(total, 3),
        minutes=minutes.expected_minutes,
        components={k: round(v, 3) for k, v in components.items()},
    )


def project(
    bootstrap: dict,
    fixture_runs: dict[int, list[dict]],
    strength: TeamStrength,
) -> dict[int, list[ExpectedPoints]]:
    """Expected points for every player across their team's upcoming fixtures."""
    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    team_short = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

    projections: dict[int, list[ExpectedPoints]] = {}
    for player in bootstrap["elements"]:
        run = fixture_runs.get(player["team"], [])
        if not run:
            continue
        minutes = estimate_minutes(player)
        position = positions[player["element_type"]]
        projections[player["id"]] = [
            project_fixture(player, position, fixture, strength, minutes, team_short)
            for fixture in run
        ]
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
