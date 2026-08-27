"""The write endpoint, driven through its pure request function.

No socket is opened: `handle_request` is the whole endpoint, so auth, size,
parsing, validation and the write are exercised directly. The security-critical
lines get their own tests — no token means closed, a wrong token is refused, an
illegal team is never written.
"""
from __future__ import annotations

import json

import pytest

from gaffer import serve
from gaffer.overrides import MyTeam, load


def _bootstrap():
    types = [{"id": 1, "singular_name_short": "GKP"},
             {"id": 2, "singular_name_short": "DEF"},
             {"id": 3, "singular_name_short": "MID"},
             {"id": 4, "singular_name_short": "FWD"}]
    spec = [("GKP", 1, 3), ("DEF", 2, 8), ("MID", 3, 8), ("FWD", 4, 4)]
    elements, pid = [], 1
    for _n, etype, count in spec:
        for _ in range(count):
            elements.append({"id": pid, "element_type": etype, "team": pid})
            pid += 1
    return {"element_types": types, "elements": elements}


def _team_body(gameweek=2):
    return json.dumps({
        "gameweek": gameweek,
        "players": [1, 2, 4, 5, 6, 7, 8, 12, 13, 14, 15, 16, 20, 21, 22],
        "captain": 20, "vice": 12, "bench": [2, 8, 16, 22],
    }).encode()


TOKEN = "s3cr3t-token-value"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def call(method, path, headers=None, body=b"", *, token=TOKEN, store):
    return serve.handle_request(method, path, headers or {}, body,
                                token=token, bootstrap=_bootstrap(), store=store)


class TestAuth:
    def test_writing_needs_a_token_configured_on_the_server(self, tmp_path):
        store = tmp_path / "myteam.json"
        status, payload = call("POST", "/api/team", AUTH, _team_body(),
                               token="", store=store)
        assert status == 503 and not payload["ok"]
        assert load(store) is None          # nothing written

    def test_a_missing_bearer_is_refused(self, tmp_path):
        store = tmp_path / "myteam.json"
        status, _ = call("POST", "/api/team", {}, _team_body(), store=store)
        assert status == 401
        assert load(store) is None

    def test_a_wrong_token_is_refused(self, tmp_path):
        store = tmp_path / "myteam.json"
        bad = {"authorization": "Bearer not-the-token"}
        status, _ = call("POST", "/api/team", bad, _team_body(), store=store)
        assert status == 401
        assert load(store) is None

    def test_reading_the_team_needs_no_token(self, tmp_path):
        store = tmp_path / "myteam.json"
        status, payload = call("GET", "/api/team", {}, store=store)
        assert status == 200 and payload["team"] is None


class TestWriting:
    def test_a_valid_team_is_saved(self, tmp_path):
        store = tmp_path / "myteam.json"
        status, payload = call("POST", "/api/team", AUTH, _team_body(), store=store)
        assert status == 200 and payload["ok"]
        saved = load(store)
        assert isinstance(saved, MyTeam) and saved.captain == 20

    def test_an_illegal_team_is_rejected_and_not_written(self, tmp_path):
        store = tmp_path / "myteam.json"
        body = json.loads(_team_body())
        body["players"] = body["players"][:14]        # only fourteen
        status, payload = call("POST", "/api/team", AUTH,
                               json.dumps(body).encode(), store=store)
        assert status == 422 and not payload["ok"]
        assert load(store) is None

    def test_malformed_json_is_a_clean_400(self, tmp_path):
        store = tmp_path / "myteam.json"
        status, payload = call("POST", "/api/team", AUTH, b"{ not json",
                               store=store)
        assert status == 400 and not payload["ok"]

    def test_missing_fields_are_a_clean_400(self, tmp_path):
        store = tmp_path / "myteam.json"
        status, _ = call("POST", "/api/team", AUTH,
                         json.dumps({"gameweek": 2}).encode(), store=store)
        assert status == 400

    def test_an_oversized_body_is_refused(self, tmp_path):
        store = tmp_path / "myteam.json"
        huge = b'{"x":"' + b"a" * (serve.MAX_BODY + 1) + b'"}'
        status, _ = call("POST", "/api/team", AUTH, huge, store=store)
        assert status == 413
        assert load(store) is None

    def test_clear_removes_a_saved_team(self, tmp_path):
        store = tmp_path / "myteam.json"
        call("POST", "/api/team", AUTH, _team_body(), store=store)
        status, payload = call("POST", "/api/team", AUTH,
                               json.dumps({"clear": True}).encode(), store=store)
        assert status == 200 and payload["ok"]
        assert load(store) is None

    def test_the_saved_team_reads_back_over_get(self, tmp_path):
        store = tmp_path / "myteam.json"
        call("POST", "/api/team", AUTH, _team_body(), store=store)
        status, payload = call("GET", "/api/team", {}, store=store)
        assert status == 200 and payload["team"]["captain"] == 20

    def test_the_republish_hook_fires_only_on_a_successful_write(self, tmp_path):
        store = tmp_path / "myteam.json"
        fired = []
        # good write fires it
        serve.handle_request("POST", "/api/team", AUTH, _team_body(),
                             token=TOKEN, bootstrap=_bootstrap(), store=store,
                             on_saved=lambda: fired.append(True))
        # rejected write does not
        body = json.loads(_team_body()); body["captain"] = 999
        serve.handle_request("POST", "/api/team", AUTH, json.dumps(body).encode(),
                             token=TOKEN, bootstrap=_bootstrap(), store=store,
                             on_saved=lambda: fired.append(True))
        assert fired == [True]


class TestRouting:
    def test_an_unknown_path_is_404(self, tmp_path):
        status, _ = call("GET", "/api/something-else", {}, store=tmp_path / "m.json")
        assert status == 404

    def test_a_trailing_slash_is_the_same_route(self, tmp_path):
        status, _ = call("GET", "/api/team/", {}, store=tmp_path / "m.json")
        assert status == 200
