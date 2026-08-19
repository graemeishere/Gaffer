"""The strategies being compared.

Each one turns what was known before a season into a projected points total for
that season. The model is one entry among several deliberately obvious
alternatives — if it cannot beat "pick whoever scored most last year", it is not
earning its complexity.

The model strategy calls the real projection code rather than a copy of it. A
backtest that exercises a reimplementation tells you about the reimplementation.
"""
from __future__ import annotations

from gaffer.backtest.dataset import SeasonRow
from gaffer.model.minutes import estimate as estimate_minutes
from gaffer.model.points import project_fixture
from gaffer.model.strength import TeamStrength

SEASON_GAMES = 38

# A fixture against nobody in particular: both sides average, no home advantage.
# Past seasons do not expose which club a player turned out for, so the fixture
# half of the model cannot be exercised here and is held flat instead of guessed.
NEUTRAL = TeamStrength(
    attack={1: 1.0, 2: 1.0},
    defence={1: 1.0, 2: 1.0},
    home_advantage=1.0,
    matches_fitted=0,
    source="neutral",
)
NEUTRAL_FIXTURE = {"gameweek": 1, "opponent": 2, "home": True, "difficulty": 3}


def _as_player_dict(prior: SeasonRow) -> dict:
    """Shape a past season into what the projection code expects to be handed."""
    return {
        "id": prior.code,
        "team": 1,
        "minutes": prior.minutes,
        "starts": prior.starts,
        "status": "a",
        "chance_of_playing_next_round": None,
        "news": "",
        "expected_goals_per_90": prior.per_90(prior.expected_goals),
        "expected_assists_per_90": prior.per_90(prior.expected_assists),
        "expected_goals_conceded_per_90": prior.per_90(prior.expected_goals_conceded),
        "defensive_contribution_per_90": prior.per_90(prior.defensive_contribution),
        "saves_per_90": prior.per_90(prior.saves),
        "bps": prior.bps,
        "yellow_cards": prior.yellow_cards,
        "red_cards": prior.red_cards,
    }


def model_projection(prior: SeasonRow) -> float:
    """The engine's own projection, extended over a full season."""
    player = _as_player_dict(prior)
    minutes = estimate_minutes(player)
    one_game = project_fixture(
        player, prior.position, NEUTRAL_FIXTURE, NEUTRAL, minutes, {1: "A", 2: "B"})
    return one_game.total * SEASON_GAMES


def last_season_points(prior: SeasonRow) -> float:
    """What almost everyone actually does. The benchmark to beat."""
    return float(prior.points)


def points_per_minute(prior: SeasonRow) -> float:
    """Rate rather than volume — rewards players who were good when they played."""
    return prior.per_90(prior.points) * SEASON_GAMES


def minutes_played(prior: SeasonRow) -> float:
    """A deliberately dumb control: just pick whoever plays the most."""
    return float(prior.minutes)


def underlying_numbers(prior: SeasonRow) -> float:
    """Expected goals and assists only, ignoring what he actually converted."""
    involvement = prior.expected_goals + prior.expected_assists
    return involvement / max(prior.appearances, 1e-9) * SEASON_GAMES


STRATEGIES = {
    "model": model_projection,
    "last season's points": last_season_points,
    "points per 90": points_per_minute,
    "underlying xG + xA": underlying_numbers,
    "minutes played": minutes_played,
}
