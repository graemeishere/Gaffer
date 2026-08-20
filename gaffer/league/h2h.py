"""Head-to-head leagues, which are a different game from classic ones.

In a classic league everyone accumulates points all season and the highest total
wins, so the aim is to outscore the field and every extra point counts. A
head-to-head league draws you against one manager each gameweek: win and you take
three league points, draw one, lose nothing. **Margin is worthless.** Beating your
opponent by a single point pays exactly the same as beating them by fifty.

Three consequences, and each one inverts the advice a classic league would give:

* **Maximising expected points is not the objective.** What matters is the
  probability of outscoring one specific person, which is a different quantity —
  a squad with a lower average but a fatter tail can win more often.
* **Shared players are irrelevant.** A footballer you and your opponent both own
  cannot change the result no matter what he does. Only the difference between
  the two squads decides the match, so ownership is measured against your
  opponent alone, not the league.
* **Take risk when you are the underdog, not when you are ahead.** Facing a
  stronger squad, a predictable week loses; you need the spread. Facing a weaker
  one, variance is the only thing that can rob you of a win you should get.
  People reliably do the opposite.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from gaffer.ingest import FplClient

# FPL's head-to-head scoring.
WIN, DRAW, LOSS = 3, 1, 0


@dataclass
class Match:
    gameweek: int
    entry_1: int
    entry_1_name: str
    entry_1_points: int
    entry_2: int
    entry_2_name: str
    entry_2_points: int
    finished: bool

    def opponent_of(self, entry_id: int) -> tuple[int, str] | None:
        if self.entry_1 == entry_id:
            return self.entry_2, self.entry_2_name
        if self.entry_2 == entry_id:
            return self.entry_1, self.entry_1_name
        return None


@dataclass
class MatchOdds:
    opponent: int
    opponent_name: str
    gameweek: int
    p_win: float
    p_draw: float
    p_loss: float
    my_mean: float
    their_mean: float
    expected_league_points: float
    shared_players: int
    my_differentials: int
    their_differentials: int
    stance: str
    reason: str

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("p_win", "p_draw", "p_loss"):
            data[key] = round(data[key], 4)
        for key in ("my_mean", "their_mean", "expected_league_points"):
            data[key] = round(data[key], 2)
        return data


def is_head_to_head(standings: dict) -> bool:
    return standings.get("league", {}).get("scoring") == "h"


def read_league_any(league_id: int, client: FplClient | None = None) -> tuple[dict, str]:
    """Fetch a league's standings without being told which kind it is.

    The classic endpoint returns 404 for a head-to-head league rather than
    redirecting or explaining, which is indistinguishable from a league that does
    not exist — so try both before concluding anything.
    """
    client = client or FplClient()
    try:
        standings = client.league_standings(league_id)
        return standings, "classic"
    except Exception:
        pass
    standings = client.league_h2h_standings(league_id)
    return standings, "h2h"


def read_matches(league_id: int, client: FplClient | None = None,
                 *, max_pages: int = 10) -> list[Match]:
    """Every head-to-head fixture the league has published."""
    client = client or FplClient()
    matches: list[Match] = []
    page = 1
    while page <= max_pages:
        payload = client.league_h2h_matches(league_id, page=page)
        for row in payload.get("results", []):
            matches.append(Match(
                gameweek=row.get("event") or 0,
                entry_1=row.get("entry_1_entry") or 0,
                entry_1_name=row.get("entry_1_name") or "?",
                entry_1_points=row.get("entry_1_points") or 0,
                entry_2=row.get("entry_2_entry") or 0,
                entry_2_name=row.get("entry_2_name") or "?",
                entry_2_points=row.get("entry_2_points") or 0,
                finished=bool(row.get("winner")) or bool(row.get("is_knockout")) is False
                and (row.get("entry_1_points") or 0) + (row.get("entry_2_points") or 0) > 0,
            ))
        if not payload.get("has_next"):
            break
        page += 1
    return matches


def fixture_for(matches: list[Match], entry_id: int, gameweek: int) -> Match | None:
    """The one match that decides your week."""
    for match in matches:
        if match.gameweek == gameweek and match.opponent_of(entry_id):
            return match
    return None


def compare_squads(mine: list[int], theirs: list[int]) -> tuple[int, int, int]:
    """(shared, mine only, theirs only).

    Shared players are dead weight in a head-to-head: whatever they score lands
    on both sides of the scoreline and cancels out exactly.
    """
    a, b = set(mine), set(theirs)
    return len(a & b), len(a - b), len(b - a)


def advise_match(p_win: float, p_loss: float, shared: int, squad_size: int) -> tuple[str, str]:
    """Whether to court variance or suppress it, for this one match."""
    overlap = shared / squad_size if squad_size else 0.0

    if p_win >= 0.60:
        stance = "protect"
        reason = (f"You are the favourite at {p_win:.0%}. Margin pays nothing here, so "
                  "there is no reward for chasing a bigger score — only the risk of "
                  "handing back a win you should already have. Match their picks where "
                  "you reasonably can.")
    elif p_loss >= 0.55:
        stance = "gamble"
        reason = (f"You are the underdog at {p_win:.0%}. A predictable gameweek loses "
                  "this match; you need outcomes they do not share. Captain differently "
                  "and take the players they do not own.")
    else:
        stance = "balanced"
        reason = (f"Close to even at {p_win:.0%}. Neither courting nor avoiding variance "
                  "is clearly right — take the highest expected points and let it play.")

    if overlap >= 0.7:
        reason += (f" Note {shared} of {squad_size} players are shared, so they cannot "
                   "affect the result at all — the match turns on the handful that differ.")
    return stance, reason
