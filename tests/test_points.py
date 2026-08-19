"""Expected points, component by component."""
import math

import pytest

from gaffer.model.points import (
    _conceded_deduction,
    _poisson_at_least,
    project,
    project_fixture,
)
from gaffer.model.minutes import estimate as estimate_minutes
from gaffer.model.strength import TeamStrength
from gaffer.model.fixtures import team_fixture_runs


class TestProbabilityHelpers:
    def test_at_least_zero_is_certain(self):
        assert _poisson_at_least(0, 5.0) == 1.0

    def test_threshold_is_harder_to_reach_than_the_mean(self):
        assert _poisson_at_least(12, 6.0) < _poisson_at_least(6, 6.0)

    def test_no_rate_means_no_chance(self):
        assert _poisson_at_least(10, 0.0) == 0.0

    def test_conceded_deduction_is_negative_and_grows(self):
        light = _conceded_deduction(0.8)
        heavy = _conceded_deduction(2.5)
        assert heavy < light < 0


class TestProjection:
    def _setup(self, bootstrap, fixtures):
        strength = TeamStrength.fit(fixtures, bootstrap)
        runs = team_fixture_runs(fixtures, horizon=2)
        return strength, runs

    def test_every_player_gets_one_projection_per_fixture(self, bootstrap, fixtures):
        strength, runs = self._setup(bootstrap, fixtures)
        projections = project(bootstrap, runs, strength)
        assert all(len(v) == 2 for v in projections.values())

    def test_striker_out_scores_cameo(self, bootstrap, fixtures):
        strength, runs = self._setup(bootstrap, fixtures)
        projections = project(bootstrap, runs, strength)
        striker = sum(p.total for p in projections[1])
        cameo = sum(p.total for p in projections[2])
        assert striker > cameo

    def test_goalkeeper_earns_saves_and_clean_sheets(self, bootstrap, fixtures):
        strength, runs = self._setup(bootstrap, fixtures)
        projections = project(bootstrap, runs, strength)
        components = projections[3][0].components
        assert components["saves"] > 0
        assert components["clean_sheet"] > 0
        assert components["conceded"] < 0

    def test_forward_gets_no_clean_sheet_points(self, bootstrap, fixtures):
        strength, runs = self._setup(bootstrap, fixtures)
        projections = project(bootstrap, runs, strength)
        assert "clean_sheet" not in projections[1][0].components

    def test_defcon_rewards_crossing_the_threshold(self, bootstrap, fixtures, player_factory):
        """A defender needs 10 actions. Someone averaging 9 should score
        occasionally; someone averaging 2 should barely ever."""
        strength, runs = self._setup(bootstrap, fixtures)
        minutes = estimate_minutes(player_factory(minutes=3000, starts=34))
        high = project_fixture(
            player_factory(id=9, team=1, element_type=2, defensive_contribution_per_90="9.0"),
            "DEF", runs[1][0], strength, minutes, {1: "ALP", 2: "BET"})
        low = project_fixture(
            player_factory(id=10, team=1, element_type=2, defensive_contribution_per_90="2.0"),
            "DEF", runs[1][0], strength, minutes, {1: "ALP", 2: "BET"})
        assert high.components["defcon"] > low.components["defcon"]
        assert low.components["defcon"] < 0.1

    def test_injured_player_projects_near_zero(self, bootstrap, fixtures, player_factory):
        strength, runs = self._setup(bootstrap, fixtures)
        minutes = estimate_minutes(
            player_factory(minutes=3000, starts=34, status="i"))
        result = project_fixture(
            player_factory(id=11, team=1, element_type=4, expected_goals_per_90="0.8"),
            "FWD", runs[1][0], strength, minutes, {1: "ALP", 2: "BET"})
        assert result.total == pytest.approx(0.0, abs=1e-6)

    def test_easier_fixture_projects_more_points(self, bootstrap, fixtures, player_factory):
        """The same striker should be worth more against a weak defence."""
        strength = TeamStrength.fit(fixtures, bootstrap)
        strength.attack = {1: 1.0, 2: 1.0}
        strength.defence = {1: 0.5, 2: 1.8}
        runs = team_fixture_runs(fixtures, horizon=2)
        minutes = estimate_minutes(player_factory(minutes=3000, starts=34))
        striker = player_factory(id=12, team=1, element_type=4, expected_goals_per_90="0.8")

        versus_leaky = project_fixture(striker, "FWD", {"gameweek": 1, "opponent": 2,
                                       "home": True, "difficulty": 2},
                                       strength, minutes, {1: "ALP", 2: "BET"})
        versus_solid = project_fixture(striker, "FWD", {"gameweek": 1, "opponent": 1,
                                       "home": True, "difficulty": 5},
                                       strength, minutes, {1: "ALP", 2: "BET"})
        assert versus_leaky.components["goals"] > versus_solid.components["goals"]
