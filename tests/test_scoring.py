"""Prediction logging and in-season scoring.

The load-bearing rule is which prediction gets marked. Several runs make
projections for the same gameweek, and the only fair one is the last made
*before* the deadline — the one you could actually have acted on. Scoring a
later one would be marking the model's homework after showing it the answers.
"""
import pytest

from gaffer.rank import PlayerRow
from gaffer.score import score_gameweek, score_all, summarise
from gaffer.store import Store


def row(pid, xp, var=None, price=5.0):
    xs = xp if isinstance(xp, list) else [xp]
    return PlayerRow(
        id=pid, name=f"P{pid}", team=1, position="MID", price=price, owned=1.0,
        xp=xs, var=var or [1.0] * len(xs), projected=sum(xs),
        per_million=sum(xs) / price, minutes=80.0, fixture_score=3.0,
        availability=1.0, confidence="high", moved_club=False, note="",
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.upsert_reference(
        [{"id": 1, "name": "Alpha", "short_name": "ALP"}],
        [{"id": i, "web_name": f"P{i}", "team": 1, "element_type": 3,
          "team_join_date": None} for i in range(1, 41)],
        {3: "MID"},
    )
    yield s
    s.close()


def live(points_by_id, minutes=90):
    return {"elements": [
        {"id": pid, "stats": {"total_points": pts, "minutes": minutes,
                              "goals_scored": 0, "assists": 0,
                              "clean_sheets": 0, "bonus": 0}}
        for pid, pts in points_by_id.items()]}


class TestRecording:
    def test_writes_one_row_per_player_per_gameweek_ahead(self, store):
        store.record_predictions([row(1, [5.0, 4.0, 6.0])], first_gameweek=3)
        rows = [tuple(r) for r in store.conn.execute(
            "SELECT target_gameweek, horizon_index FROM prediction")]
        assert sorted(rows) == [(3, 0), (4, 1), (5, 2)]

    def test_keeps_the_variance_alongside_the_projection(self, store):
        store.record_predictions([row(1, [5.0], var=[9.0])], first_gameweek=1)
        assert store.conn.execute("SELECT variance FROM prediction").fetchone()[0] == 9.0

    def test_two_runs_both_survive(self, store):
        """History is the point — a later run must not overwrite an earlier one."""
        import time
        store.record_predictions([row(1, [5.0])], first_gameweek=1)
        time.sleep(1.05)
        store.record_predictions([row(1, [6.0])], first_gameweek=1)
        assert store.conn.execute(
            "SELECT COUNT(DISTINCT made_at) FROM prediction").fetchone()[0] == 2

    def test_records_actual_results(self, store):
        assert store.record_actuals(1, live({1: 8, 2: 2})) == 2
        assert store.conn.execute(
            "SELECT points FROM actual WHERE player_id = 1").fetchone()[0] == 8

    def test_a_gameweek_counts_as_scored_only_with_both_halves(self, store):
        store.record_actuals(1, live({1: 8}))
        assert store.scored_gameweeks() == []
        store.record_predictions([row(1, [5.0])], first_gameweek=1)
        assert store.scored_gameweeks() == [1]


class TestWhichPredictionCounts:
    def test_uses_the_last_prediction_before_the_deadline(self, store):
        """A later run must not be the one marked — that is hindsight."""
        import time
        store.record_predictions([row(i, [2.0]) for i in range(1, 21)], first_gameweek=1)
        time.sleep(1.05)
        cutoff = store.record_predictions(
            [row(i, [9.0]) for i in range(1, 21)], first_gameweek=1)
        store.record_actuals(1, live({i: 5 for i in range(1, 21)}))

        # Scoring with a cutoff at the second run must see the first run's 2.0s.
        paired = store.prediction_vs_actual(1, before=cutoff)
        assert {r["xp"] for r in paired} == {2.0}

    def test_without_a_cutoff_the_latest_prediction_is_used(self, store):
        store.record_predictions([row(i, [2.0]) for i in range(1, 21)], first_gameweek=1)
        store.record_actuals(1, live({i: 5 for i in range(1, 21)}))
        paired = store.prediction_vs_actual(1)
        assert paired and {r["xp"] for r in paired} == {2.0}


class TestScoring:
    def _seed(self, store, predicted, actual):
        store.record_predictions([row(i, [predicted(i)]) for i in range(1, 41)],
                                 first_gameweek=1)
        store.record_actuals(1, live({i: actual(i) for i in range(1, 41)}))

    def test_a_perfect_model_scores_perfectly(self, store):
        self._seed(store, lambda i: float(i % 12), lambda i: i % 12)
        result = score_gameweek(store, 1)
        assert result.mean_absolute_error == pytest.approx(0.0)
        assert result.rank_correlation == pytest.approx(1.0)
        assert result.bias == pytest.approx(0.0)

    def test_detects_systematic_over_prediction(self, store):
        self._seed(store, lambda i: 6.0, lambda i: 2)
        assert score_gameweek(store, 1).bias == pytest.approx(4.0)

    def test_detects_under_prediction_at_the_top(self, store):
        """Where squad points are actually won."""
        self._seed(store, lambda i: 3.0, lambda i: 20 if i <= 8 else 2)
        assert score_gameweek(store, 1).top_quintile_bias < -10

    def test_separates_players_who_appeared(self, store):
        store.record_predictions([row(i, [4.0]) for i in range(1, 41)], first_gameweek=1)
        payload = live({i: 4 for i in range(1, 41)})
        for element in payload["elements"][:20]:
            element["stats"]["minutes"] = 0
        store.record_actuals(1, payload)
        result = score_gameweek(store, 1)
        assert result.players == 40
        assert result.played == 20

    def test_too_few_players_scores_nothing(self, store):
        store.record_predictions([row(1, [4.0])], first_gameweek=1)
        store.record_actuals(1, live({1: 4}))
        assert score_gameweek(store, 1) is None


class TestSummary:
    def test_empty_summary_explains_itself_rather_than_showing_zeros(self):
        summary = summarise([])
        assert summary["gameweeks"] == 0
        assert "note" in summary
        assert "mean_absolute_error" not in summary

    def test_summarises_across_gameweeks(self, store):
        for gw in (1, 2):
            store.record_predictions([row(i, [4.0]) for i in range(1, 41)], first_gameweek=gw)
            store.record_actuals(gw, live({i: 4 for i in range(1, 41)}))
        summary = summarise(score_all(store))
        assert summary["gameweeks"] == 2
        assert summary["mean_absolute_error"] == pytest.approx(0.0)


class TestDurability:
    """Every machine this runs on is disposable, so the log has to outlive them."""

    def test_export_and_reimport_round_trip(self, store, tmp_path):
        store.record_predictions([row(i, [4.0, 5.0]) for i in range(1, 6)], first_gameweek=2)
        path = store.export_predictions(tmp_path / "record.csv")
        assert path.exists()

        fresh = Store(tmp_path / "fresh.sqlite")
        fresh.upsert_reference(
            [{"id": 1, "name": "Alpha", "short_name": "ALP"}],
            [{"id": i, "web_name": f"P{i}", "team": 1, "element_type": 3,
              "team_join_date": None} for i in range(1, 6)], {3: "MID"})
        assert fresh.import_predictions(path) == 10
        assert fresh.conn.execute("SELECT COUNT(*) FROM prediction").fetchone()[0] == 10
        fresh.close()

    def test_reimport_does_not_duplicate(self, store, tmp_path):
        store.record_predictions([row(1, [4.0])], first_gameweek=1)
        path = store.export_predictions(tmp_path / "record.csv")
        store.import_predictions(path)
        assert store.conn.execute("SELECT COUNT(*) FROM prediction").fetchone()[0] == 1

    def test_importing_a_missing_file_is_harmless(self, store, tmp_path):
        assert store.import_predictions(tmp_path / "absent.csv") == 0

    def test_actuals_round_trip(self, store, tmp_path):
        store.record_actuals(1, live({1: 7, 2: 3}))
        path = store.export_actuals(tmp_path / "actuals.csv")
        fresh = Store(tmp_path / "fresh2.sqlite")
        assert fresh.import_actuals(path) == 2
        fresh.close()


FAR_FUTURE = {gw: "2099-01-01T00:00:00+00:00" for gw in range(1, 39)}


class TestPruning:
    def test_collapses_to_the_last_prediction(self, store):
        import time
        for value in (1.0, 2.0, 3.0):
            store.record_predictions([row(i, [value]) for i in range(1, 4)], first_gameweek=1)
            time.sleep(1.05)
        store.prune_predictions(FAR_FUTURE)
        kept = {r[0] for r in store.conn.execute(
            "SELECT xp FROM prediction WHERE target_gameweek = 1")}
        assert kept == {3.0}, "pruning kept a superseded draft instead of the final one"

    def test_deletes_predictions_made_after_the_deadline(self, store):
        """A run between the deadline and the gameweek being marked finished would
        otherwise record a projection made knowing the team news. That is
        hindsight and must never be scored."""
        store.record_predictions([row(1, [4.0])], first_gameweek=1)
        store.conn.execute(
            "INSERT INTO prediction VALUES ('2030-01-01T00:00:00+00:00',1,1,0,99.0,1.0,80.0,5.0,'x')")
        store.conn.commit()
        store.prune_predictions({1: "2029-01-01T00:00:00+00:00"})
        kept = [r[0] for r in store.conn.execute("SELECT xp FROM prediction")]
        assert 99.0 not in kept
        assert kept == [4.0]

    def test_each_gameweek_keeps_its_own_prediction(self, store):
        store.record_predictions([row(1, [4.0, 5.0, 6.0])], first_gameweek=1)
        store.prune_predictions(FAR_FUTURE)
        assert store.conn.execute("SELECT COUNT(*) FROM prediction").fetchone()[0] == 3


class TestTheQueryContractWithReview:
    """`prediction_vs_actual` feeds both gaffer.score and gaffer.review.

    Adding player_id for the review broke two tests here that unpacked the row
    by arity, which is how a positional read fails: silently taking the wrong
    column, or loudly taking none. Both readers now go by name, and this holds
    that.
    """

    def test_rows_are_addressable_by_name(self, tmp_path):
        from gaffer.store import Store

        with Store(tmp_path / "t.db") as store:
            store.conn.execute("INSERT INTO team (id, name, short_name) "
                               "VALUES (1, 'Team', 'TEA')")
            store.conn.execute("INSERT INTO player (id, web_name, team_id, position) "
                               "VALUES (1, 'Player', 1, 'MID')")
            store.conn.execute(
                "INSERT INTO prediction (made_at, target_gameweek, player_id, "
                " horizon_index, xp, price) VALUES ('2026-01-01', 1, 1, 0, 4.5, 7.0)")
            store.conn.execute(
                "INSERT INTO actual (gameweek, player_id, points, minutes) "
                "VALUES (1, 1, 9, 90)")
            store.conn.commit()
            row = store.prediction_vs_actual(1)[0]

        assert row["xp"] == 4.5
        assert row["points"] == 9
        assert row["minutes"] == 90
        assert row["player_id"] == 1, "the review joins on this"
