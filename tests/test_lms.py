"""Last Man Standing.

The format has one rule that makes it interesting — a club can be used once —
and one that makes it hard: a draw is a defeat. Both are easy to model and easy
to lose, so these pin the behaviour that would otherwise rot quietly:

- draws are counted, and counted the way the pool counts them;
- a club is never used twice, whatever the odds say;
- the planner beats "take the best team every week", which is the only reason
  for it to exist;
- and the record knows whether you are still in.
"""
import json
from dataclasses import dataclass

import pytest

from gaffer.lms.advise import advise, crowd_shares
from gaffer.lms.odds import MatchOdds, fixture_odds, outcome_probabilities
from gaffer.lms.plan import candidates, greedy_route, plan_route
from gaffer.lms.rules import Rules
from gaffer.lms.state import (DREW, LOST, LmsState, Pick, UnknownTeam, WON,
                              read_state, resolve_team, write_state)


class FakeStrength:
    """Expected goals straight from a table, so a test can state the fixture it
    means rather than construct a squad that happens to imply it."""

    def __init__(self, rates):
        self.rates = rates
        self.source, self.matches_fitted = "test", 0

    def expected_goals(self, home, away):
        return self.rates[(home, away)]


def fixture(fid, event, home, away, kickoff="2026-08-21T14:00:00Z", **kw):
    base = {"id": fid, "event": event, "team_h": home, "team_a": away,
            "team_h_difficulty": 3, "team_a_difficulty": 3, "finished": False,
            "team_h_score": None, "team_a_score": None, "kickoff_time": kickoff}
    return base | kw


def odds(gameweek, team, opponent, win, draw, home=True):
    return MatchOdds(gameweek=gameweek, team=team, opponent=opponent, home=home,
                     win=win, draw=draw, loss=1 - win - draw,
                     expected_for=1.5, expected_against=1.0)


NAMES = {1: "Alpha", 2: "Beta", 3: "Gamma", 4: "Delta", 5: "Epsilon", 6: "Zeta"}
OUT = Rules(draw_survives=False, lives=1, horizon=3)
SURVIVES = Rules(draw_survives=True, lives=1, horizon=3)


class TestOutcomes:
    def test_probabilities_sum_to_one(self):
        assert sum(outcome_probabilities(1.7, 1.1)) == pytest.approx(1.0, abs=1e-6)

    def test_the_stronger_side_is_favourite(self):
        home, _, away = outcome_probabilities(2.4, 0.7)
        assert home > away

    def test_a_mismatch_is_not_a_certainty(self):
        # The most common way to go out is treating 2.4 v 0.7 as a formality.
        home, draw, away = outcome_probabilities(2.4, 0.7)
        assert home < 0.85
        assert draw + away > 0.15

    def test_low_score_correction_lifts_the_draw(self):
        """Independent Poisson under-counts draws, and in this format the draw
        is half the risk. Without the correction every pick looks safer."""
        _, corrected, _ = outcome_probabilities(1.3, 1.1, rho=-0.13)
        _, independent, _ = outcome_probabilities(1.3, 1.1, rho=0.0)
        assert corrected > independent

    def test_survival_counts_the_draw_only_when_the_pool_does(self):
        row = odds(1, 1, 2, win=0.55, draw=0.25)
        assert row.survival(draw_survives=False) == pytest.approx(0.55)
        assert row.survival(draw_survives=True) == pytest.approx(0.80)


class TestFixtureOdds:
    def test_both_sides_of_every_fixture_appear(self):
        strength = FakeStrength({(1, 2): (1.8, 0.9)})
        rounds = fixture_odds([fixture(1, 5, 1, 2)], strength)
        assert {o.team for o in rounds[5]} == {1, 2}
        home = next(o for o in rounds[5] if o.team == 1)
        away = next(o for o in rounds[5] if o.team == 2)
        assert home.win == pytest.approx(away.loss)
        assert home.home and not away.home

    def test_finished_and_unscheduled_fixtures_are_ignored(self):
        strength = FakeStrength({(1, 2): (1.5, 1.0)})
        rounds = fixture_odds([
            fixture(1, 4, 1, 2, finished=True, team_h_score=2, team_a_score=0),
            fixture(2, None, 1, 2),
        ], strength)
        assert rounds == {}

    def test_a_double_gameweek_counts_once(self):
        """Two fixtures in a round is not two chances to survive — the pool
        settles on one match, so treating it as two invents a safety net."""
        strength = FakeStrength({(1, 2): (1.6, 1.0), (3, 1): (1.2, 1.3)})
        rounds = fixture_odds([
            fixture(1, 6, 1, 2, kickoff="2026-09-12T14:00:00Z"),
            fixture(2, 6, 3, 1, kickoff="2026-09-15T19:00:00Z"),
        ], strength)
        mine = [o for o in rounds[6] if o.team == 1]
        assert len(mine) == 1
        assert mine[0].home and mine[0].doubled


class TestCandidates:
    def test_used_clubs_are_off_the_board(self):
        rounds = {1: [odds(1, 1, 2, 0.7, 0.2), odds(1, 3, 4, 0.5, 0.25)]}
        available = candidates(rounds, used=[1], rules=OUT)
        assert [o.team for o in available[1]] == [3]

    def test_a_round_with_nothing_left_is_dropped(self):
        rounds = {1: [odds(1, 1, 2, 0.7, 0.2)], 2: [odds(2, 3, 4, 0.6, 0.2)]}
        available = candidates(rounds, used=[1], rules=OUT)
        assert list(available) == [2]

    def test_the_horizon_bounds_the_plan(self):
        rounds = {gw: [odds(gw, gw, 9, 0.6, 0.2)] for gw in range(1, 8)}
        available = candidates(rounds, used=[], rules=Rules(horizon=3))
        assert list(available) == [1, 2, 3]


class TestPlanning:
    def test_a_club_is_never_used_twice(self):
        rounds = {
            1: [odds(1, 1, 5, 0.80, 0.12), odds(1, 2, 6, 0.55, 0.25)],
            2: [odds(2, 1, 6, 0.78, 0.13), odds(2, 2, 5, 0.50, 0.28)],
        }
        route = plan_route(rounds, NAMES, Rules(horizon=2))
        assert len({p.team for p in route.picks}) == 2

    def test_it_holds_a_club_back_for_the_week_that_needs_it(self):
        """Alpha is the best pick in both rounds. Greedy spends them in GW1,
        where Beta would also have done, and is left with nothing in GW2."""
        rounds = {
            1: [odds(1, 1, 5, 0.75, 0.15), odds(1, 2, 6, 0.72, 0.16)],
            2: [odds(2, 1, 6, 0.75, 0.15), odds(2, 2, 5, 0.30, 0.30)],
        }
        rules = Rules(horizon=2)
        planned = plan_route(rounds, NAMES, rules)
        greedy = greedy_route(rounds, NAMES, rules)

        assert planned.first.name == "Beta"
        assert greedy.first.name == "Alpha"
        assert planned.survival > greedy.survival

    def test_the_draw_rule_can_change_the_pick(self):
        """A side that draws a lot is a bad pick in one pool and a good one in
        the other. Getting this backwards is not a small error."""
        rounds = {1: [odds(1, 1, 5, win=0.50, draw=0.40),
                      odds(1, 2, 6, win=0.55, draw=0.10)]}
        assert plan_route(rounds, NAMES, Rules(horizon=1)).first.name == "Beta"
        assert plan_route(rounds, NAMES, SURVIVES).first.name == "Alpha"

    def test_the_route_is_truncated_when_the_clubs_run_out(self):
        rounds = {
            1: [odds(1, 1, 5, 0.7, 0.2)],
            2: [odds(2, 1, 6, 0.7, 0.2)],   # only Alpha, and Alpha is spent
        }
        route = plan_route(rounds, NAMES, Rules(horizon=2))
        assert route.status == "truncated"
        assert route.rounds == 1

    def test_forcing_a_pick_plans_around_it(self):
        rounds = {
            1: [odds(1, 1, 5, 0.75, 0.15), odds(1, 2, 6, 0.72, 0.16)],
            2: [odds(2, 1, 6, 0.75, 0.15), odds(2, 2, 5, 0.30, 0.30)],
        }
        forced = plan_route(rounds, NAMES, Rules(horizon=2), force=(1, 1))
        assert forced.first.name == "Alpha"
        assert forced.picks[1].name == "Beta"

    def test_survival_is_the_product_of_the_rounds(self):
        rounds = {1: [odds(1, 1, 5, 0.60, 0.2)], 2: [odds(2, 2, 6, 0.50, 0.2)]}
        route = plan_route(rounds, NAMES, Rules(horizon=2))
        assert route.survival == pytest.approx(0.30)

    def test_no_fixtures_is_reported_not_crashed(self):
        assert plan_route({}, NAMES, OUT).status == "no-fixtures"


class TestState:
    def test_a_round_trip_keeps_the_record(self, tmp_path):
        state = LmsState()
        state.record(1, 3, "Gamma")
        path = write_state(state, tmp_path / "lms.json")
        assert json.loads(path.read_text())["picks"][0]["name"] == "Gamma"
        assert read_state(path).used == [3]

    def test_changing_your_mind_replaces_rather_than_spends_two_clubs(self):
        state = LmsState()
        state.record(1, 3, "Gamma")
        state.record(1, 4, "Delta")
        assert state.used == [4]

    def test_results_are_read_from_the_fixture_list(self):
        state = LmsState()
        state.record(1, 1, "Alpha")
        state.record(2, 2, "Beta")
        settled = state.settle([
            fixture(1, 1, 1, 2, finished=True, team_h_score=2, team_a_score=1),
            fixture(2, 2, 3, 2, finished=True, team_h_score=1, team_a_score=1),
        ])
        assert settled == 2
        assert [p.result for p in state.picks] == [WON, DREW]

    def test_a_draw_ends_it_or_does_not_depending_on_the_pool(self):
        state = LmsState(picks=[Pick(1, 2, "Beta", result=DREW)])
        assert not state.alive(draw_survives=False, lives=1)
        assert state.alive(draw_survives=True, lives=1)

    def test_a_spare_life_absorbs_one_defeat(self):
        state = LmsState(picks=[Pick(1, 2, "Beta", result=LOST)])
        assert not state.alive(draw_survives=False, lives=1)
        assert state.alive(draw_survives=False, lives=2)

    def test_a_borrowed_club_is_spent_but_not_played(self):
        state = LmsState()
        state.borrow(3, "Gamma")
        assert state.used == [3]
        assert state.rounds_survived(draw_survives=False) == 0
        assert state.alive(draw_survives=False, lives=1)

    def test_a_missing_record_is_an_empty_season(self, tmp_path):
        assert read_state(tmp_path / "nothing.json").used == []


class TestNames:
    TEAMS = [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
             {"id": 2, "name": "Man City", "short_name": "MCI"},
             {"id": 3, "name": "Man Utd", "short_name": "MUN"}]

    def test_full_name_short_name_and_prefix(self):
        assert resolve_team("arsenal", self.TEAMS) == 1
        assert resolve_team("MCI", self.TEAMS) == 2
        assert resolve_team("ars", self.TEAMS) == 1

    def test_an_ambiguous_name_is_an_error_not_a_guess(self):
        with pytest.raises(UnknownTeam, match="more than one"):
            resolve_team("Man", self.TEAMS)

    def test_an_unknown_name_says_so(self):
        with pytest.raises(UnknownTeam, match="no club"):
            resolve_team("Real Madrid", self.TEAMS)


class TestAdvice:
    ROUNDS = {
        1: [odds(1, 1, 5, 0.75, 0.15), odds(1, 2, 6, 0.72, 0.16),
            odds(1, 3, 4, 0.40, 0.30)],
        2: [odds(2, 1, 6, 0.75, 0.15), odds(2, 2, 5, 0.30, 0.30),
            odds(2, 3, 4, 0.45, 0.25)],
    }

    def test_it_recommends_the_club_that_leaves_the_best_route(self):
        result = advise(self.ROUNDS, NAMES, LmsState(), Rules(horizon=2))
        assert result.status == "alive"
        assert result.pick == "Beta"
        assert "Alpha" in result.reason   # says why the favourite was passed over

    def test_every_option_is_priced_over_the_whole_route(self):
        result = advise(self.ROUNDS, NAMES, LmsState(), Rules(horizon=2))
        assert [o.cost for o in result.options][0] == pytest.approx(0.0)
        assert all(o.cost >= 0 for o in result.options)
        alpha = next(o for o in result.options if o.name == "Alpha")
        assert alpha.cost > 0        # the favourite this week is the worse route
        assert alpha.reserved_for == 2   # because the plan wants them in GW2

    def test_used_clubs_never_appear_as_options(self):
        state = LmsState()
        state.record(0, 2, "Beta")
        result = advise(self.ROUNDS, NAMES, state, Rules(horizon=2))
        assert "Beta" not in [o.name for o in result.options]
        assert result.used == ["Beta"]

    def test_being_out_stops_the_advice(self):
        state = LmsState(picks=[Pick(1, 2, "Beta", result=LOST)])
        result = advise(self.ROUNDS, NAMES, state, Rules(horizon=2))
        assert result.status == "out"
        assert result.pick is None
        assert "Eliminated" in result.reason

    def test_running_out_of_clubs_is_explained(self):
        state = LmsState()
        for team in (1, 2, 3):
            state.borrow(team, NAMES[team])
        result = advise(self.ROUNDS, NAMES, state, Rules(horizon=2))
        assert result.status == "no-fixtures"

    def test_the_planning_gain_is_reported_against_greedy(self):
        result = advise(self.ROUNDS, NAMES, LmsState(), Rules(horizon=2))
        assert result.planning_gain > 0
        assert result.route["survival"] > result.greedy["survival"]


class TestCrowd:
    def test_the_field_clusters_on_the_favourite(self):
        shares = crowd_shares([odds(1, 1, 5, 0.75, 0.15), odds(1, 2, 6, 0.45, 0.25)], OUT)
        assert shares[1] > shares[2]
        assert sum(shares.values()) == pytest.approx(1.0)

    def test_it_is_a_model_and_never_certain(self):
        """No pool publishes its picks, so nothing here should ever read as
        knowledge. A share of 100% would."""
        shares = crowd_shares([odds(1, 1, 5, 0.90, 0.05), odds(1, 2, 6, 0.20, 0.30)], OUT)
        assert shares[1] < 0.9
        assert shares[2] > 0.05


class TestPricingEdges:
    def test_a_pick_that_cannot_finish_the_horizon_is_not_ranked_first(self):
        """A forced pick that strands a later round produces a shorter route,
        and a shorter route multiplies to a *higher* number. Ranking on that
        would recommend precisely the club that breaks the season."""
        rounds = {
            1: [odds(1, 1, 5, 0.30, 0.20), odds(1, 2, 6, 0.90, 0.05)],
            2: [odds(2, 2, 5, 0.60, 0.20)],   # only Beta plays in GW2
        }
        result = advise(rounds, NAMES, LmsState(), Rules(horizon=2))
        assert result.pick == "Alpha"
        beta = next(o for o in result.options if o.name == "Beta")
        assert beta.route_survival == 0.0
        assert beta.cost == pytest.approx(1.0)


class TestSeasonAdvice:
    def test_results_are_settled_and_written_back(self, tmp_path, monkeypatch):
        from gaffer.lms.advise import season_advice
        from gaffer.lms.rules import Rules as R

        path = tmp_path / "lms.json"
        state = LmsState()
        state.record(1, 1, "Alpha")
        write_state(state, path)

        bootstrap = {"teams": [{"id": i, "name": NAMES[i], "short_name": NAMES[i][:3].upper()}
                               for i in NAMES],
                     "events": [{"id": 2, "is_next": True}]}
        played = [fixture(1, 1, 1, 2, finished=True, team_h_score=3, team_a_score=0),
                  fixture(2, 2, 1, 3)]
        monkeypatch.setattr("gaffer.config.LMS_USED", "")

        result = season_advice(played, bootstrap, FakeStrength({(1, 3): (1.6, 0.9)}),
                               gameweek=2, rules=R(horizon=1), state_path=path)

        assert json.loads(path.read_text())["picks"][0]["result"] == WON
        assert result.rounds_survived == 1
        assert "Alpha" not in [o.name for o in result.options]
