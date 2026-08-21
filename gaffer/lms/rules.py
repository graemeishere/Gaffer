"""The pool's rules, because they are not the same in any two pools.

Last Man Standing has no governing body and no rulebook. Three things vary
between pools, and each one changes the arithmetic enough that advice built for
the wrong variant is worse than no advice:

- **Does a draw survive?** Usually not, which is what makes the format hard:
  a fixture the model calls 65/22/13 is a 65% pick, not an 87% one. A minority
  of pools count a draw as a pass, and there the strongest picks become away
  sides at solid teams rather than home favourites.
- **How many lives?** One is standard. Two changes the risk appetite entirely —
  with a life in hand, spending a strong team early costs less than it looks,
  because you can afford to be wrong once.
- **Do teams come back?** Most pools lock a club out for the whole season, which
  is the constraint the planner exists to handle. Some reset the used list when
  the field thins out, which shortens the horizon worth planning over.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from gaffer import config


@dataclass(frozen=True)
class Rules:
    draw_survives: bool = False
    lives: int = 1
    # How many rounds ahead to plan. Deeper is not automatically better: the
    # fixture list is known all season, so the model can see GW30, but a route
    # planned that far out is reshuffled by every result between here and there.
    # Far enough to stop the planner spending its best teams cheaply, no further.
    horizon: int = 8

    @classmethod
    def from_env(cls) -> "Rules":
        return cls(
            draw_survives=config.LMS_DRAW_SURVIVES,
            lives=config.LMS_LIVES,
            horizon=config.LMS_HORIZON,
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def lives_phrase(self) -> str:
        return "one life" if self.lives == 1 else f"{self.lives} lives"

    @property
    def draw_phrase(self) -> str:
        return "a draw survives" if self.draw_survives else "a draw is out"

    @property
    def summary(self) -> str:
        return (f"{self.draw_phrase}, {self.lives_phrase}, "
                f"planning {self.horizon} rounds ahead")
