"""How much of a match do we expect this player to be on the pitch for?

This is the single largest source of error in any fantasy projection. A perfect
model of a player's talent paired with a naive minutes model loses to the
reverse — a brilliant forward who starts on the bench scores nothing, and no
amount of expected-goals precision fixes that.

Three numbers come out, and the points model needs all three: the chance he
appears at all, the chance he lasts an hour (which gates both the second
appearance point and clean-sheet points), and his expected minutes.
"""
from __future__ import annotations

from dataclasses import dataclass

# A typical start lasts most of the match; a substitute appearance is short.
MINUTES_PER_START = 82.0
MINUTES_PER_SUB = 18.0

# Share of starts that reach the hour mark.
START_SURVIVES_60 = 0.86

# Shrinkage for start rate, in games. A player with a handful of appearances is
# pulled toward the squad-wide base rate rather than trusted outright.
START_RATE_PRIOR_GAMES = 6.0
BASE_START_RATE = 0.45

SEASON_GAMES = 38


@dataclass
class MinutesModel:
    p_appear: float       # plays at all
    p_60: float           # reaches 60 minutes
    expected_minutes: float
    p_available: float    # fit and in the squad
    note: str = ""

    @property
    def share_of_match(self) -> float:
        return self.expected_minutes / 90.0


def _availability(player: dict) -> tuple[float, str]:
    """Fitness, straight from the flags FPL populates off club and press
    conference news. `chance_of_playing_next_round` is the club's own number."""
    status = player.get("status", "a")
    chance = player.get("chance_of_playing_next_round")
    news = (player.get("news") or "").strip()

    if chance is not None:
        return chance / 100.0, news
    if status == "a":
        return 1.0, news
    return {"d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}.get(status, 1.0), news


def estimate(player: dict, *, games_played: int = SEASON_GAMES) -> MinutesModel:
    """Build the minutes picture for one player.

    `games_played` is how many games the evidence covers. Callers hand this a
    record already expressed over a full season — see `gaffer.model.carryover`,
    which blends last season with this one before anything gets here, because
    the API zeroes these fields at the rollover and the model would otherwise
    have no evidence at all in August.
    """
    p_available, news = _availability(player)

    starts = float(player.get("starts") or 0)
    minutes = float(player.get("minutes") or 0)
    games = float(max(games_played, 1))

    # Start rate, shrunk toward the base rate by how much evidence we have.
    raw_start_rate = starts / games
    weight = games / (games + START_RATE_PRIOR_GAMES)
    start_rate = raw_start_rate * weight + BASE_START_RATE * (1 - weight)
    start_rate = max(0.0, min(1.0, start_rate))

    # Minutes not explained by starts came off the bench. This keeps regular
    # substitutes — who do score points — from being modelled as never playing.
    minutes_per_game = minutes / games
    sub_minutes = max(0.0, minutes_per_game - start_rate * MINUTES_PER_START)
    sub_rate = max(0.0, min(1.0 - start_rate, sub_minutes / MINUTES_PER_SUB))

    p_appear = p_available * min(1.0, start_rate + sub_rate)
    p_60 = p_available * start_rate * START_SURVIVES_60
    expected_minutes = p_available * (start_rate * MINUTES_PER_START + sub_rate * MINUTES_PER_SUB)

    return MinutesModel(
        p_appear=round(p_appear, 4),
        p_60=round(p_60, 4),
        expected_minutes=round(expected_minutes, 2),
        p_available=round(p_available, 3),
        note=news,
    )


MinutesModel.estimate = staticmethod(estimate)
