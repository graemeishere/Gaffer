"""Settings loading.

`.env` holds the team and league IDs on a deployed box. It is read by the
package rather than only by systemd, so a run started by hand, by cron or by the
timer all see the same settings — the alternative is advice that silently
ignores your league depending on how it was launched.
"""
import importlib
import os

import pytest

from gaffer import config


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    def write(text: str):
        path = tmp_path / ".env"
        path.write_text(text)
        return path
    return write


class TestEnvFile:
    def test_reads_key_value_lines(self, env_file, monkeypatch):
        monkeypatch.delenv("GAFFER_ENTRY", raising=False)
        config._load_env_file(env_file("GAFFER_ENTRY=1234567\n"))
        assert os.environ["GAFFER_ENTRY"] == "1234567"
        monkeypatch.delenv("GAFFER_ENTRY", raising=False)

    def test_ignores_comments_and_blank_lines(self, env_file, monkeypatch):
        monkeypatch.delenv("GAFFER_LEAGUE", raising=False)
        config._load_env_file(env_file("# a note\n\nGAFFER_LEAGUE=42\n"))
        assert os.environ["GAFFER_LEAGUE"] == "42"
        monkeypatch.delenv("GAFFER_LEAGUE", raising=False)

    def test_strips_surrounding_quotes(self, env_file, monkeypatch):
        monkeypatch.delenv("GAFFER_ENTRY", raising=False)
        config._load_env_file(env_file('GAFFER_ENTRY="777"\n'))
        assert os.environ["GAFFER_ENTRY"] == "777"
        monkeypatch.delenv("GAFFER_ENTRY", raising=False)

    def test_a_real_environment_variable_wins(self, env_file, monkeypatch):
        """Otherwise a stale file would quietly override the command line."""
        monkeypatch.setenv("GAFFER_ENTRY", "999")
        config._load_env_file(env_file("GAFFER_ENTRY=1234567\n"))
        assert os.environ["GAFFER_ENTRY"] == "999"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        config._load_env_file(tmp_path / "absent.env")

    def test_a_malformed_line_is_skipped(self, env_file, monkeypatch):
        monkeypatch.delenv("GAFFER_ENTRY", raising=False)
        config._load_env_file(env_file("this line has no equals\nGAFFER_ENTRY=5\n"))
        assert os.environ["GAFFER_ENTRY"] == "5"
        monkeypatch.delenv("GAFFER_ENTRY", raising=False)


class TestEnvInt:
    def test_parses_a_number(self, monkeypatch):
        monkeypatch.setenv("GAFFER_TEST_ID", "1234567")
        assert config.env_int("GAFFER_TEST_ID") == 1234567

    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("GAFFER_TEST_ID", raising=False)
        assert config.env_int("GAFFER_TEST_ID") is None

    def test_nonsense_is_none_rather_than_a_crash(self, monkeypatch):
        """A typo in .env should not stop the engine running."""
        monkeypatch.setenv("GAFFER_TEST_ID", "my-team")
        assert config.env_int("GAFFER_TEST_ID") is None
