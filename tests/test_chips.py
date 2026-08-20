"""Chip timing, and the greedy trap it has to avoid.

A chip is a season-long allocation, not a weekly choice. The failure mode is an
engine that fires in gameweek one because that week narrowly leads a short
window — over six weeks every week looks like every other week, so the best of
them is barely better than the average and a naive rule always plays.
"""
import pytest

from gaffer.optimise.chips import advise_chip, bench_boost, triple_captain
from gaffer.rank import PlayerRow


def row(pid, position, per_gw, horizon=6):
    xp = per_gw if isinstance(per_gw, list) else [per_gw] * horizon
    return PlayerRow(
        id=pid, name=f"P{pid}", team=1, position=position, price=5.0, owned=1.0,
        xp=xp, projected=sum(xp), per_million=sum(xp) / 5.0, minutes=80.0,
        fixture_score=3.0, availability=1.0, confidence="high", moved_club=False, note="",
    )


@pytest.fixture
def squad():
    ids, positions, rows = [], {}, {}
    spec = [("GKP", 2, 3.0), ("DEF", 5, 4.0), ("MID", 5, 5.0), ("FWD", 3, 6.0)]
    pid = 1
    for position, count, base in spec:
        for i in range(count):
            rows[pid] = row(pid, position, base - i * 0.4)
            positions[pid] = position
            ids.append(pid)
            pid += 1
    return ids, positions, rows


class TestGreedyTrap:
    def test_does_not_play_when_later_weeks_are_unseen(self):
        """Six weeks in view, thirty-two unseen — where the doubles live."""
        values = [7.0, 7.1, 7.2, 7.0, 6.9, 7.0]
        advice = advise_chip("triple captain", values, 1, gameweeks_remaining=38)
        assert advice.action == "hold"
        assert "unseen" in advice.reason

    def test_does_not_play_on_a_flat_window_even_with_full_view(self):
        """Leading a flat window is not standing out."""
        values = [7.2, 7.1, 7.0, 7.0, 6.9, 7.0]
        advice = advise_chip("triple captain", values, 1, gameweeks_remaining=6)
        assert advice.action == "hold"

    def test_plays_when_this_week_genuinely_stands_out(self):
        """A double gameweek: this week is worth twice any other, and nothing
        later is hidden."""
        values = [18.0, 7.0, 6.8, 7.1, 6.9, 7.0]
        advice = advise_chip("bench boost", values, 12, gameweeks_remaining=6)
        assert advice.action == "play"
        assert advice.best_gameweek == 12

    def test_reports_the_best_week_it_can_see(self):
        values = [5.0, 6.0, 12.0, 5.5]
        advice = advise_chip("bench boost", values, 10, gameweeks_remaining=4)
        assert advice.best_gameweek == 12
        assert advice.best_value == 12.0

    def test_no_values_holds(self):
        assert advise_chip("free hit", [], 1).action == "hold"


class TestChipValues:
    def test_triple_captain_is_worth_the_best_player(self, squad):
        ids, positions, rows = squad
        values = triple_captain(ids, rows, positions, horizon=3)
        best = max(r.xp[0] for r in rows.values())
        assert values[0] == pytest.approx(best)

    def test_bench_boost_is_worth_the_four_substitutes(self, squad):
        ids, positions, rows = squad
        values = bench_boost(ids, rows, positions, horizon=3)
        assert values[0] > 0
        total = sum(r.xp[0] for r in rows.values())
        assert values[0] < total, "bench boost cannot be worth the whole squad"

    def test_values_track_the_horizon_length(self, squad):
        ids, positions, rows = squad
        assert len(triple_captain(ids, rows, positions, horizon=4)) == 4
