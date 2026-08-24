"""Quantities the rules of the game fix in advance, which the model must respect.

Modelling each player in isolation lets the parts stop adding up to the whole.
Scored against GW1 the board under-predicted by 37%, and it decomposed almost
entirely into two conservation failures:

  minutes   14,362 projected across the league against 19,652 actually played.
            Tottenham came out at 1004 and Hull at 118 — every Hull player who
            went the full ninety was projected at five minutes, because none of
            them had a Premier League record to be projected from. Hull then
            beat Manchester United 2-0.

  bonus     30.1 projected across the round against 64 awarded. Half the bonus
            in the game was allocated to nobody.

Both totals are set by the rules, not estimated, so normalising to them cannot
be fitted to a result — which is why these are safe to change on one gameweek's
evidence when almost nothing else is.
"""
from __future__ import annotations

import pytest

from gaffer.model.carryover import effective_player
from gaffer.model.minutes import (START_SURVIVES_60, TEAM_MINUTES_PER_FIXTURE,
                                  MinutesModel, estimate, normalise_team)


def model(expected_minutes, available=1.0):
    share = min(1.0, expected_minutes / 90.0)
    return MinutesModel(p_appear=share, p_60=share * START_SURVIVES_60,
                        expected_minutes=expected_minutes, p_available=available)


def squad(regulars=11, regular_minutes=70.0, fringe=9, fringe_minutes=12.0):
    out = {i: model(regular_minutes) for i in range(regulars)}
    out.update({regulars + i: model(fringe_minutes) for i in range(fringe)})
    return out


class TestThePromotedClubBug:
    """A player with no prior Premier League season had this season's totals
    divided by a full 38 games, so a promoted-club starter who had just played
    ninety minutes read as a substitute — for the rest of the season."""

    def test_a_promoted_starter_reads_as_a_starter_after_one_game(self):
        played_the_full_match = {"minutes": 90, "starts": 1, "status": "a"}
        got = estimate(effective_player(played_the_full_match, None, games_played=1))
        assert got.expected_minutes > 60, "regression: this returned 6.9"

    def test_before_a_ball_is_kicked_there_is_nothing_to_scale(self):
        """Zero games played must not divide by zero; the base rate stands in
        until the club normalisation gives him a share of a real match."""
        got = estimate(effective_player({"minutes": 0, "starts": 0, "status": "a"},
                                        None, games_played=0))
        assert got.expected_minutes > 0


class TestMinutesAddUpToAMatch:
    def test_a_normal_squad_sums_to_a_full_match(self):
        out = normalise_team(squad())
        total = sum(m.expected_minutes for m in out.values())
        assert total == pytest.approx(TEAM_MINUTES_PER_FIXTURE, abs=0.5)

    def test_a_promoted_squad_with_no_record_still_fields_a_team(self):
        """Hull's actual GW1 shape: twenty players, five minutes each."""
        out = normalise_team({i: model(5.0) for i in range(20)})
        total = sum(m.expected_minutes for m in out.values())
        assert total == pytest.approx(TEAM_MINUTES_PER_FIXTURE, abs=0.5)

    def test_nobody_plays_more_than_a_full_match(self):
        lopsided = {0: model(88.0), 1: model(85.0)}
        lopsided.update({i: model(2.0) for i in range(2, 20)})
        out = normalise_team(lopsided)
        assert max(m.expected_minutes for m in out.values()) <= 90.0 + 1e-6

    def test_scaling_preserves_who_is_first_choice(self):
        out = normalise_team(squad())
        assert out[0].expected_minutes > out[15].expected_minutes

    def test_the_unavailable_do_not_take_a_share(self):
        """Their minutes go to team-mates, not to a player already ruled out."""
        s = squad()
        s[0] = model(70.0, available=0.0)
        out = normalise_team(s)
        assert out[0].expected_minutes == pytest.approx(70.0), "left untouched"
        rest = sum(m.expected_minutes for pid, m in out.items() if pid != 0)
        assert rest == pytest.approx(TEAM_MINUTES_PER_FIXTURE, abs=0.5)

    def test_appearance_odds_move_with_the_minutes(self):
        """The points model reads p_appear and p_60 too. Scaling minutes alone
        would leave a player down for eighty minutes and 6% likely to appear."""
        out = normalise_team({i: model(5.0) for i in range(20)})
        assert out[0].p_appear > 5.0 / 90.0

    def test_probabilities_never_exceed_availability(self):
        out = normalise_team({i: model(5.0, available=0.5) for i in range(20)})
        for m in out.values():
            assert m.p_appear <= m.p_available + 1e-9
            assert m.p_60 <= m.p_appear + 1e-9

    def test_a_club_with_nobody_available_does_not_divide_by_zero(self):
        s = {i: model(50.0, available=0.0) for i in range(20)}
        assert normalise_team(s) == s

    def test_with_no_signal_at_all_the_clubs_valuation_decides(self):
        """A promoted squad in pre-season has no minutes to rank on. Price is
        the only read on who the club thinks is first choice."""
        nothing = {i: model(0.0) for i in range(20)}
        price = {i: (6.0 if i < 11 else 4.0) for i in range(20)}
        out = normalise_team(nothing, fallback_weight=price)
        assert sum(m.expected_minutes for m in out.values()) == pytest.approx(
            TEAM_MINUTES_PER_FIXTURE, abs=0.5)
        assert out[0].expected_minutes > out[15].expected_minutes


class TestBonusAddsUpToAFixture:
    """Three points, two and one. The logistic scored each player against an
    absolute bps scale with no idea who else was on the pitch."""

    def board(self):
        """Two clubs, one fixture, wired the way `project` expects."""
        elements, teams = [], [{"id": 1, "short_name": "AAA"}, {"id": 2, "short_name": "BBB"}]
        for i in range(1, 23):
            elements.append({"id": i, "team": 1 if i <= 11 else 2,
                             "element_type": 3, "now_cost": 50})
        return {"elements": elements, "teams": teams,
                "element_types": [{"id": 3, "singular_name_short": "MID"}]}

    def runs(self, bonus_each):
        from gaffer.model.points import ExpectedPoints

        out = {}
        for i in range(1, 23):
            home = i <= 11
            out[i] = [ExpectedPoints(
                player_id=i, gameweek=1, opponent="BBB" if home else "AAA",
                at_home=home, total=2.0 + bonus_each, minutes=80.0,
                variance=1.0 + bonus_each, components={"bonus": bonus_each})]
        return out

    def test_a_fixture_hands_out_exactly_six(self):
        from gaffer.model.points import BONUS_PER_FIXTURE, normalise_bonus

        runs = self.runs(0.2)          # 22 x 0.2 = 4.4, short of six
        normalise_bonus(self.board(), runs)
        total = sum(r[0].components["bonus"] for r in runs.values())
        assert total == pytest.approx(BONUS_PER_FIXTURE, abs=0.05)

    def test_it_scales_down_as_well_as_up(self):
        from gaffer.model.points import BONUS_PER_FIXTURE, normalise_bonus

        runs = self.runs(1.0)          # 22 points of bonus in one match
        normalise_bonus(self.board(), runs)
        total = sum(r[0].components["bonus"] for r in runs.values())
        assert total == pytest.approx(BONUS_PER_FIXTURE, abs=0.05)

    def test_the_total_moves_with_the_component(self):
        """Rewriting the component without the total would leave the two
        disagreeing, and the board prints the total."""
        from gaffer.model.points import normalise_bonus

        runs = self.runs(0.2)
        before = runs[1][0].total
        normalise_bonus(self.board(), runs)
        after = runs[1][0]
        assert after.total == pytest.approx(before - 0.2 + after.components["bonus"], abs=1e-6)

    def test_ordering_survives(self):
        from gaffer.model.points import normalise_bonus

        runs = self.runs(0.2)
        runs[1][0].components["bonus"] = 0.9    # the standout in this fixture
        normalise_bonus(self.board(), runs)
        assert runs[1][0].components["bonus"] > runs[2][0].components["bonus"]

    def test_a_fixture_nobody_can_earn_bonus_in_is_left_alone(self):
        from gaffer.model.points import normalise_bonus

        runs = self.runs(0.0)
        normalise_bonus(self.board(), runs)
        assert sum(r[0].components["bonus"] for r in runs.values()) == 0.0
