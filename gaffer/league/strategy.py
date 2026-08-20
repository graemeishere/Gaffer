"""What to do about the fourteen people you are actually playing.

National ownership is close to irrelevant here. What decides your table is only
the *difference* between your squad and your rivals' — points you score on a
player they all own move everybody equally and change nothing. So ownership is
measured inside the league, and advice follows from where you stand in it.

The dynamic part matters most, and it is where people reliably get it backwards:
behind, you need spread, so you want players the field does not have; ahead, you
want the opposite — own what they own and let the lead run down the clock.
Managers tend to take risks when leading and play safe when chasing, which is
exactly wrong, and it is the easiest edge in this whole project to actually take.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class OwnershipRow:
    player_id: int
    name: str
    owned_by: int          # how many rivals hold him
    rivals: int
    mine: bool

    @property
    def share(self) -> float:
        return self.owned_by / self.rivals if self.rivals else 0.0

    @property
    def kind(self) -> str:
        """How this player behaves in *this* league, regardless of how good he is."""
        if self.mine and self.share >= 0.7:
            return "template"       # moves you with the field, not against it
        if self.mine and self.share <= 0.3:
            return "differential"   # your score diverges from theirs here
        if not self.mine and self.share >= 0.5:
            return "exposure"       # they gain and you do not when he returns
        return "neutral"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["share"] = round(self.share, 3)
        data["kind"] = self.kind
        return data


@dataclass
class StrategyAdvice:
    stance: str            # protect | balanced | chase
    reason: str
    win_probability: float
    template_count: int
    differential_count: int
    biggest_exposure: list[str] = field(default_factory=list)
    suggested: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def effective_ownership(
    my_squad: list[int],
    rival_squads: dict[int, list[int]],
    names: dict[int, str],
) -> list[OwnershipRow]:
    """How much of the league holds each player, mine and theirs alike."""
    rivals = len(rival_squads)
    counts: dict[int, int] = {}
    for squad in rival_squads.values():
        for pid in set(squad):
            counts[pid] = counts.get(pid, 0) + 1

    considered = set(my_squad) | set(counts)
    rows = [
        OwnershipRow(
            player_id=pid,
            name=names.get(pid, str(pid)),
            owned_by=counts.get(pid, 0),
            rivals=rivals,
            mine=pid in set(my_squad),
        )
        for pid in considered
    ]
    rows.sort(key=lambda r: (-r.owned_by, r.name))
    return rows


def advise(
    rows: list[OwnershipRow],
    *,
    win_probability: float,
    gameweeks_left: int,
    my_rank: int = 0,
    rivals: int = 0,
) -> StrategyAdvice:
    """Whether to take variance on or squeeze it out."""
    template = [r for r in rows if r.kind == "template"]
    differentials = [r for r in rows if r.kind == "differential"]
    exposure = sorted((r for r in rows if r.kind == "exposure"),
                      key=lambda r: -r.owned_by)[:3]

    late = gameweeks_left <= 8
    if win_probability >= 0.55:
        stance = "protect"
        reason = ("You are ahead on the simulation. Extra variance can only cost you "
                  "from here — match the field rather than trying to pull away.")
        suggested = ("Move toward what your rivals own, especially the captain. "
                     "A shared blank costs you nothing; a solo one costs you the league.")
    elif win_probability <= 0.25 and late:
        stance = "chase"
        reason = ("Behind with little time left. Playing the percentages loses slowly, "
                  "which is still losing — you need outcomes the field does not share.")
        suggested = ("Take players your rivals do not own and captain differently. "
                     "The aim is a wide range of outcomes, not a good average.")
    elif win_probability <= 0.35:
        stance = "chase"
        reason = ("Behind, but with enough gameweeks left that patience is still worth "
                  "something. Lean toward differentials without abandoning the base.")
        suggested = ("Hold the template core and take your risks in one or two places, "
                     "starting with the armband.")
    else:
        stance = "balanced"
        reason = ("Close enough that neither chasing nor protecting is clearly right. "
                  "Maximise points and revisit when the picture separates.")
        suggested = "Pick the highest expected points and let the table develop."

    return StrategyAdvice(
        stance=stance,
        reason=reason,
        win_probability=win_probability,
        template_count=len(template),
        differential_count=len(differentials),
        biggest_exposure=[f"{r.name} ({r.owned_by}/{r.rivals})" for r in exposure],
        suggested=suggested,
    )
