"""Assembling projections into the ranked board the report and page render.

This is presentation, not modelling: it flattens per-gameweek expected points
into one row per player, attaches the flags a reader needs to judge the number,
and sorts. Everything numeric arrives from `gaffer.model`.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from gaffer import config
from gaffer.model.minutes import estimate as estimate_minutes
from gaffer.model.points import ExpectedPoints


@dataclass
class PlayerRow:
    id: int
    name: str
    team: str
    position: str
    price: float
    owned: float
    xp: list[float]        # one entry per gameweek in the horizon
    var: list[float]       # spread on each, so a band travels with every number
    projected: float       # their sum
    per_million: float
    minutes: float         # expected minutes per match
    fixture_score: float   # mean model difficulty of the run, 1 easy to 5 hard
    availability: float
    confidence: str
    moved_club: bool
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def _confidence(player: dict, moved: bool) -> str:
    """How much weight the reader should put on this row.

    Driven by how much evidence sits behind the player's rates, then knocked down
    a level if he changed club — his numbers were earned somewhere else.
    """
    appearances = (player.get("minutes") or 0) / 90.0
    if appearances >= 20:
        level = "high"
    elif appearances >= 8:
        level = "medium"
    else:
        level = "low"
    if moved and level == "high":
        level = "medium"
    return level


def build_board(
    bootstrap: dict,
    projections: dict[int, list[ExpectedPoints]],
    strength,
) -> list[PlayerRow]:
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}

    rows: list[PlayerRow] = []
    for player in bootstrap["elements"]:
        runs = projections.get(player["id"])
        if not runs:
            continue
        if player.get("status") == "u" and not (player.get("minutes") or 0):
            continue  # unavailable and never played — noise on the board

        minutes = estimate_minutes(player)
        moved = (player.get("team_join_date") or "") >= config.TRANSFER_WINDOW_START
        price = player["now_cost"] / 10.0
        xp = [round(r.total, 2) for r in runs]
        var = [round(r.variance, 3) for r in runs]
        projected = sum(xp)

        difficulty = [
            strength.difficulty(player["team"], _opponent_id(bootstrap, r.opponent), r.at_home)
            for r in runs
        ]

        notes = []
        if moved:
            notes.append("new club — prior stats earned elsewhere")
        if minutes.note:
            notes.append(minutes.note)
        if minutes.p_60 < 0.4 and minutes.p_available > 0:
            notes.append("rotation risk")

        rows.append(PlayerRow(
            id=player["id"],
            name=player["web_name"],
            team=teams[player["team"]],
            position=positions[player["element_type"]],
            price=price,
            owned=float(player.get("selected_by_percent") or 0),
            xp=xp,
            var=var,
            projected=round(projected, 2),
            per_million=round(projected / price, 3) if price else 0.0,
            minutes=minutes.expected_minutes,
            fixture_score=round(sum(difficulty) / len(difficulty), 2) if difficulty else 3.0,
            availability=minutes.p_available,
            confidence=_confidence(player, moved),
            moved_club=moved,
            note="; ".join(notes),
        ))

    rows.sort(key=lambda r: -r.projected)
    return rows


def _opponent_id(bootstrap: dict, short_name: str) -> int:
    for t in bootstrap["teams"]:
        if t["short_name"] == short_name:
            return t["id"]
    return 0
