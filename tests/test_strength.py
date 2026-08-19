"""Team ratings, and the cold start that nearly broke them."""
from gaffer.model.strength import PROMOTED_ATTACK, TeamStrength


def test_falls_back_to_prior_with_no_results(bootstrap, fixtures):
    strength = TeamStrength.fit(fixtures, bootstrap)
    assert strength.source == "prior"
    assert strength.matches_fitted == 0


def test_promoted_side_is_weak_not_hopeless(bootstrap, fixtures):
    """A club with no Premier League minutes in its squad has nothing to
    aggregate. Left alone it collapses to the clamp floor and the model decides
    it cannot score at all, which buries its players and inflates every
    opponent's clean sheet."""
    bootstrap = {**bootstrap, "teams": bootstrap["teams"] + [
        {"id": 3, "name": "Promoted", "short_name": "PRO"}]}
    strength = TeamStrength.fit(fixtures, bootstrap)
    assert strength.attack[3] == PROMOTED_ATTACK
    assert strength.attack[3] > 0.6, "promoted side should not be at the clamp floor"
    assert strength.defence[3] > 1.0, "promoted side should be expected to concede more"


def test_home_side_expects_more_goals_than_away(bootstrap, fixtures):
    strength = TeamStrength.fit(fixtures, bootstrap)
    strength.attack = {1: 1.0, 2: 1.0}
    strength.defence = {1: 1.0, 2: 1.0}
    home, away = strength.expected_goals(1, 2)
    assert home > away, "home advantage is not being applied"


def test_expected_goals_are_never_zero(bootstrap, fixtures):
    strength = TeamStrength.fit(fixtures, bootstrap)
    strength.attack = {1: 0.01, 2: 0.01}
    strength.defence = {1: 0.01, 2: 0.01}
    home, away = strength.expected_goals(1, 2)
    assert home > 0 and away > 0


def test_difficulty_is_on_the_one_to_five_scale(bootstrap, fixtures):
    strength = TeamStrength.fit(fixtures, bootstrap)
    for at_home in (True, False):
        d = strength.difficulty(1, 2, at_home)
        assert 1.0 <= d <= 5.0


def test_fitting_uses_results_once_they_exist(bootstrap, fixtures):
    played = [{**fixtures[0], "finished": True, "team_h_score": 3, "team_a_score": 0}]
    strength = TeamStrength.fit(played, bootstrap)
    assert strength.matches_fitted == 1
    assert strength.source == "blended", "one result should not outweigh the prior"
