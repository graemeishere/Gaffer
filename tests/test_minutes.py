"""Minutes drive everything — a brilliant player who starts on the bench scores nothing."""
import pytest

from gaffer.model.minutes import estimate


def test_regular_starter_expects_most_of_the_match(player_factory):
    m = estimate(player_factory(minutes=3000, starts=34))
    assert m.expected_minutes > 70
    assert m.p_60 > 0.7


def test_bit_part_player_expects_little(player_factory):
    m = estimate(player_factory(minutes=317, starts=1))
    assert m.expected_minutes < 25
    assert m.p_60 < 0.2


def test_regular_substitute_is_not_zeroed(player_factory):
    """Never starts but always comes on — still worth points, still worth modelling."""
    m = estimate(player_factory(minutes=1140, starts=0))
    assert m.expected_minutes > 10
    assert m.p_appear > 0.4


def test_injury_flag_zeroes_the_projection(player_factory):
    m = estimate(player_factory(minutes=3000, starts=34, status="i"))
    assert m.expected_minutes == 0
    assert m.p_60 == 0


def test_explicit_chance_of_playing_scales_everything(player_factory):
    full = estimate(player_factory(minutes=3000, starts=34))
    doubt = estimate(player_factory(minutes=3000, starts=34,
                                    status="d", chance_of_playing_next_round=25))
    assert doubt.expected_minutes == pytest.approx(full.expected_minutes * 0.25, rel=0.02)


def test_news_is_carried_through(player_factory):
    m = estimate(player_factory(status="d", chance_of_playing_next_round=50,
                                news="Knock - 50% chance of playing"))
    assert "Knock" in m.note
