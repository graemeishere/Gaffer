"""The team you have picked, before the API will admit to it.

The public Fantasy Premier League API only reveals a gameweek's picks once its
deadline has locked them. Between making your transfers and the deadline — often
several days — the only team the board can read is your last locked one, so it
shows a side you have already changed and goes on advising moves you have
already made.

This is the missing half: a small record of the team you *intend* to field,
which the run applies on top of the last locked squad so the board, the transfer
advice and the league comparison all work from the side you are actually going
to play. It is keyed to a gameweek and expires on its own — the moment the
deadline passes and the API reveals the real team, that gameweek is behind the
one being advised, the override no longer matches, and the real data wins with
nothing to clean up.

Everything here is deliberately strict. The override can arrive from a public
endpoint, so a stored file is treated as a claim to be checked, never as
instructions: fifteen real players, the right shape, a legal eleven, or it is
ignored and the board falls back to what the API says.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

from gaffer import config
from gaffer.optimise.squad import (LINEUP_MAX, LINEUP_MIN, LINEUP_SIZE,
                                   SQUAD_QUOTA)


@dataclass
class MyTeam:
    """A picked side, as fifteen player ids plus the roles across them."""
    gameweek: int
    players: list[int]        # all fifteen
    captain: int
    vice: int
    bench: list[int]          # the four, in auto-substitution order

    @property
    def starters(self) -> list[int]:
        bench = set(self.bench)
        return [p for p in self.players if p not in bench]

    def as_dict(self) -> dict:
        return asdict(self)


def load(path: Path | None = None) -> MyTeam | None:
    """Read the stored team, or None if there is none or it will not parse.

    A malformed file is never fatal: the board simply carries on with what the
    API returned. The shape is checked here; whether the *contents* are a legal
    squad is `validate`'s job, against the actual player list.
    """
    path = path or config.MYTEAM_OVERRIDE
    try:
        raw = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None
    try:
        return MyTeam(
            gameweek=int(raw["gameweek"]),
            players=[int(p) for p in raw["players"]],
            captain=int(raw["captain"]),
            vice=int(raw["vice"]),
            bench=[int(p) for p in raw["bench"]],
        )
    except (KeyError, TypeError, ValueError):
        return None


def validate(team: MyTeam, bootstrap: dict) -> tuple[bool, str]:
    """Is this a squad that could actually be fielded? (ok, reason).

    Checked against the real player list, so a made-up or mistyped id is caught,
    and against the same quotas the optimiser obeys, so a shape FPL would reject
    never reaches the board. The reason is returned for the endpoint to hand
    back and for the run to log when it declines one.
    """
    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    club = {p["id"]: p["team"] for p in bootstrap["elements"]}
    pos = {p["id"]: positions.get(p["element_type"]) for p in bootstrap["elements"]}

    ids = team.players
    if len(ids) != config.SQUAD_SIZE:
        return False, f"a squad is {config.SQUAD_SIZE} players, not {len(ids)}"
    if len(set(ids)) != len(ids):
        return False, "the same player appears twice"
    unknown = [p for p in ids if p not in pos]
    if unknown:
        return False, f"unknown player id(s): {', '.join(map(str, unknown))}"

    counts: dict[str, int] = {q: 0 for q in SQUAD_QUOTA}
    for p in ids:
        counts[pos[p]] = counts.get(pos[p], 0) + 1
    for position, quota in SQUAD_QUOTA.items():
        if counts.get(position, 0) != quota:
            return False, (f"need {quota} {position}, have {counts.get(position, 0)}")

    per_club: dict[int, int] = {}
    for p in ids:
        per_club[club[p]] = per_club.get(club[p], 0) + 1
    if any(n > config.MAX_PER_CLUB for n in per_club.values()):
        return False, f"more than {config.MAX_PER_CLUB} players from one club"

    held = set(ids)
    if team.captain not in held:
        return False, "the captain is not in the squad"
    if team.vice not in held:
        return False, "the vice-captain is not in the squad"
    if team.captain == team.vice:
        return False, "the captain and vice-captain are the same player"
    if len(team.bench) != config.SQUAD_SIZE - LINEUP_SIZE:
        return False, f"the bench is {config.SQUAD_SIZE - LINEUP_SIZE} players"
    if not set(team.bench) <= held:
        return False, "a benched player is not in the squad"
    if len(set(team.bench)) != len(team.bench):
        return False, "the same player is benched twice"

    # The eleven that is left has to be a shape FPL allows.
    shape: dict[str, int] = {q: 0 for q in SQUAD_QUOTA}
    for p in team.starters:
        shape[pos[p]] += 1
    if shape["GKP"] != 1:
        return False, "the eleven must have exactly one goalkeeper"
    for position in ("DEF", "MID", "FWD"):
        if not (LINEUP_MIN[position] <= shape[position] <= LINEUP_MAX[position]):
            return False, (f"{shape[position]} {position} in the eleven is not a "
                           f"legal formation")
    return True, "ok"


def save(team: MyTeam, path: Path | None = None) -> Path:
    """Write the team atomically, so a half-written file is never read.

    Written to a temporary file in the same directory and moved into place, which
    is atomic on the same filesystem — a concurrent run reads either the old team
    or the new one, never a truncated one.
    """
    path = Path(path or config.MYTEAM_OVERRIDE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".myteam-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(team.as_dict(), handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def clear(path: Path | None = None) -> bool:
    """Forget the stored team. True if there was one to remove."""
    path = Path(path or config.MYTEAM_OVERRIDE)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def as_actual(team: MyTeam, gameweek: int) -> dict:
    """The team in the shape the board's manager panel expects.

    Mirrors what `run` builds from an API picks payload — starters, bench,
    captain, vice, gameweek — with a `source` of "manual" so the page can say
    the side was entered by hand and the API has yet to confirm it.
    """
    return {
        "captain": team.captain,
        "vice": team.vice,
        "starters": team.starters,
        "bench": list(team.bench),
        "gameweek": gameweek,
        "source": "manual",
    }
