"""The weekly post-mortem, built from the GW1 numbers that prompted it.

44 points against a league mean of 49.1 looked like the model getting something
wrong. It was not: the league ranged 23 to 74 that week, a standard deviation of
13.6, so the gap was 0.37 of one deviation. The eleven fielded were the model's
own top eleven of those fifteen, and both managers who beat the field captained
the same player.

These fixtures are that week, so they are a regression record rather than
invented shapes — and the first test is the one the section exists for.
"""
from __future__ import annotations

import pytest

from gaffer.league.standings import Rival
from gaffer.review import DIFFERENTIAL_OWNERSHIP, review_gameweek, summarise

# The workplace league's actual GW1 scores, the user excluded.
LEAGUE = [74, 71, 57, 57, 53, 52, 51, 51, 50, 48, 45, 38, 23, 23]


def rivals(scores=None):
    return [Rival(entry_id=i, name=f"r{i}", manager="", rank=0, total_points=0,
                  gameweek_points=s) for i, s in enumerate(scores or LEAGUE)]


def bootstrap(players):
    return {
        "elements": [{"id": p["id"], "web_name": p["name"], "element_type": 3,
                      "selected_by_percent": p.get("own", 50.0)} for p in players],
        "element_types": [{"id": 3, "singular_name_short": "MID"}],
    }


def build(players, captain_id, bench_ids=(), bench_points=0, entry_points=None):
    picks, live = [], {}
    for slot, p in enumerate(players, start=1):
        on_bench = p["id"] in bench_ids
        picks.append({"element": p["id"], "position": slot,
                      "multiplier": 0 if on_bench else (2 if p["id"] == captain_id else 1),
                      "is_captain": p["id"] == captain_id})
        live[p["id"]] = {"total_points": p["pts"], "minutes": p.get("min", 90)}
    scored = sum(p["pts"] * (2 if p["id"] == captain_id else 1)
                 for p in players if p["id"] not in bench_ids)
    picks_payload = {"picks": picks,
                     "entry_history": {"points": entry_points if entry_points is not None
                                       else scored,
                                       "points_on_bench": bench_points}}
    return picks_payload, live, bootstrap(players)


def gw1_squad():
    """The real eleven, their ownership and what they returned."""
    return [
        {"id": 1, "name": "Verbruggen", "pts": 6, "own": 21.6},
        {"id": 2, "name": "Guehi", "pts": 10, "own": 18.2},
        {"id": 3, "name": "Virgil", "pts": 2, "own": 19.6},
        {"id": 4, "name": "Senesi", "pts": 3, "own": 8.0},
        {"id": 5, "name": "Enzo", "pts": 1, "own": 5.1, "min": 25},
        {"id": 6, "name": "Anderson", "pts": 2, "own": 7.0, "min": 62},
        {"id": 7, "name": "Mbeumo", "pts": 2, "own": 37.2},
        {"id": 8, "name": "Schade", "pts": 3, "own": 3.6},
        {"id": 9, "name": "Thiago", "pts": 0, "own": 17.2},
        {"id": 10, "name": "Haaland", "pts": 2, "own": 68.7},
        {"id": 11, "name": "Joao Pedro", "pts": 11, "own": 65.0},
    ]


class TestTheGapIsReadInContext:
    """Without the spread beside it a four-point gap invites a change to a
    model that is working. This is the finding the section exists to deliver."""

    def review(self):
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        return review_gameweek(1, p, live, boot, rivals=rivals(), model_captain=10)

    def test_it_reports_the_leagues_spread_not_just_the_gap(self):
        r = self.review()
        assert r.league_spread == pytest.approx(13.6, abs=0.1)

    def test_four_points_against_that_spread_is_an_ordinary_week(self):
        r = self.review()
        assert r.deviations_from_mean == pytest.approx(-0.38, abs=0.02)
        assert r.within_normal_variation
        assert "ordinary week" in r.verdict

    def test_the_same_gap_against_a_tight_league_is_not_ordinary(self):
        """It is the ratio that matters, so a narrow league must read
        differently — otherwise the section is just always reassuring."""
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        tight = review_gameweek(1, p, live, boot,
                                rivals=rivals([48, 49, 47, 48, 50, 49, 48]),
                                model_captain=10)
        assert not tight.within_normal_variation
        assert "worth" in tight.verdict

    def test_it_says_where_you_finished(self):
        r = self.review()
        assert (r.league_position, r.league_size) == (12, 15)


class TestTheCaptain:
    def test_the_models_own_pick_blanking_is_not_an_override(self):
        """GW1: right call, bad outcome. Reading that as a mistake would send
        the reader after a model that had just agreed with them."""
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals(), model_captain=10)
        assert r.captain_agreed
        assert r.captain.name == "Haaland" and r.captain.points == 2

    def test_overriding_the_model_is_flagged(self):
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals(), model_captain=11)
        assert not r.captain_agreed

    def test_cost_is_measured_against_your_own_eleven(self):
        """Hindsight over 600 players is not a decision anyone could have made,
        and quoting it would make every week look like a blunder."""
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals(), model_captain=10)
        assert r.captain_cost == 9        # Joao Pedro 11 against Haaland 2

    def test_captaining_the_top_scorer_costs_nothing(self):
        p, live, boot = build(gw1_squad(), captain_id=11, entry_points=55)
        r = review_gameweek(1, p, live, boot, rivals=rivals(), model_captain=11)
        assert r.captain_cost == 0


class TestBenchAndSubs:
    def test_bench_points_are_reported(self):
        p, live, boot = build(gw1_squad(), captain_id=10, bench_points=5,
                              entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals())
        assert r.points_on_bench == 5

    def test_a_starter_who_never_came_on_counts_as_an_auto_sub(self):
        squad = gw1_squad()
        squad[3] = dict(squad[3], min=0, pts=0)
        p, live, boot = build(squad, captain_id=10, entry_points=41)
        r = review_gameweek(1, p, live, boot, rivals=rivals())
        assert r.auto_subs == 1

    def test_a_full_eleven_fires_no_subs(self):
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        assert review_gameweek(1, p, live, boot, rivals=rivals()).auto_subs == 0


class TestDifferentialsAndProjection:
    def test_it_counts_the_low_owned_picks_and_what_they_returned(self):
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals())
        assert [d.name for d in r.differentials] == ["Senesi", "Enzo", "Anderson", "Schade"]
        assert sum(d.points for d in r.differentials) == 9
        assert all(d.ownership < DIFFERENTIAL_OWNERSHIP for d in r.differentials)

    def test_the_eleven_is_scored_against_what_we_projected(self):
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        proj = {1: 3.31, 2: 4.17, 3: 3.55, 4: 3.22, 5: 4.33, 6: 4.33,
                7: 4.25, 8: 3.79, 9: 4.32, 10: 7.03, 11: 3.93}
        r = review_gameweek(1, p, live, boot, rivals=rivals(), projections=proj)
        assert r.xi_points == 42
        assert r.xi_projected == pytest.approx(46.23, abs=0.01)

    def test_the_armband_is_excluded_from_the_elevens_raw_return(self):
        """Comparing a doubled score against an undoubled projection would
        flatter the model by whatever the captain happened to score."""
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals())
        assert r.xi_points == 42 and r.points == 44


class TestItDegradesHonestly:
    def test_a_league_nobody_can_be_read_from_yet(self):
        """Picks are private until a deadline passes; an empty field must not
        divide by zero or invent a position."""
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=[])
        assert r.league_spread == 0.0
        assert r.deviations_from_mean == 0.0
        assert r.league_position == 0
        assert "No league to compare" in r.verdict

    def test_an_unfinished_gameweek_says_so(self):
        """FPL had not settled GW1's bonus when this was first run. Reporting
        provisional numbers as final is the same mistake as a banner that
        claims the season has not started once it has."""
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals(), provisional=True)
        assert r.provisional
        assert any("provisional" in line for line in summarise(r))

    def test_a_settled_gameweek_does_not(self):
        p, live, boot = build(gw1_squad(), captain_id=10, entry_points=44)
        r = review_gameweek(1, p, live, boot, rivals=rivals())
        assert not any("provisional" in line for line in summarise(r))
