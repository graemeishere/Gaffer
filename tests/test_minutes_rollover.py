"""The season rollover, which emptied the model's evidence base in silence.

`bootstrap-static` is not a season store. Through pre-season every per-player
field carries last season's record — minutes, starts, expected goals per 90,
bps — and the moment the new season starts they are all reset to zero. The
model read those fields directly, so on the morning of GW1 it lost everything
it knew at once: the best projection in the game fell from 38.0 points to 3.8,
every player collapsed onto the same floor, and the captaincy became an
arbitrary tie-break that landed on a goalkeeper.

Nothing failed. The suite passed, the run exited 0, the page rendered. These
tests exist so that cannot happen quietly a second time.
"""
from __future__ import annotations

import pytest

from gaffer import config
from gaffer.model.carryover import carryover_weight, effective_player
from gaffer.model.minutes import BASE_START_RATE, SEASON_GAMES, estimate

# An ever-present defender's last season, shaped like FPL's `history_past`.
EVER_PRESENT = {"minutes": 3420, "starts": 38, "goals": 3, "assists": 4,
                "clean_sheets": 14, "saves": 0, "bps": 700,
                "yellow_cards": 5, "red_cards": 0, "defensive_contribution": 190}
ROTATED = dict(EVER_PRESENT, minutes=1200, starts=12, bps=200)

# What the API actually returns on the morning of GW1.
ZEROED = {"minutes": 0, "starts": 0, "status": "a", "expected_goals_per_90": 0,
          "expected_assists_per_90": 0, "saves_per_90": 0, "bps": 0}


def minutes_for(history, games_played=0, player=None):
    return estimate(effective_player(player or dict(ZEROED), history, games_played))


class TestTheRollover:
    def test_a_zeroed_bootstrap_falls_back_to_last_season(self):
        """The exact regression. Before the fix this returned 5.03 minutes."""
        assert minutes_for(EVER_PRESENT).expected_minutes > 60, (
            "an ever-present defender must not read as a bit-part player just "
            "because the API cleared his totals overnight")

    def test_the_whole_league_does_not_collapse_to_one_number(self):
        """The collapse was only visible in the spread: every player alike."""
        gap = (minutes_for(EVER_PRESENT).expected_minutes
               - minutes_for(ROTATED).expected_minutes)
        assert gap > 15

    def test_it_clears_the_credibility_gate(self):
        assert minutes_for(EVER_PRESENT).expected_minutes >= config.MINIMUM_CREDIBLE_MINUTES

    def test_scoring_rates_survive_the_rollover_too(self):
        """Minutes alone were not enough. Every per-90 rate resets as well, so a
        player could be restored to 77 minutes and still project nothing."""
        out = effective_player(dict(ZEROED), EVER_PRESENT, games_played=0)
        assert out["expected_goals_per_90"] > 0
        assert out["expected_assists_per_90"] > 0
        assert out["bps"] > 0

    def test_the_rate_matches_last_seasons_output(self):
        """3 goals in 3420 minutes is 0.079 per 90, not 3 and not 0."""
        out = effective_player(dict(ZEROED), EVER_PRESENT, games_played=0)
        assert out["expected_goals_per_90"] == pytest.approx(3 / 3420 * 90, abs=0.005)

    def test_totals_and_minutes_move_together(self):
        """bps is consumed as bps-per-minute. Scaling one without the other
        would read a full season's bonus over ninety minutes of football."""
        out = effective_player(dict(ZEROED), EVER_PRESENT, games_played=0)
        assert out["bps"] / out["minutes"] == pytest.approx(700 / 3420, abs=1e-6)


class TestBlending:
    def test_with_no_games_played_last_season_carries_it(self):
        blended = minutes_for(EVER_PRESENT, games_played=0)
        full = estimate(dict(EVER_PRESENT, status="a"), games_played=SEASON_GAMES)
        assert blended.expected_minutes == pytest.approx(full.expected_minutes, abs=1.0)

    def test_a_full_season_of_evidence_overrides_last_year(self):
        """A player who lost his place is judged on this season, not last.

        Asserted as convergence rather than a threshold: by the end of a season
        the blend must land on what this season alone says, whatever number
        that is. A fixed pseudo-count prior never quite gets there.
        """
        blended = minutes_for(EVER_PRESENT, games_played=SEASON_GAMES)
        alone = estimate(dict(ZEROED), games_played=SEASON_GAMES)
        assert blended.expected_minutes == pytest.approx(alone.expected_minutes, abs=0.5)

    def test_the_handover_is_monotonic(self):
        """No cliff: evidence shifts across as games accumulate."""
        seen = [minutes_for(EVER_PRESENT, games_played=g).expected_minutes
                for g in (0, 2, 5, 10, 20, 38)]
        assert seen == sorted(seen, reverse=True), seen
        assert seen[0] - seen[-1] > 40

    def test_the_weight_starts_at_nothing_and_ends_at_everything(self):
        assert carryover_weight(0) == 0.0
        assert carryover_weight(SEASON_GAMES) == 1.0

    def test_a_player_with_no_prior_season_uses_the_base_rate(self):
        """A promotion or a new signing has no record. That must read as
        uncertainty, not as a confident zero."""
        got = minutes_for(None)
        assert 0 < got.expected_minutes < BASE_START_RATE * 90

    def test_a_prior_season_with_no_minutes_is_not_treated_as_evidence(self):
        """Registered but never played is not the same as never registered, and
        dividing by those zero minutes would be a crash."""
        got = minutes_for(dict(EVER_PRESENT, minutes=0, starts=0))
        assert got.expected_minutes > 0

    def test_injury_flags_still_bite_through_the_blend(self):
        """Last season's record must not resurrect a player who is out now.
        Availability describes today; last season has nothing to say about it."""
        fit = minutes_for(EVER_PRESENT)
        out = minutes_for(EVER_PRESENT, player=dict(ZEROED, status="i"))
        assert out.expected_minutes < fit.expected_minutes


class TestTheCredibilityGate:
    """Whatever empties the model next, the page must say so rather than
    publish an arbitrary tie-break as advice."""

    def test_the_threshold_rejects_the_collapse_we_saw(self):
        """The live board's best player was on 6.9 expected minutes."""
        assert 6.9 < config.MINIMUM_CREDIBLE_MINUTES

    def test_the_threshold_accepts_a_healthy_board(self):
        """The pre-rollover board peaked at 77.2 and must not be rejected."""
        assert 77.2 > config.MINIMUM_CREDIBLE_MINUTES
        assert minutes_for(EVER_PRESENT).expected_minutes > config.MINIMUM_CREDIBLE_MINUTES

    def test_the_reason_names_the_cause_not_the_symptom(self):
        """The old empty state blamed configuration for what was timing, and
        sent the reader looking in entirely the wrong place."""
        from gaffer.run import EVIDENCE_BROKEN_REASON

        assert "withheld" in EVIDENCE_BROKEN_REASON
        assert "new season" in EVIDENCE_BROKEN_REASON
        assert "as you picked it" in EVIDENCE_BROKEN_REASON


class TestThePageStatesItsBasis:
    """The banner used to assert "this season has not happened" forever. It
    went on saying that after the season started, while the model was in fact
    running on nothing at all."""

    def basis(self, games_played):
        from gaffer.publish.render import _basis_warning

        class S:
            source, matches_fitted = "fitted", 40
        return _basis_warning(S(), games_played)

    def test_before_a_ball_is_kicked_it_says_last_season(self):
        assert "last season" in self.basis(0)
        assert "hasn't kicked off" in self.basis(0)

    def test_early_season_admits_the_sample_is_thin(self):
        text = self.basis(2)
        assert "2 gameweeks" in text and "too early" in text

    def test_mid_season_says_it_is_a_blend(self):
        assert "blend" in self.basis(10)

    def test_late_season_drops_last_year_entirely(self):
        text = self.basis(30)
        assert "last season" not in text
        assert "30 gameweeks" in text

    def test_it_never_claims_the_season_has_not_happened_once_it_has(self):
        for played in (1, 5, 12, 25, 38):
            text = self.basis(played)
            assert "has not started" not in text
            assert "hasn't kicked off" not in text
