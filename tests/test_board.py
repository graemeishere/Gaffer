"""The board is presentation — it must not invent or lose information."""
from gaffer.model.fixtures import team_fixture_runs
from gaffer.model.points import project
from gaffer.model.strength import TeamStrength
from gaffer.rank import build_board


def _board(bootstrap, fixtures, horizon=2):
    strength = TeamStrength.fit(fixtures, bootstrap)
    runs = team_fixture_runs(fixtures, horizon)
    return build_board(bootstrap, project(bootstrap, runs, strength), strength), strength


def test_sorted_by_projection(bootstrap, fixtures):
    rows, _ = _board(bootstrap, fixtures)
    assert rows == sorted(rows, key=lambda r: -r.projected)


def test_xp_array_matches_the_horizon(bootstrap, fixtures):
    rows, _ = _board(bootstrap, fixtures, horizon=2)
    assert all(len(r.xp) == 2 for r in rows)


def test_projected_is_the_sum_of_the_array(bootstrap, fixtures):
    rows, _ = _board(bootstrap, fixtures)
    for r in rows:
        assert abs(r.projected - sum(r.xp)) < 0.05


def test_movers_are_flagged(bootstrap, fixtures, player_factory):
    bootstrap = {**bootstrap, "elements": bootstrap["elements"] + [
        player_factory(id=99, web_name="Signing", team=1, element_type=3,
                       minutes=2500, starts=28, team_join_date="2026-07-15")]}
    rows, _ = _board(bootstrap, fixtures)
    signing = next(r for r in rows if r.name == "Signing")
    assert signing.moved_club
    assert "new club" in signing.note
    assert signing.confidence != "high", "a mover's record was earned elsewhere"


def test_value_is_points_per_million(bootstrap, fixtures):
    rows, _ = _board(bootstrap, fixtures)
    for r in rows:
        assert abs(r.per_million - r.projected / r.price) < 0.01
