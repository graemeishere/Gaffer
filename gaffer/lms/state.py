"""What you have already used, and whether you are still in.

The used list is the whole game. A projection that ignores it recommends the
same three clubs every week, which is precisely the advice that gets people
knocked out in October having spent every good team on fixtures they would have
survived anyway.

Nothing about a pool is public — it is a spreadsheet in someone's inbox — so the
record has to be kept locally and by hand. It lives beside the prediction log
for the same reason that does: the machines this runs on are disposable, and a
season's picks that exist only in a container about to be reclaimed are the same
as no picks at all.

Results are not entered by hand. A pick is a club and a gameweek, the fixture
list says what that club did, so the engine settles its own record — which also
means it can tell you that you are out rather than cheerfully planning a route
for someone who was eliminated on Saturday.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from gaffer import config

PENDING = "pending"
WON = "won"
DREW = "drew"
LOST = "lost"


@dataclass
class Pick:
    gameweek: int
    team: int
    name: str
    result: str = PENDING

    def survived(self, draw_survives: bool) -> bool | None:
        """True, False, or None while the fixture has not been played."""
        if self.result == PENDING:
            return None
        if self.result == WON:
            return True
        return self.result == DREW and draw_survives

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LmsState:
    picks: list[Pick] = field(default_factory=list)
    note: str = ""

    # ---- reading -------------------------------------------------------

    @property
    def used(self) -> list[int]:
        """Team IDs that are spent, in the order they were spent."""
        return [p.team for p in self.picks]

    def lives_used(self, draw_survives: bool) -> int:
        return sum(1 for p in self.picks if p.survived(draw_survives) is False)

    def alive(self, draw_survives: bool, lives: int) -> bool:
        return self.lives_used(draw_survives) < lives

    def rounds_survived(self, draw_survives: bool) -> int:
        return sum(1 for p in self.picks if p.survived(draw_survives) is True)

    def pick_for(self, gameweek: int) -> Pick | None:
        return next((p for p in self.picks if p.gameweek == gameweek), None)

    # ---- writing -------------------------------------------------------

    def record(self, gameweek: int, team: int, name: str) -> Pick:
        """Log a pick, replacing any earlier one for the same round.

        Replacing rather than appending is deliberate: changing your mind before
        the deadline is normal, and a second row for the same gameweek would
        silently burn a club you never actually used.
        """
        self.picks = [p for p in self.picks if p.gameweek != gameweek]
        pick = Pick(gameweek=gameweek, team=team, name=name)
        self.picks.append(pick)
        self.picks.sort(key=lambda p: p.gameweek)
        return pick

    def borrow(self, team: int, name: str) -> None:
        """Treat a club as spent for this run without claiming it was played.

        Used for clubs named on the command line. The outcome stays unknown, so
        it neither costs a life nor counts as a round survived — it only takes
        the club off the board, which is the whole point of saying it.
        """
        if team in self.used:
            return
        self.picks.append(Pick(gameweek=0, team=team, name=name))

    def settle(self, fixtures: list[dict]) -> int:
        """Fill in results for picks whose fixture has now been played."""
        settled = 0
        for pick in self.picks:
            if pick.result != PENDING:
                continue
            if pick.gameweek <= 0:
                continue   # borrowed for this run only; there is no fixture to read
            outcome = _result_for(fixtures, pick.gameweek, pick.team)
            if outcome:
                pick.result = outcome
                settled += 1
        return settled

    def as_dict(self) -> dict:
        return {"picks": [p.as_dict() for p in self.picks], "note": self.note}


def _result_for(fixtures: list[dict], gameweek: int, team: int) -> str | None:
    """What that club did in that gameweek, or None if it has not happened."""
    played = [
        f for f in fixtures
        if f.get("event") == gameweek and f.get("finished")
        and team in (f.get("team_h"), f.get("team_a"))
        and f.get("team_h_score") is not None and f.get("team_a_score") is not None
    ]
    if not played:
        return None
    # A club with two fixtures in a round is settled on the first, matching the
    # fixture the odds were built from.
    f = sorted(played, key=lambda f: f.get("kickoff_time") or "")[0]
    home = f["team_h"] == team
    scored = f["team_h_score"] if home else f["team_a_score"]
    conceded = f["team_a_score"] if home else f["team_h_score"]
    if scored > conceded:
        return WON
    return DREW if scored == conceded else LOST


def read_state(path: Path | None = None) -> LmsState:
    path = path or config.LMS_STATE
    if not path.exists():
        return LmsState()
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        # A corrupt record should not stop the engine running; an empty one just
        # means the planner assumes nothing has been used yet, and says so.
        return LmsState(note="the saved record could not be read")
    picks = [Pick(**p) for p in raw.get("picks", [])]
    return LmsState(picks=picks, note=raw.get("note", ""))


def write_state(state: LmsState, path: Path | None = None) -> Path:
    path = path or config.LMS_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.as_dict(), indent=1))
    return path


class UnknownTeam(ValueError):
    """Raised when a name matches no club, or more than one."""


def resolve_team(name: str, teams: list[dict]) -> int:
    """A club ID from whatever the user typed.

    Accepts the full name, the three-letter code, or any unambiguous prefix.
    Ambiguity is an error rather than a guess, because "Man" costing someone
    their entry is not a trade worth making for the convenience.
    """
    wanted = name.strip().lower()
    if not wanted:
        raise UnknownTeam("no club given")

    for team in teams:
        if wanted in (team["name"].lower(), team.get("short_name", "").lower()):
            return team["id"]

    matches = [t for t in teams
               if t["name"].lower().startswith(wanted)
               or t.get("short_name", "").lower().startswith(wanted)]
    if len(matches) == 1:
        return matches[0]["id"]
    if matches:
        options = ", ".join(sorted(t["name"] for t in matches))
        raise UnknownTeam(f"'{name}' matches more than one club: {options}")
    raise UnknownTeam(f"'{name}' matches no club")


def resolve_many(names: str | list[str], teams: list[dict]) -> list[int]:
    """A comma-separated list of clubs as IDs, ignoring blanks."""
    if isinstance(names, str):
        names = names.split(",")
    return [resolve_team(n, teams) for n in names if n and n.strip()]
