"""Keeping the model's evidence alive across a season rollover.

`bootstrap-static` is not a season store. Through pre-season its per-player
fields carry last season's record — minutes, starts, expected goals per 90, bps,
the lot — and the moment the new season starts every one of them is reset to
zero. The model read those fields directly, so on the morning of GW1 it lost
everything it knew in one go: the best projection in the game fell from 38
points to 3.8, every player in the league collapsed onto the same floor, and the
captaincy became an arbitrary tie-break that landed on a goalkeeper.

Nothing errored. That is the part worth designing against.

This module rebuilds a usable player record by blending last season's totals
with whatever this season has so far, weighted by how many games have actually
been played. Everything downstream — the minutes model, the points model, the
board — then works on that record unchanged.

One honest compromise: FPL's `history_past` carries goals and assists but no
expected goals or assists, so last season's xG90 and xA90 are proxied by his
actual scoring rate. Actual output is noisier than expected output over a
season, which overstates whoever finished above their chances and understates
whoever finished below. The board's existing confidence flag already reads off
minutes, so a thin record still shows as thin; the proxy is a reason to treat
August's numbers as a shortlist, which is what the banner says.
"""
from __future__ import annotations

from gaffer.model.minutes import SEASON_GAMES

# How fast this season's evidence takes over from last season's. At GW1 there is
# nothing to go on and last year carries the estimate entirely; by GW8 the two
# weigh about the same; by GW20 last year barely registers.
CARRYOVER_PRIOR_GAMES = 8.0

# Rates the points model reads per 90 minutes, mapped to the season total in
# `history_past` they can be rebuilt from.
PER_90_FROM_TOTAL = {
    "expected_goals_per_90": "goals",
    "expected_assists_per_90": "assists",
    "saves_per_90": "saves",
    "defensive_contribution_per_90": "defensive_contribution",
}

# Season totals the points model reads directly, dividing by minutes itself.
SEASON_TOTALS = ("bps", "yellow_cards", "red_cards", "minutes", "starts")


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def carryover_weight(games_played: float) -> float:
    """How much of the estimate this season carries, against last season.

    The prior decays as well as being outweighed: last season is worth about
    eight games in August and nothing at all by May. A fixed pseudo-count never
    quite goes away, which would leave a player who had not kicked a ball all
    year still reading against last season's form in the spring.
    """
    games = max(_f(games_played), 0.0)
    prior = CARRYOVER_PRIOR_GAMES * max(0.0, 1.0 - games / SEASON_GAMES)
    return games / (games + prior) if (games + prior) else 0.0


def _scaled_to_a_season(player: dict, games_played: float) -> dict:
    """This season's record alone, expressed over a full season.

    For a player with no prior season there is nothing to blend, but the totals
    still have to arrive downstream on the scale `estimate` expects. Before a
    ball is kicked there is genuinely nothing, and the player passes through
    untouched to meet the base rate.
    """
    games = _f(games_played)
    if games <= 0:
        return player
    out = dict(player)
    for field in SEASON_TOTALS:
        out[field] = _f(player.get(field)) / games * SEASON_GAMES
    return out


def effective_player(player: dict, history: dict | None, games_played: float) -> dict:
    """A player record the model can actually use, whatever the calendar says.

    Returns a copy with playing time and scoring rates blended from last season
    and this one. Availability is deliberately NOT blended — `status`, `news`
    and `chance_of_playing_next_round` describe him today, and last season has
    nothing to say about whether he is injured now.
    """
    prev_minutes = _f(history.get("minutes")) if history else 0.0
    weight = carryover_weight(games_played)

    if prev_minutes <= 0:
        # No prior top-flight minutes: a promotion, or a signing from abroad.
        # There is nothing to carry, but the record still has to be expressed
        # over a full season, because that is what `estimate` assumes it is
        # being handed. Returning the raw player here left a promoted-club
        # starter's 90 minutes divided by 38 games — 6.9 expected minutes for
        # someone who had just played the whole match, for the rest of the
        # season. Three clubs, 15% of the league, every week.
        return _scaled_to_a_season(player, games_played)

    if weight >= 1.0:
        return player

    out = dict(player)

    for field, total in PER_90_FROM_TOTAL.items():
        prev_rate = _f(history.get(total)) / prev_minutes * 90.0
        now_rate = _f(player.get(field))
        out[field] = round(now_rate * weight + prev_rate * (1 - weight), 4)

    # Totals are consumed as `total / minutes`, so both halves have to move
    # together or the ratio is nonsense — a full season of bonus points divided
    # by ninety minutes of football would read as a hundred points a game.
    for field in SEASON_TOTALS:
        prev_total = _f(history.get(field))
        now_total = _f(player.get(field))
        if games_played <= 0:
            out[field] = prev_total
        else:
            per_game = (now_total / games_played) * weight + \
                       (prev_total / SEASON_GAMES) * (1 - weight)
            out[field] = per_game * SEASON_GAMES

    out["_carryover_weight"] = round(weight, 3)
    return out
