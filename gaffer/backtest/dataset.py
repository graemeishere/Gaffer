"""Assembling a history to test against.

The FPL API keeps per-gameweek logs only for the season in progress, but it does
keep per-season totals going back several years, and those totals carry the
underlying numbers — expected goals, expected assists, starts, bonus points. That
is enough to ask the question that matters: given what a player did last season,
does this model predict what he did next season better than the obvious
alternatives?

Two honest limits come with the data, and both are stated wherever results are
reported rather than buried here:

* **Survivor bias.** Only players still in the game today have histories to
  fetch. Anyone who dropped out of the Premier League is invisible, so the pool
  is tilted toward players who stayed good enough to remain in it.
* **No fixtures.** Which club a player turned out for in a past season is not
  exposed, so a historical projection cannot be adjusted for opponents. This
  therefore tests the player-rating half of the model, not the team-strength
  half.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from gaffer.ingest import FplClient

FETCH_WORKERS = 8

# Opta's underlying numbers only appear in FPL's history from 2022/23, and the
# defensive-contribution stat from 2024/25. Testing the model on a season whose
# *prior* season predates those is not a fair test — it is a test of a model
# with its inputs removed, which will lose to anything and tell you nothing.
FIRST_SEASON_WITH_XG = "2022/23"
FIRST_SEASON_WITH_DEFCON = "2024/25"


@dataclass(frozen=True)
class SeasonRow:
    """One player's totals for one completed season."""
    code: int
    name: str
    position: str
    season: str
    points: int
    minutes: int
    starts: int
    cost_start: float
    cost_end: float
    goals: int
    assists: int
    clean_sheets: int
    goals_conceded: int
    saves: int
    bonus: int
    bps: int
    yellow_cards: int
    red_cards: int
    expected_goals: float
    expected_assists: float
    expected_goals_conceded: float
    defensive_contribution: float

    @property
    def appearances(self) -> float:
        return self.minutes / 90.0

    def per_90(self, value: float) -> float:
        return (value / self.minutes * 90.0) if self.minutes else 0.0


def build_dataset(
    bootstrap: dict,
    client: FplClient | None = None,
    *,
    limit: int | None = None,
    progress=None,
) -> dict[tuple[int, str], SeasonRow]:
    """Fetch every player's season history, keyed by (player code, season).

    Keyed on `code` rather than `id` because ids are reassigned between seasons
    while the code follows the player.
    """
    client = client or FplClient()
    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    players = bootstrap["elements"][:limit] if limit else bootstrap["elements"]

    def fetch(player: dict):
        try:
            return player, client.player_summary(player["id"])
        except Exception:
            return player, None

    rows: dict[tuple[int, str], SeasonRow] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for player, summary in pool.map(fetch, players):
            done += 1
            if progress and done % 100 == 0:
                progress(done, len(players))
            if not summary:
                continue
            for past in summary.get("history_past", []):
                row = SeasonRow(
                    code=past.get("element_code") or player["code"],
                    name=player["web_name"],
                    position=positions[player["element_type"]],
                    season=past["season_name"],
                    points=past.get("total_points") or 0,
                    minutes=past.get("minutes") or 0,
                    starts=past.get("starts") or 0,
                    cost_start=(past.get("start_cost") or 0) / 10.0,
                    cost_end=(past.get("end_cost") or 0) / 10.0,
                    goals=past.get("goals_scored") or 0,
                    assists=past.get("assists") or 0,
                    clean_sheets=past.get("clean_sheets") or 0,
                    goals_conceded=past.get("goals_conceded") or 0,
                    saves=past.get("saves") or 0,
                    bonus=past.get("bonus") or 0,
                    bps=past.get("bps") or 0,
                    yellow_cards=past.get("yellow_cards") or 0,
                    red_cards=past.get("red_cards") or 0,
                    expected_goals=_as_float(past.get("expected_goals")),
                    expected_assists=_as_float(past.get("expected_assists")),
                    expected_goals_conceded=_as_float(past.get("expected_goals_conceded")),
                    defensive_contribution=_as_float(past.get("defensive_contribution")),
                )
                rows[(row.code, row.season)] = row
    return rows


def available_seasons(rows: dict[tuple[int, str], SeasonRow], minimum: int = 30) -> list[str]:
    """Seasons with enough players to be worth testing on."""
    counts: dict[str, int] = {}
    for (_, season) in rows:
        counts[season] = counts.get(season, 0) + 1
    return sorted(s for s, n in counts.items() if n >= minimum)


def previous_season(season: str) -> str:
    start = int(season.split("/")[0])
    return f"{start - 1}/{str(start)[-2:]}"


def _season_start(season: str) -> int:
    return int(season.split("/")[0])


def input_coverage(rows: dict[tuple[int, str], SeasonRow], season: str) -> dict[str, float]:
    """What share of a season's players carry each of the model's inputs."""
    players = [r for (_, s), r in rows.items() if s == season]
    if not players:
        return {"players": 0, "expected_goals": 0.0, "defensive_contribution": 0.0}
    return {
        "players": len(players),
        "expected_goals": sum(1 for r in players if r.expected_goals > 0) / len(players),
        "defensive_contribution":
            sum(1 for r in players if r.defensive_contribution > 0) / len(players),
    }


def testable_seasons(
    rows: dict[tuple[int, str], SeasonRow], minimum: int = 30
) -> list[str]:
    """Seasons the model can actually be judged on.

    A season qualifies when it has enough players *and* its prior season carries
    the underlying numbers the model reads. Without that filter the oldest
    seasons quietly test a blindfolded model and drag the verdict down with them.
    """
    out = []
    for season in available_seasons(rows, minimum):
        prior = previous_season(season)
        if _season_start(prior) < _season_start(FIRST_SEASON_WITH_XG):
            continue
        if input_coverage(rows, prior)["expected_goals"] < 0.5:
            continue
        out.append(season)
    return out


def season_pairs(
    rows: dict[tuple[int, str], SeasonRow],
    test_season: str,
    *,
    min_minutes: int = 900,
) -> list[tuple[SeasonRow, SeasonRow]]:
    """(what we knew, what happened) for every player with both seasons.

    `min_minutes` applies to the *prior* season only. Filtering on the test
    season would be hindsight — dropping players who then got injured is exactly
    the mistake that makes a backtest look better than the strategy really is.
    """
    prior_season = previous_season(test_season)
    pairs = []
    for (code, season), actual in rows.items():
        if season != test_season:
            continue
        prior = rows.get((code, prior_season))
        if prior and prior.minutes >= min_minutes:
            pairs.append((prior, actual))
    return pairs


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
