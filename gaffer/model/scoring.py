"""The FPL scoring table.

Not available from the API — `game_config` carries no point values — so it is
transcribed here from the official rules and kept in one place so a mid-season
rule change is a one-line edit rather than a hunt through the codebase.

Verified against the published 2026/27 rules. Note goalkeeper goals are worth
10, not the 6 they were historically; getting this from memory would have
quietly mis-priced every goalkeeper in the game.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scoring:
    playing_under_60: int = 1
    playing_60_plus: int = 2

    goal: dict[str, int] = field(default_factory=lambda: {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4})
    assist: int = 3
    clean_sheet: dict[str, int] = field(default_factory=lambda: {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0})

    saves_per_point: int = 3
    penalty_save: int = 5
    penalty_miss: int = -2

    # One point deducted per two goals conceded, goalkeepers and defenders only.
    conceded_per_deduction: int = 2
    conceded_positions: tuple[str, ...] = ("GKP", "DEF")

    yellow_card: int = -1
    red_card: int = -3
    own_goal: int = -2

    # Defensive contribution: defenders need 10 clearances, blocks, interceptions
    # and tackles; midfielders and forwards need 12, recoveries included. Worth 2
    # points, capped — hitting double the threshold does not pay twice.
    defcon_threshold: dict[str, int] = field(
        default_factory=lambda: {"GKP": 999, "DEF": 10, "MID": 12, "FWD": 12}
    )
    defcon_points: int = 2

    max_bonus: int = 3

    def goal_value(self, position: str) -> int:
        return self.goal.get(position, 4)

    def clean_sheet_value(self, position: str) -> int:
        return self.clean_sheet.get(position, 0)


SCORING = Scoring()
