"""The team you have entered by hand, before the API will show it.

Everything the override touches is safety-critical in one direction: a stored
file can come from a public endpoint, so it must be treated as a claim to be
checked, not as a team to be trusted. These tests pin the checks — a wrong
count, a made-up id, an illegal shape — and the round trip through disk.
"""
from __future__ import annotations

import json

import pytest

from gaffer import config, overrides
from gaffer.overrides import MyTeam


def _bootstrap():
    """Twenty players an override can be built from: 2 GKP, 5 DEF, 5 MID, 3 FWD
    across a couple of clubs, plus spares so a swap has somewhere to go."""
    types = [{"id": 1, "singular_name_short": "GKP"},
             {"id": 2, "singular_name_short": "DEF"},
             {"id": 3, "singular_name_short": "MID"},
             {"id": 4, "singular_name_short": "FWD"}]
    spec = [("GKP", 1, 3), ("DEF", 2, 8), ("MID", 3, 8), ("FWD", 4, 4)]
    elements, pid = [], 1
    for _name, etype, count in spec:
        for _ in range(count):
            # A distinct club each, so no squad trips the max-per-club rule
            # incidentally — the club check has its own test that forces it.
            elements.append({"id": pid, "element_type": etype, "team": pid})
            pid += 1
    return {"element_types": types, "elements": elements}


def _legal_team(gameweek=2):
    # ids by position from _bootstrap: GKP 1-3, DEF 4-11, MID 12-19, FWD 20-23.
    players = [1, 2,               # 2 GKP
               4, 5, 6, 7, 8,      # 5 DEF
               12, 13, 14, 15, 16, # 5 MID
               20, 21, 22]         # 3 FWD
    return MyTeam(gameweek=gameweek, players=players, captain=20, vice=12,
                  bench=[2, 8, 16, 22])   # a keeper, a defender, a mid, a forward


class TestValidation:
    def test_a_legal_team_passes(self):
        ok, reason = overrides.validate(_legal_team(), _bootstrap())
        assert ok, reason

    def test_the_wrong_number_of_players_is_rejected(self):
        team = _legal_team()
        team.players = team.players[:14]
        ok, reason = overrides.validate(team, _bootstrap())
        assert not ok and "15" in reason

    def test_a_made_up_player_is_rejected(self):
        team = _legal_team()
        team.players[0] = 999
        ok, reason = overrides.validate(team, _bootstrap())
        assert not ok and "unknown" in reason

    def test_a_broken_quota_is_rejected(self):
        # Swap a midfielder for a third keeper: 3 GKP, 4 MID — illegal.
        team = _legal_team()
        team.players = [1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 14, 15, 20, 21, 22]
        ok, reason = overrides.validate(team, _bootstrap())
        assert not ok and "GKP" in reason

    def test_four_from_one_club_is_rejected(self):
        boot = _bootstrap()
        for e in boot["elements"]:      # force everyone onto one club
            e["team"] = 1
        ok, reason = overrides.validate(_legal_team(), boot)
        assert not ok and "club" in reason

    def test_a_captain_off_the_squad_is_rejected(self):
        team = _legal_team()
        team.captain = 999
        ok, reason = overrides.validate(team, _bootstrap())
        assert not ok and "captain" in reason

    def test_captain_and_vice_cannot_be_the_same(self):
        team = _legal_team()
        team.vice = team.captain
        ok, reason = overrides.validate(team, _bootstrap())
        assert not ok and "same" in reason

    def test_an_illegal_formation_is_rejected(self):
        # Bench all three forwards, leaving nought up front: no legal shape.
        team = _legal_team()
        team.bench = [2, 20, 21, 22]    # a keeper and all three forwards
        team.captain, team.vice = 12, 13   # keep the armband on the pitch
        ok, reason = overrides.validate(team, _bootstrap())
        assert not ok and "formation" in reason.lower()


class TestRoundTrip:
    def test_save_then_load_is_the_same_team(self, tmp_path):
        path = tmp_path / "myteam.json"
        team = _legal_team()
        overrides.save(team, path)
        back = overrides.load(path)
        assert back == team

    def test_load_is_none_when_there_is_no_file(self, tmp_path):
        assert overrides.load(tmp_path / "nope.json") is None

    def test_a_corrupt_file_loads_as_none_not_an_error(self, tmp_path):
        path = tmp_path / "myteam.json"
        path.write_text("{ this is not json")
        assert overrides.load(path) is None

    def test_a_file_missing_a_field_loads_as_none(self, tmp_path):
        path = tmp_path / "myteam.json"
        path.write_text(json.dumps({"gameweek": 2, "players": [1, 2, 3]}))
        assert overrides.load(path) is None

    def test_save_is_atomic_leaving_no_temp_files(self, tmp_path):
        overrides.save(_legal_team(), tmp_path / "myteam.json")
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".myteam-")]
        assert leftovers == []

    def test_clear_removes_it(self, tmp_path):
        path = tmp_path / "myteam.json"
        overrides.save(_legal_team(), path)
        assert overrides.clear(path) is True
        assert overrides.load(path) is None
        assert overrides.clear(path) is False   # nothing left to remove


class TestShape:
    def test_starters_are_the_eleven_not_on_the_bench(self):
        team = _legal_team()
        assert len(team.starters) == 11
        assert set(team.starters).isdisjoint(team.bench)

    def test_as_actual_matches_the_manager_panel_shape(self):
        team = _legal_team()
        actual = overrides.as_actual(team, gameweek=2)
        assert actual["captain"] == team.captain
        assert actual["vice"] == team.vice
        assert set(actual["starters"]) == set(team.starters)
        assert actual["bench"] == team.bench
        assert actual["gameweek"] == 2
        assert actual["source"] == "manual"
