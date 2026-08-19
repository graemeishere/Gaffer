"""The backtest, and the guards that keep it honest.

A backtest that quietly tests the wrong thing is worse than none at all, because
it produces a number people believe. These lock down the three ways this one
could mislead: testing a model whose inputs did not exist yet, filtering the
test season with hindsight, and picking the starting eleven after the fact.
"""
import pytest

from gaffer.backtest import dataset as ds
from gaffer.backtest.dataset import (
    FIRST_SEASON_WITH_XG,
    SeasonRow,
    input_coverage,
    previous_season,
    season_pairs,
)
from gaffer.backtest.harness import spearman
from gaffer.backtest.strategies import STRATEGIES, model_projection


def season_row(code, season, **kw):
    base = dict(
        code=code, name=f"P{code}", position="MID", season=season, points=100,
        minutes=2700, starts=30, cost_start=6.0, cost_end=6.5, goals=8, assists=6,
        clean_sheets=8, goals_conceded=30, saves=0, bonus=12, bps=500,
        yellow_cards=3, red_cards=0, expected_goals=7.5, expected_assists=5.5,
        expected_goals_conceded=35.0, defensive_contribution=180.0,
    )
    return SeasonRow(**(base | kw))


class TestSeasonArithmetic:
    @pytest.mark.parametrize("season,expected", [
        ("2025/26", "2024/25"), ("2023/24", "2022/23"), ("2000/01", "1999/00"),
    ])
    def test_previous_season(self, season, expected):
        assert previous_season(season) == expected


class TestPairing:
    def test_pairs_need_both_seasons(self):
        rows = {
            (1, "2024/25"): season_row(1, "2024/25"),
            (1, "2025/26"): season_row(1, "2025/26"),
            (2, "2025/26"): season_row(2, "2025/26"),  # no prior season
        }
        pairs = season_pairs(rows, "2025/26")
        assert [p[0].code for p in pairs] == [1]

    def test_minutes_filter_applies_to_the_prior_season_only(self):
        """Filtering the test season on minutes would drop players who then got
        injured — hindsight, and exactly what makes a backtest lie."""
        rows = {
            (1, "2024/25"): season_row(1, "2024/25", minutes=2700),
            (1, "2025/26"): season_row(1, "2025/26", minutes=90, points=3),
        }
        pairs = season_pairs(rows, "2025/26", min_minutes=900)
        assert len(pairs) == 1, "a player who got injured was silently excluded"
        assert pairs[0][1].points == 3

    def test_thin_prior_seasons_are_excluded(self):
        rows = {
            (1, "2024/25"): season_row(1, "2024/25", minutes=200),
            (1, "2025/26"): season_row(1, "2025/26"),
        }
        assert season_pairs(rows, "2025/26", min_minutes=900) == []


class TestInputCoverage:
    def test_seasons_without_underlying_data_are_not_testable(self):
        """Before 2022/23 the API carries no expected goals. Testing there scores
        a model with its inputs removed, which loses to anything."""
        rows = {}
        for code in range(60):
            rows[(code, "2021/22")] = season_row(code, "2021/22", expected_goals=0.0,
                                                 expected_assists=0.0)
            rows[(code, "2022/23")] = season_row(code, "2022/23")
        assert "2022/23" not in ds.testable_seasons(rows)

    def test_seasons_with_underlying_data_are_testable(self):
        rows = {}
        for code in range(60):
            rows[(code, "2024/25")] = season_row(code, "2024/25")
            rows[(code, "2025/26")] = season_row(code, "2025/26")
        assert "2025/26" in ds.testable_seasons(rows)

    def test_coverage_reports_the_share_with_each_input(self):
        rows = {
            (1, "2024/25"): season_row(1, "2024/25", expected_goals=5.0),
            (2, "2024/25"): season_row(2, "2024/25", expected_goals=0.0),
        }
        assert input_coverage(rows, "2024/25")["expected_goals"] == 0.5

    def test_the_xg_era_boundary_is_recorded(self):
        assert FIRST_SEASON_WITH_XG == "2022/23"


class TestSpearman:
    def test_perfect_agreement(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_disagreement(self):
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_ranks_not_magnitudes(self):
        """Being wrong about everyone by the same factor costs nothing when the
        job is choosing between players."""
        assert spearman([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)

    def test_ties_do_not_break_it(self):
        assert -1.0 <= spearman([1, 1, 1, 2], [5, 5, 6, 7]) <= 1.0

    def test_too_few_points_returns_zero(self):
        assert spearman([1.0], [2.0]) == 0.0


class TestStrategies:
    def test_every_strategy_returns_a_number(self):
        row = season_row(1, "2024/25")
        for name, strategy in STRATEGIES.items():
            value = strategy(row)
            assert isinstance(value, float), f"{name} did not return a float"
            assert value >= 0

    def test_the_model_is_among_the_strategies(self):
        assert "model" in STRATEGIES

    def test_naive_benchmark_is_present(self):
        """The model has to be measured against what people actually do."""
        assert "last season's points" in STRATEGIES

    def test_model_rates_a_stronger_season_higher(self):
        weak = season_row(1, "2024/25", expected_goals=1.0, expected_assists=1.0,
                          minutes=1800, starts=18, bps=150)
        strong = season_row(2, "2024/25", expected_goals=18.0, expected_assists=8.0,
                            minutes=3200, starts=35, bps=800)
        assert model_projection(strong) > model_projection(weak)

    def test_model_barely_projects_for_a_player_who_never_played(self):
        """Not zero — a player with no minutes behind him is an unknown, not a
        certainty, and the minutes model gives him the squad-wide base rate. But
        he must sit far below an established starter."""
        unknown = model_projection(season_row(1, "2024/25", minutes=0, starts=0))
        regular = model_projection(season_row(2, "2024/25", minutes=3000, starts=34))
        assert 0 <= unknown < regular * 0.25
