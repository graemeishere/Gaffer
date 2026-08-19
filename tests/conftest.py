import pytest


def make_player(**kw):
    base = {
        "id": 1, "web_name": "Test", "team": 1, "element_type": 4, "now_cost": 50,
        "minutes": 0, "total_points": 0, "starts": 0, "status": "a",
        "chance_of_playing_next_round": None, "news": "", "selected_by_percent": "1.0",
        "team_join_date": "2020-07-01", "expected_goals_per_90": "0.0",
        "expected_assists_per_90": "0.0", "expected_goal_involvements_per_90": "0.0",
        "expected_goals_conceded_per_90": "1.4", "defensive_contribution_per_90": "0.0",
        "saves_per_90": "0.0", "bps": 0, "yellow_cards": 0, "red_cards": 0,
    }
    return base | kw


@pytest.fixture
def player_factory():
    return make_player


@pytest.fixture
def bootstrap(player_factory):
    return {
        "teams": [
            {"id": 1, "name": "Alpha", "short_name": "ALP"},
            {"id": 2, "name": "Beta", "short_name": "BET"},
        ],
        "element_types": [
            {"id": 1, "singular_name_short": "GKP"},
            {"id": 2, "singular_name_short": "DEF"},
            {"id": 3, "singular_name_short": "MID"},
            {"id": 4, "singular_name_short": "FWD"},
        ],
        "elements": [
            player_factory(id=1, web_name="Striker", team=1, element_type=4, now_cost=100,
                           minutes=3000, total_points=240, starts=34,
                           expected_goals_per_90="0.70", expected_assists_per_90="0.20",
                           expected_goal_involvements_per_90="0.90", bps=800),
            player_factory(id=2, web_name="Cameo", team=1, element_type=4, now_cost=45,
                           minutes=300, total_points=36, starts=1,
                           expected_goals_per_90="0.60", expected_goal_involvements_per_90="0.65"),
            player_factory(id=3, web_name="Stopper", team=2, element_type=1, now_cost=50,
                           minutes=3420, total_points=140, starts=38,
                           saves_per_90="3.0", expected_goals_conceded_per_90="1.20"),
            player_factory(id=4, web_name="Back", team=2, element_type=2, now_cost=55,
                           minutes=3000, total_points=130, starts=34,
                           defensive_contribution_per_90="9.0",
                           expected_goals_conceded_per_90="1.20"),
        ],
    }


@pytest.fixture
def fixtures():
    return [
        {"id": 1, "event": 1, "team_h": 1, "team_a": 2, "team_h_difficulty": 2,
         "team_a_difficulty": 4, "finished": False, "team_h_score": None,
         "team_a_score": None, "kickoff_time": "2026-08-21T18:00:00Z"},
        {"id": 2, "event": 2, "team_h": 2, "team_a": 1, "team_h_difficulty": 3,
         "team_a_difficulty": 3, "finished": False, "team_h_score": None,
         "team_a_score": None, "kickoff_time": "2026-08-28T18:00:00Z"},
    ]
