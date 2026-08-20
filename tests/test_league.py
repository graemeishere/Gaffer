"""Mini-league simulation and strategy.

The sampler is the load-bearing part: if drawing a gameweek does not reproduce
the projection's mean, every comparison built on it is biased. The rest guards
the head-to-head accounting and the stance logic, which people reliably get
backwards — risk when leading, safety when chasing.
"""
import random

import pytest

from gaffer.league import (
    advise, effective_ownership, sample_gameweek, simulate_league,
)


def draws(**kw):
    base = {
        "goal_rate": 0.4, "goal_value": 5, "assist_rate": 0.25, "assist_value": 3,
        "clean_sheet_chance": 0.0, "clean_sheet_value": 0,
        "p_appear": 0.95, "p_60": 0.85, "steady": 0.8,
    }
    return base | kw


class TestSampler:
    def test_reproduces_the_projected_mean(self):
        """Bias here poisons every comparison downstream."""
        d = draws()
        expected = (d["steady"] + d["p_60"] * 2 + (d["p_appear"] - d["p_60"]) * 1
                    + d["goal_rate"] * d["goal_value"] + d["assist_rate"] * d["assist_value"])
        rng = random.Random(3)
        sampled = sum(sample_gameweek(d, rng) for _ in range(60000)) / 60000
        assert sampled == pytest.approx(expected, abs=0.08)

    def test_a_player_who_never_appears_scores_nothing(self):
        rng = random.Random(3)
        assert all(sample_gameweek(draws(p_appear=0.0), rng) == 0 for _ in range(200))

    def test_never_returns_a_negative_score(self):
        rng = random.Random(3)
        assert all(sample_gameweek(draws(), rng) >= 0 for _ in range(2000))

    def test_a_volatile_player_spreads_wider_than_a_steady_one(self):
        """Same average, different shape — the whole basis of league strategy."""
        rng = random.Random(11)
        volatile = draws(goal_rate=1.0, goal_value=6, steady=0.1, assist_rate=0.0)
        steady = draws(goal_rate=0.0, assist_rate=0.0, steady=6.1)

        def spread(d):
            xs = [sample_gameweek(d, rng) for _ in range(20000)]
            mean = sum(xs) / len(xs)
            return (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5

        assert spread(volatile) > spread(steady) * 2


class TestSimulation:
    def test_a_better_squad_wins_more_often(self):
        strong = {(i, 0): draws(goal_rate=0.9) for i in range(1, 16)}
        weak = {(i, 0): draws(goal_rate=0.05, steady=0.2) for i in range(16, 31)}
        result = simulate_league(
            list(range(1, 16)), 1, {99: (list(range(16, 31)), 16)},
            {**strong, **weak}, gameweeks=1, trials=400)
        assert result.win_probability > 0.9

    def test_head_to_head_is_counted_within_a_trial(self):
        """Comparing sorted distributions afterwards would pair my tenth
        percentile against theirs, which is a different quantity entirely."""
        identical = {(i, 0): draws() for i in range(1, 31)}
        result = simulate_league(
            list(range(1, 16)), 1, {99: (list(range(16, 31)), 16)},
            identical, gameweeks=1, trials=800)
        # Same squad shape both sides, but I captain — so I should win most, not all.
        assert 0.5 < result.beat_each[99] < 1.0

    def test_percentiles_bracket_the_mean(self):
        d = {(i, 0): draws() for i in range(1, 16)}
        result = simulate_league(list(range(1, 16)), 1, {}, d, gameweeks=1, trials=400)
        assert result.my_p10 <= result.my_mean <= result.my_p90

    def test_is_reproducible_for_a_given_seed(self):
        d = {(i, 0): draws() for i in range(1, 31)}
        args = (list(range(1, 16)), 1, {99: (list(range(16, 31)), 16)}, d)
        a = simulate_league(*args, gameweeks=1, trials=300, seed=5)
        b = simulate_league(*args, gameweeks=1, trials=300, seed=5)
        assert a.win_probability == b.win_probability


class TestOwnership:
    def test_classifies_by_share_within_the_league(self):
        mine = [1, 2, 3]
        rivals = {10: [1, 2], 11: [1, 2], 12: [1, 9], 13: [1, 9]}
        rows = {r.player_id: r for r in effective_ownership(mine, rivals, {})}
        assert rows[1].kind == "template"       # everyone has him
        assert rows[3].kind == "differential"   # only I do
        assert rows[9].kind == "exposure"       # half of them do, I do not

    def test_share_is_measured_against_rival_count(self):
        rows = {r.player_id: r for r in effective_ownership([1], {10: [1], 11: []}, {})}
        assert rows[1].share == 0.5


class TestStance:
    def _rows(self):
        return effective_ownership([1, 2], {10: [1], 11: [1], 12: [1]}, {})

    def test_ahead_means_protect(self):
        assert advise(self._rows(), win_probability=0.7, gameweeks_left=20).stance == "protect"

    def test_behind_and_late_means_chase(self):
        """Playing the percentages from behind loses slowly, which is still losing."""
        assert advise(self._rows(), win_probability=0.1, gameweeks_left=4).stance == "chase"

    def test_level_means_balanced(self):
        assert advise(self._rows(), win_probability=0.45, gameweeks_left=20).stance == "balanced"

    def test_advice_always_carries_a_reason(self):
        for probability in (0.05, 0.3, 0.45, 0.8):
            result = advise(self._rows(), win_probability=probability, gameweeks_left=10)
            assert result.reason and result.suggested
