"""The scoring table is transcribed by hand, so it gets guarded by hand.

The goalkeeper goal value is the one worth pinning: it is 10, not the 6 it was
historically, and taking it from memory would mis-price every keeper in the game.
"""
from gaffer.model.scoring import SCORING


def test_goalkeeper_goals_are_worth_ten():
    assert SCORING.goal_value("GKP") == 10


def test_goal_value_falls_by_position():
    assert (SCORING.goal_value("GKP") > SCORING.goal_value("DEF")
            > SCORING.goal_value("MID") > SCORING.goal_value("FWD"))


def test_clean_sheets_only_pay_defensive_positions():
    assert SCORING.clean_sheet_value("GKP") == SCORING.clean_sheet_value("DEF") == 4
    assert SCORING.clean_sheet_value("MID") == 1
    assert SCORING.clean_sheet_value("FWD") == 0


def test_defensive_contribution_thresholds():
    assert SCORING.defcon_threshold["DEF"] == 10
    assert SCORING.defcon_threshold["MID"] == SCORING.defcon_threshold["FWD"] == 12
    assert SCORING.defcon_points == 2


def test_conceded_deduction_applies_to_keepers_and_defenders_only():
    assert set(SCORING.conceded_positions) == {"GKP", "DEF"}
