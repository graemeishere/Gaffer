"""Tests for the Phase 0 ranking maths.

These guard the two mistakes that made the first run useless: trusting a per-90
rate built on a handful of minutes, and projecting every player as a full-time
starter.
"""
import pytest

from gaffer import config
from gaffer.rank.value import (
    _base_rate,
    _baseline_rate,
    _fixture_multiplier,
    _playing_time,
    _availability,
    rank_players,
    team_fixture_runs,
)


def player(**kw):
    base = {"minutes": 0, "total_points": 0, "starts": 0, "status": "a",
            "chance_of_playing_next_round": None, "news": ""}
    return base | kw


class TestShrinkage:
    def test_small_sample_is_pulled_toward_baseline(self):
        """A cameo scorer must not out-rate a season-long starter."""
        cameo, _ = _base_rate(player(minutes=317, total_points=37), baseline=4.0)
        regular, _ = _base_rate(player(minutes=2953, total_points=239), baseline=4.0)
        assert cameo < regular, "small sample beat a full season — shrinkage is not working"

    def test_shrinkage_scales_with_evidence(self):
        """The property that matters: a full season is pulled proportionally far
        less than a handful of appearances. The absolute size of the pull is a
        tuning decision (SHRINKAGE_APPEARANCES), not something to pin down here."""
        def pull(minutes, points):
            raw = points / minutes * 90
            shrunk, _ = _base_rate(player(minutes=minutes, total_points=points), baseline=4.0)
            return abs(shrunk - raw) / raw

        full_season = pull(2953, 239)
        handful = pull(317, 37)
        assert full_season < handful / 2, "a full season should be trusted far more than four cameos"
        assert full_season < 0.25, "a 33-game sample should not be pulled more than a quarter of the way"

    def test_no_minutes_returns_the_baseline(self):
        rate, confidence = _base_rate(player(), baseline=4.0)
        assert rate == pytest.approx(4.0)
        assert confidence == "low"

    @pytest.mark.parametrize("minutes,expected", [(2700, "high"), (900, "medium"), (200, "low")])
    def test_confidence_tracks_sample_size(self, minutes, expected):
        _, confidence = _base_rate(player(minutes=minutes, total_points=100), baseline=4.0)
        assert confidence == expected


class TestPlayingTime:
    def test_bounded_to_one(self):
        assert _playing_time(player(minutes=3420, starts=38)) == 1.0

    def test_bit_part_player_is_heavily_discounted(self):
        assert _playing_time(player(minutes=317, starts=1)) < 0.15

    def test_regular_substitute_is_not_zeroed(self):
        """Someone who never starts but always plays 30 minutes still has value."""
        assert _playing_time(player(minutes=1140, starts=0)) > 0.3

    def test_never_played_is_zero(self):
        assert _playing_time(player()) == 0.0


class TestFixtures:
    def test_average_difficulty_is_neutral(self):
        multiplier, mean = _fixture_multiplier([{"difficulty": 3}] * 6)
        assert multiplier == pytest.approx(1.0)
        assert mean == pytest.approx(3.0)

    def test_easy_run_helps_and_hard_run_hurts(self):
        easy, _ = _fixture_multiplier([{"difficulty": 2}] * 6)
        hard, _ = _fixture_multiplier([{"difficulty": 4}] * 6)
        assert easy > 1.0 > hard

    def test_multiplier_is_clamped(self):
        extreme, _ = _fixture_multiplier([{"difficulty": 1}] * 6)
        assert extreme <= 1.30

    def test_empty_run_is_neutral(self):
        assert _fixture_multiplier([]) == (1.0, 3.0)


class TestAvailability:
    def test_fit_player_is_full(self):
        assert _availability(player())[0] == 1.0

    def test_injured_player_is_zero(self):
        assert _availability(player(status="i"))[0] == 0.0

    def test_explicit_chance_wins(self):
        assert _availability(player(status="d", chance_of_playing_next_round=25))[0] == 0.25


class TestFixtureRuns:
    def test_horizon_is_respected_and_finished_games_skipped(self):
        fixtures = [
            {"id": 1, "event": 1, "team_h": 1, "team_a": 2, "team_h_difficulty": 2,
             "team_a_difficulty": 4, "finished": True, "kickoff_time": "2026-08-21T18:00:00Z"},
            {"id": 2, "event": 2, "team_h": 2, "team_a": 1, "team_h_difficulty": 3,
             "team_a_difficulty": 3, "finished": False, "kickoff_time": "2026-08-28T18:00:00Z"},
            {"id": 3, "event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 2,
             "team_a_difficulty": 4, "finished": False, "kickoff_time": "2026-09-04T18:00:00Z"},
        ]
        runs = team_fixture_runs(fixtures, horizon=1)
        assert len(runs[1]) == 1
        assert runs[1][0]["gameweek"] == 2, "a finished fixture leaked into the run"


class TestEndToEnd:
    def test_rank_players_on_a_minimal_payload(self):
        bootstrap = {
            "teams": [{"id": 1, "name": "Alpha", "short_name": "ALP"},
                      {"id": 2, "name": "Beta", "short_name": "BET"}],
            "element_types": [{"id": 1, "singular_name_short": "GKP"},
                              {"id": 4, "singular_name_short": "FWD"}],
            "elements": [
                player(id=1, web_name="Star", team=1, element_type=4, now_cost=100,
                       minutes=3000, total_points=240, starts=34, selected_by_percent="50.0",
                       team_join_date="2020-07-01"),
                player(id=2, web_name="Cameo", team=1, element_type=4, now_cost=45,
                       minutes=300, total_points=36, starts=1, selected_by_percent="1.0",
                       team_join_date="2020-07-01"),
                player(id=3, web_name="Keeper", team=2, element_type=1, now_cost=50,
                       minutes=3420, total_points=140, starts=38, selected_by_percent="10.0",
                       team_join_date="2026-07-01"),
            ],
        }
        fixtures = [{"id": 1, "event": 1, "team_h": 1, "team_a": 2, "team_h_difficulty": 2,
                     "team_a_difficulty": 4, "finished": False, "kickoff_time": "2026-08-21T18:00:00Z"}]
        scores = rank_players(bootstrap, fixtures, horizon=1)

        by_name = {s.name: s for s in scores}
        assert by_name["Star"].projected > by_name["Cameo"].projected
        assert by_name["Keeper"].moved_club is True
        assert all(s.per_million > 0 for s in scores)
        assert scores == sorted(scores, key=lambda s: -s.projected)
