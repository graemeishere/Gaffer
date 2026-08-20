"""Head-to-head leagues.

A classic league and a head-to-head league need opposite advice, so the failure
mode that matters is treating one as the other. These pin the differences:
margin pays nothing, shared players cannot affect the result, and risk belongs
to the underdog rather than the favourite.
"""
import random

import pytest

from gaffer.league.h2h import (
    DRAW, LOSS, WIN, Match, advise_match, compare_squads, fixture_for,
    is_head_to_head,
)
from gaffer.league.simulate import simulate_match


def match(gw, a, b, a_pts=0, b_pts=0):
    return Match(gameweek=gw, entry_1=a, entry_1_name=f"T{a}", entry_1_points=a_pts,
                 entry_2=b, entry_2_name=f"T{b}", entry_2_points=b_pts, finished=False)


def draws(**kw):
    base = {
        "goal_rate": 0.4, "goal_value": 5, "assist_rate": 0.25, "assist_value": 3,
        "clean_sheet_chance": 0.0, "clean_sheet_value": 0,
        "p_appear": 0.95, "p_60": 0.85, "steady": 0.8,
    }
    return base | kw


class TestDetection:
    def test_scoring_h_is_head_to_head(self):
        assert is_head_to_head({"league": {"scoring": "h"}})

    def test_scoring_c_is_not(self):
        assert not is_head_to_head({"league": {"scoring": "c"}})

    def test_missing_scoring_is_not(self):
        assert not is_head_to_head({"league": {}})


class TestFixtures:
    def test_finds_your_match_either_side_of_the_tie(self):
        matches = [match(1, 100, 200), match(1, 300, 400)]
        assert fixture_for(matches, 200, 1).opponent_of(200) == (100, "T100")
        assert fixture_for(matches, 300, 1).opponent_of(300) == (400, "T400")

    def test_returns_nothing_for_another_gameweek(self):
        assert fixture_for([match(1, 100, 200)], 100, 2) is None

    def test_returns_nothing_for_a_manager_not_playing(self):
        assert fixture_for([match(1, 100, 200)], 999, 1) is None

    def test_opponent_of_an_uninvolved_entry_is_none(self):
        assert match(1, 100, 200).opponent_of(999) is None


class TestSquadComparison:
    def test_counts_shared_and_unique(self):
        shared, mine, theirs = compare_squads([1, 2, 3, 4], [3, 4, 5])
        assert (shared, mine, theirs) == (2, 2, 1)

    def test_identical_squads_are_entirely_shared(self):
        """Two identical squads cannot produce anything but a draw."""
        shared, mine, theirs = compare_squads([1, 2, 3], [1, 2, 3])
        assert (shared, mine, theirs) == (3, 0, 0)


class TestStance:
    def test_the_favourite_protects(self):
        """Margin pays nothing, so a likely winner gains nothing from variance."""
        stance, _ = advise_match(p_win=0.75, p_loss=0.20, shared=8, squad_size=15)
        assert stance == "protect"

    def test_the_underdog_gambles(self):
        """A predictable gameweek loses; an upset needs spread."""
        stance, _ = advise_match(p_win=0.22, p_loss=0.70, shared=8, squad_size=15)
        assert stance == "gamble"

    def test_an_even_match_is_balanced(self):
        stance, _ = advise_match(p_win=0.48, p_loss=0.45, shared=8, squad_size=15)
        assert stance == "balanced"

    def test_heavy_overlap_is_called_out(self):
        _, reason = advise_match(p_win=0.5, p_loss=0.45, shared=13, squad_size=15)
        assert "13 of 15" in reason

    def test_light_overlap_is_not_mentioned(self):
        _, reason = advise_match(p_win=0.5, p_loss=0.45, shared=3, squad_size=15)
        assert "cannot affect" not in reason

    def test_scoring_constants_match_the_game(self):
        assert (WIN, DRAW, LOSS) == (3, 1, 0)


class TestMatchSimulation:
    def _draws(self, mine_rate, theirs_rate):
        d = {(i, 0): draws(goal_rate=mine_rate) for i in range(1, 16)}
        d.update({(i, 0): draws(goal_rate=theirs_rate) for i in range(16, 31)})
        return d

    def test_probabilities_sum_to_one(self):
        p_win, p_draw, p_loss, _, _ = simulate_match(
            list(range(1, 16)), 1, list(range(16, 31)), 16,
            self._draws(0.4, 0.4), trials=500)
        assert p_win + p_draw + p_loss == pytest.approx(1.0)

    def test_a_stronger_squad_wins_more_often(self):
        p_win, _, p_loss, _, _ = simulate_match(
            list(range(1, 16)), 1, list(range(16, 31)), 16,
            self._draws(1.0, 0.05), trials=800)
        assert p_win > p_loss

    def test_draws_are_possible(self):
        """Scores are whole numbers, so ties happen — reporting zero would be
        wrong in a competition where a draw is worth a third of a win."""
        _, p_draw, _, _, _ = simulate_match(
            list(range(1, 16)), 1, list(range(16, 31)), 16,
            self._draws(0.4, 0.4), trials=3000)
        assert p_draw > 0

    def test_means_are_reported(self):
        _, _, _, mine, theirs = simulate_match(
            list(range(1, 16)), 1, list(range(16, 31)), 16,
            self._draws(0.8, 0.2), trials=500)
        assert mine > theirs > 0

    def test_reproducible_for_a_seed(self):
        args = (list(range(1, 16)), 1, list(range(16, 31)), 16, self._draws(0.4, 0.4))
        a = simulate_match(*args, trials=400, seed=3)
        b = simulate_match(*args, trials=400, seed=3)
        assert a == b
