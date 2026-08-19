"""Phase 0 ranking — a deliberately crude stand-in for the real model.

This is NOT expected points. It is last season's scoring rate, nudged by the
difficulty of the next few fixtures and by whether the player is fit. It exists
to prove the pipeline end to end and to give something usable before the season
starts. Phase 1 replaces it with a minutes model, team strength ratings and a
proper points decomposition.

Two honesty features are load-bearing:
  * every score carries a confidence, driven by sample size and club changes;
  * players who changed club this summer are flagged, because their prior-season
    numbers were earned somewhere else.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable

from gaffer import config


@dataclass
class PlayerScore:
    id: int
    name: str
    team: str
    position: str
    price: float
    owned: float
    projected: float      # proxy points over the horizon
    per_million: float    # projected points per £m
    fixture_score: float  # mean difficulty of the next N fixtures, 1 easy - 5 hard
    availability: float   # 0-1, our read on whether he plays
    confidence: str       # high | medium | low
    moved_club: bool
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def team_fixture_runs(fixtures: list[dict], horizon: int = config.HORIZON) -> dict[int, list[dict]]:
    """For each team, the next `horizon` fixtures with the difficulty they face."""
    upcoming = sorted(
        (f for f in fixtures if f.get("event") and not f.get("finished")),
        key=lambda f: (f["event"], f.get("kickoff_time") or ""),
    )
    runs: dict[int, list[dict]] = {}
    for f in upcoming:
        for team_id, is_home in ((f["team_h"], True), (f["team_a"], False)):
            run = runs.setdefault(team_id, [])
            if len(run) >= horizon:
                continue
            run.append({
                "gameweek": f["event"],
                "opponent": f["team_a"] if is_home else f["team_h"],
                "home": is_home,
                "difficulty": f["team_h_difficulty"] if is_home else f["team_a_difficulty"],
            })
    return runs


def _fixture_multiplier(run: list[dict]) -> tuple[float, float]:
    """Turn a fixture run into a multiplier. Average difficulty is 3, so an
    easier run scores above 1 and a harder one below. Clamped, because this is a
    nudge and should never dominate the player's own quality."""
    if not run:
        return 1.0, 3.0
    mean_difficulty = sum(f["difficulty"] for f in run) / len(run)
    multiplier = 1.0 + (3.0 - mean_difficulty) * 0.10
    return max(0.70, min(1.30, multiplier)), mean_difficulty


def _availability(player: dict) -> tuple[float, str]:
    """How likely is he to be available at all? Injury flags come straight from
    the API, which populates them from club and press-conference news."""
    status = player.get("status", "a")
    chance = player.get("chance_of_playing_next_round")

    if status == "a" and chance is None:
        return 1.0, ""
    if chance is not None:
        note = (player.get("news") or "").strip()
        return chance / 100.0, note
    return {"d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}.get(status, 1.0), (player.get("news") or "").strip()


def _base_rate(player: dict, baseline: float) -> tuple[float, str]:
    """Points per 90 last season, shrunk toward the positional baseline.

    A raw per-90 rate is nearly meaningless on small minutes: a substitute who
    scored twice in four cameos out-rates a striker who started every week. So we
    treat the baseline as prior evidence worth SHRINKAGE_APPEARANCES games and
    blend it in. A full season barely moves; four appearances move a long way.
    """
    minutes = player.get("minutes") or 0
    points = player.get("total_points") or 0
    appearances = minutes / 90.0
    k = config.SHRINKAGE_APPEARANCES

    rate = (points + baseline * k) / (appearances + k)

    if appearances >= 20:
        confidence = "high"
    elif appearances >= k:
        confidence = "medium"
    else:
        confidence = "low"
    return rate, confidence


def _playing_time(player: dict) -> float:
    """What share of a full season's minutes do we expect him to play?

    Phase 0 reads this straight off last season: how often he started, floored by
    his overall share of minutes so that regular substitutes are not zeroed out.
    Phase 1 replaces it with a real minutes model, which is the single biggest
    source of error in any FPL projection.
    """
    starts = player.get("starts") or 0
    minutes = player.get("minutes") or 0
    start_share = starts / config.SEASON_GAMES
    minute_share = minutes / (config.SEASON_GAMES * 90.0)
    return min(1.0, max(start_share, minute_share))


def _baseline_rate(players: Iterable[dict], position_id: int) -> float:
    """What a typical regular in this position scores per 90.

    Taken as the median across players with a real body of minutes, so it is not
    dragged around by either the elite or the cameo appearances. This is the
    value every player's own rate gets pulled toward.
    """
    rates = sorted(
        p["total_points"] / p["minutes"] * 90.0
        for p in players
        if p["element_type"] == position_id and (p.get("minutes") or 0) >= config.MIN_MINUTES_FOR_RATE
    )
    if not rates:
        return 2.0
    return rates[len(rates) // 2]


def rank_players(bootstrap: dict, fixtures: list[dict], horizon: int = config.HORIZON) -> list[PlayerScore]:
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    players = bootstrap["elements"]
    runs = team_fixture_runs(fixtures, horizon)
    baseline = {pid: _baseline_rate(players, pid) for pid in positions}

    scored: list[PlayerScore] = []
    for p in players:
        if p.get("status") == "u" and not (p.get("minutes") or 0):
            continue  # unavailable and never played — noise in the table

        run = runs.get(p["team"], [])
        multiplier, mean_difficulty = _fixture_multiplier(run)
        availability, news = _availability(p)
        rate, confidence = _base_rate(p, baseline[p["element_type"]])
        play_factor = _playing_time(p)

        moved = (p.get("team_join_date") or "") >= config.TRANSFER_WINDOW_START
        if moved and confidence == "high":
            # He has a real record, but it was earned at another club.
            confidence = "medium"

        # Rate per 90, scaled by how much of each gameweek we expect him to play.
        projected = rate * play_factor * multiplier * availability * len(run)
        price = p["now_cost"] / 10.0

        notes = []
        if moved:
            notes.append("new club — prior stats earned elsewhere")
        if news:
            notes.append(news)
        if confidence == "low" and not moved:
            notes.append("few appearances last season")
        elif play_factor < 0.5 and confidence != "low":
            notes.append("rotation risk — started under half of last season")

        scored.append(PlayerScore(
            id=p["id"],
            name=p["web_name"],
            team=teams[p["team"]],
            position=positions[p["element_type"]],
            price=price,
            owned=float(p.get("selected_by_percent") or 0),
            projected=round(projected, 2),
            per_million=round(projected / price, 3) if price else 0.0,
            fixture_score=round(mean_difficulty, 2),
            availability=round(availability, 2),
            confidence=confidence,
            moved_club=moved,
            note="; ".join(notes),
        ))

    scored.sort(key=lambda s: -s.projected)
    return scored
