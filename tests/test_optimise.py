"""The optimiser: squad selection, lineup choice and transfer pricing.

The constraint checks matter most. A squad that breaks the budget or the
three-per-club rule is not a suggestion, it is an illegal team the site will
reject — so these are guarding correctness, not preference.
"""
import pytest

from gaffer import config
from gaffer.optimise import best_lineup, evaluate_transfers, pick_squad, prune_candidates
from gaffer.optimise.squad import SQUAD_QUOTA
from gaffer.rank import PlayerRow


def row(pid, position, team, price, per_gw, horizon=3, **kw):
    return PlayerRow(
        id=pid, name=f"P{pid}", team=team, position=position, price=price,
        owned=1.0, xp=[per_gw] * horizon, var=[0.0] * horizon,
        projected=per_gw * horizon,
        per_million=per_gw * horizon / price, minutes=80.0,
        fixture_score=3.0, availability=kw.get("availability", 1.0),
        confidence="high", moved_club=False, note="",
    )


@pytest.fixture
def pool():
    """A pool wide enough to pick a legal fifteen from, with a clear optimum."""
    rows = []
    pid = 1
    for position, count in (("GKP", 8), ("DEF", 20), ("MID", 20), ("FWD", 12)):
        for i in range(count):
            # Price rises with quality, so the budget actually bites.
            price = 4.0 + i * 0.5
            per_gw = 1.0 + i * 0.35
            rows.append(row(pid, position, team=(pid % 8) + 1, price=price, per_gw=per_gw))
            pid += 1
    return rows


class TestSquadConstraints:
    def test_picks_exactly_fifteen(self, pool):
        squad = pick_squad(pool, time_limit=20)
        assert len(squad.players) == config.SQUAD_SIZE

    def test_respects_the_budget(self, pool):
        squad = pick_squad(pool, budget=100.0, time_limit=20)
        assert squad.cost <= 100.0 + 1e-6

    def test_respects_position_quotas(self, pool):
        squad = pick_squad(pool, time_limit=20)
        by_id = {r.id: r for r in pool}
        counts = {}
        for pid in squad.players:
            counts[by_id[pid].position] = counts.get(by_id[pid].position, 0) + 1
        assert counts == SQUAD_QUOTA

    def test_respects_the_club_limit(self, pool):
        squad = pick_squad(pool, max_per_club=3, time_limit=20)
        by_id = {r.id: r for r in pool}
        counts = {}
        for pid in squad.players:
            counts[by_id[pid].team] = counts.get(by_id[pid].team, 0) + 1
        assert max(counts.values()) <= 3

    def test_fields_eleven_every_gameweek(self, pool):
        squad = pick_squad(pool, time_limit=20)
        for gw, starters in squad.starters_by_gameweek.items():
            assert len(starters) == 11, f"gameweek {gw} did not field eleven"
            assert set(starters) <= set(squad.players)

    def test_captain_is_always_a_starter(self, pool):
        squad = pick_squad(pool, time_limit=20)
        for gw, captain in squad.captain_by_gameweek.items():
            assert captain in squad.starters_by_gameweek[gw]

    def test_a_tighter_budget_cannot_score_more(self, pool):
        rich = pick_squad(pool, budget=100.0, time_limit=20)
        poor = pick_squad(pool, budget=80.0, time_limit=20)
        assert poor.expected_points <= rich.expected_points + 1e-6

    def test_locked_player_is_always_selected(self, pool):
        cheapest = min(pool, key=lambda r: r.projected)
        squad = pick_squad(pool, locked=[cheapest.id], time_limit=20)
        assert cheapest.id in squad.players

    def test_banned_player_is_never_selected(self, pool):
        best = max(pool, key=lambda r: r.projected)
        squad = pick_squad(pool, banned=[best.id], time_limit=20)
        assert best.id not in squad.players


class TestPruning:
    def test_keeps_the_cheap_enablers(self, pool):
        """Bench fodder is what funds the premium — dropping it hides the optimum."""
        kept = {r.id for r in prune_candidates(pool, per_position=3, cheap=4)}
        for position in SQUAD_QUOTA:
            cheapest = min((r for r in pool if r.position == position), key=lambda r: r.price)
            assert cheapest.id in kept

    def test_keeps_the_best(self, pool):
        kept = {r.id for r in prune_candidates(pool, per_position=3, cheap=2)}
        best = max(pool, key=lambda r: r.projected)
        assert best.id in kept

    def test_drops_unavailable_players(self, pool):
        pool = pool + [row(999, "MID", 1, 5.0, 9.9, availability=0.0)]
        kept = {r.id for r in prune_candidates(pool)}
        assert 999 not in kept


class TestLineup:
    def _squad(self):
        ids, positions, xp = [], {}, {}
        spec = [("GKP", 2, 3.0), ("DEF", 5, 4.0), ("MID", 5, 5.0), ("FWD", 3, 6.0)]
        pid = 1
        for position, count, base in spec:
            for i in range(count):
                ids.append(pid)
                positions[pid] = position
                xp[pid] = base - i * 0.5
                pid += 1
        return ids, positions, xp

    def test_fields_eleven_in_a_legal_shape(self):
        ids, positions, xp = self._squad()
        lineup = best_lineup(ids, xp, positions)
        assert len(lineup.starters) == 11
        keepers = [p for p in lineup.starters if positions[p] == "GKP"]
        assert len(keepers) == 1

    def test_bench_holds_the_remaining_four(self):
        ids, positions, xp = self._squad()
        lineup = best_lineup(ids, xp, positions)
        assert len(lineup.bench) == 4
        assert set(lineup.starters) & set(lineup.bench) == set()

    def test_captain_is_the_highest_projection(self):
        ids, positions, xp = self._squad()
        lineup = best_lineup(ids, xp, positions)
        assert lineup.captain == max(lineup.starters, key=lambda p: xp[p])

    def test_vice_differs_from_captain(self):
        ids, positions, xp = self._squad()
        lineup = best_lineup(ids, xp, positions)
        assert lineup.vice != lineup.captain

    def test_reserve_keeper_is_last_on_the_bench(self):
        """He can only replace a keeper, so he must never block an outfield sub."""
        ids, positions, xp = self._squad()
        lineup = best_lineup(ids, xp, positions)
        assert positions[lineup.bench[-1]] == "GKP"

    def test_expected_points_counts_the_captain_twice(self):
        ids, positions, xp = self._squad()
        lineup = best_lineup(ids, xp, positions)
        raw = sum(xp[p] for p in lineup.starters)
        assert lineup.expected_points == pytest.approx(raw + xp[lineup.captain], abs=0.01)

    def test_squad_without_a_keeper_is_rejected(self):
        with pytest.raises(ValueError):
            best_lineup([1], {1: 4.0}, {1: "MID"})


class TestRobustCaptaincy:
    """The armband doubles a single, noisy one-week number. A defender's steady
    floor in a good fixture can nose ahead of the best forward in the game for
    one week without being the better captain over any longer view, so the
    horizon and the spread temper the pick — the exact case that put a centre-back
    over Haaland for gameweek two."""

    # Eleven with a legal 4-4-2 outfield plus one keeper.
    def _squad(self, weekly, horizon, sd):
        ids = list(weekly)
        positions = {1: "GKP", 2: "DEF", 3: "DEF", 4: "DEF", 5: "DEF",
                     6: "MID", 7: "MID", 8: "MID", 9: "MID", 10: "FWD", 11: "FWD"}
        return ids, positions

    def test_a_within_noise_weekly_lead_stays_on_the_horizon_best(self):
        # Player 2 (a defender) leads the week; player 10 (the striker) is far
        # and away the best over the horizon. The weekly gap is small against the
        # spread, so the armband must not leave the striker.
        weekly = {1: 3.0, 2: 6.1, 3: 4.0, 4: 4.0, 5: 4.0,
                  6: 5.0, 7: 5.0, 8: 4.0, 9: 4.0, 10: 4.5, 11: 4.0}
        horizon = dict.fromkeys(weekly, 24.0)
        horizon[10] = 38.0           # the striker is the season's best asset
        sd = {pid: 2.0 for pid in weekly}
        sd[2], sd[10] = 3.9, 3.0     # combined ≈ 4.9, half of it ≈ 2.45 > 1.6 gap
        ids, positions = self._squad(weekly, horizon, sd)
        lineup = best_lineup(ids, weekly, positions, horizon=horizon, sd=sd)
        assert lineup.captain == 10
        # Vice is the week's best of the rest — the defender who led the week.
        assert lineup.vice == 2

    def test_a_clear_weekly_lead_still_takes_the_armband(self):
        # Same anchor, but now the weekly leader is far enough ahead that the
        # lead is real, not noise — the armband follows it.
        weekly = {1: 3.0, 2: 12.0, 3: 4.0, 4: 4.0, 5: 4.0,
                  6: 5.0, 7: 5.0, 8: 4.0, 9: 4.0, 10: 4.5, 11: 4.0}
        horizon = dict.fromkeys(weekly, 24.0)
        horizon[10] = 38.0
        sd = {pid: 2.0 for pid in weekly}
        ids, positions = self._squad(weekly, horizon, sd)
        lineup = best_lineup(ids, weekly, positions, horizon=horizon, sd=sd)
        assert lineup.captain == 2

    def test_without_a_horizon_it_is_the_old_weekly_pick(self):
        weekly = {1: 3.0, 2: 6.1, 3: 4.0, 4: 4.0, 5: 4.0,
                  6: 5.0, 7: 5.0, 8: 4.0, 9: 4.0, 10: 4.5, 11: 4.0}
        _, positions = self._squad(weekly, {}, {})
        lineup = best_lineup(list(weekly), weekly, positions)
        assert lineup.captain == max(lineup.starters, key=lambda p: weekly[p])


class TestTransfers:
    def test_rejects_a_squad_of_the_wrong_size(self, pool):
        with pytest.raises(ValueError, match="squad of 15"):
            evaluate_transfers(pool, [r.id for r in pool[:10]])

    def test_an_optimal_squad_has_nothing_worth_doing(self, pool):
        squad = pick_squad(pool, time_limit=20)
        options = evaluate_transfers(pool, squad.players, bank=0.0, time_limit=15)
        assert max(o.net_gain for o in options) <= 0.5

    def test_a_weakened_squad_is_told_to_undo_the_damage(self, pool):
        by_id = {r.id: r for r in pool}
        squad = pick_squad(pool, time_limit=20).players
        best_mid = max((p for p in squad if by_id[p].position == "MID"),
                       key=lambda p: by_id[p].projected)
        spare = min((r for r in pool
                     if r.position == "MID" and r.id not in squad
                     and r.price <= by_id[best_mid].price),
                    key=lambda r: r.projected)
        weakened = [spare.id if p == best_mid else p for p in squad]
        bank = by_id[best_mid].price - spare.price

        options = evaluate_transfers(pool, weakened, bank=bank, free_transfers=1, time_limit=15)
        best = max(options, key=lambda o: o.net_gain)
        assert best.net_gain > 0
        assert spare.id in best.out

    def test_rolling_is_always_priced_at_zero(self, pool):
        squad = pick_squad(pool, time_limit=20)
        options = evaluate_transfers(pool, squad.players, time_limit=15)
        roll = next(o for o in options if o.transfers == 0)
        assert roll.net_gain == 0.0
        assert roll.hit == 0

    def test_a_second_transfer_costs_four(self, pool):
        by_id = {r.id: r for r in pool}
        squad = pick_squad(pool, time_limit=20).players
        mids = sorted((p for p in squad if by_id[p].position == "MID"),
                      key=lambda p: -by_id[p].projected)[:2]
        spares = sorted((r for r in pool if r.position == "MID" and r.id not in squad),
                        key=lambda r: r.projected)[:2]
        weakened = list(squad)
        bank = 0.0
        for old, new in zip(mids, spares):
            weakened[weakened.index(old)] = new.id
            bank += by_id[old].price - new.price

        options = evaluate_transfers(pool, weakened, bank=bank, free_transfers=1, time_limit=15)
        two = [o for o in options if o.transfers == 2]
        if two:
            assert two[0].hit == 4
            assert two[0].net_gain == pytest.approx(two[0].gross_gain - 4, abs=0.01)
