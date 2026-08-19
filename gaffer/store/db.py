"""SQLite store.

The API only ever shows you *now*. Backtesting needs a past, so every run
appends a snapshot of the mutable fields. Start collecting on day one — this
history cannot be reconstructed later.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gaffer import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS team (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    short_name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player (
    id              INTEGER PRIMARY KEY,
    web_name        TEXT NOT NULL,
    team_id         INTEGER NOT NULL REFERENCES team(id),
    position        TEXT NOT NULL,
    team_join_date  TEXT
);

-- One row per player per run. This is the history we will backtest against.
CREATE TABLE IF NOT EXISTS player_snapshot (
    taken_at        TEXT NOT NULL,
    gameweek        INTEGER,
    player_id       INTEGER NOT NULL REFERENCES player(id),
    now_cost        INTEGER NOT NULL,
    selected_by     REAL,
    total_points    INTEGER,
    minutes         INTEGER,
    form            REAL,
    status          TEXT,
    chance_playing  INTEGER,
    news            TEXT,
    PRIMARY KEY (taken_at, player_id)
);

CREATE TABLE IF NOT EXISTS fixture (
    id            INTEGER PRIMARY KEY,
    gameweek      INTEGER,
    kickoff       TEXT,
    team_h        INTEGER,
    team_a        INTEGER,
    team_h_diff   INTEGER,
    team_a_diff   INTEGER,
    finished      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_snapshot_player ON player_snapshot(player_id);
CREATE INDEX IF NOT EXISTS idx_fixture_gw ON fixture(gameweek);
"""


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or config.DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- writes ---------------------------------------------------------

    def upsert_reference(self, teams: list[dict], players: list[dict], positions: dict[int, str]) -> None:
        self.conn.executemany(
            "INSERT INTO team (id, name, short_name) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, short_name=excluded.short_name",
            [(t["id"], t["name"], t["short_name"]) for t in teams],
        )
        self.conn.executemany(
            "INSERT INTO player (id, web_name, team_id, position, team_join_date) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET web_name=excluded.web_name, team_id=excluded.team_id, "
            "position=excluded.position, team_join_date=excluded.team_join_date",
            [
                (p["id"], p["web_name"], p["team"], positions[p["element_type"]], p.get("team_join_date"))
                for p in players
            ],
        )
        self.conn.commit()

    def append_snapshot(self, players: list[dict], gameweek: int | None) -> str:
        taken_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR REPLACE INTO player_snapshot "
            "(taken_at, gameweek, player_id, now_cost, selected_by, total_points, minutes, "
            " form, status, chance_playing, news) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    taken_at, gameweek, p["id"], p["now_cost"],
                    _as_float(p.get("selected_by_percent")), p.get("total_points"), p.get("minutes"),
                    _as_float(p.get("form")), p.get("status"),
                    p.get("chance_of_playing_next_round"), p.get("news") or None,
                )
                for p in players
            ],
        )
        self.conn.commit()
        return taken_at

    def upsert_fixtures(self, fixtures: list[dict]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO fixture "
            "(id, gameweek, kickoff, team_h, team_a, team_h_diff, team_a_diff, finished) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f["id"], f.get("event"), f.get("kickoff_time"), f["team_h"], f["team_a"],
                    f.get("team_h_difficulty"), f.get("team_a_difficulty"), int(bool(f.get("finished"))),
                )
                for f in fixtures
            ],
        )
        self.conn.commit()

    # ---- reads ----------------------------------------------------------

    def snapshot_count(self) -> int:
        return self.conn.execute("SELECT COUNT(DISTINCT taken_at) FROM player_snapshot").fetchone()[0]

    def row_counts(self) -> dict[str, int]:
        return {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("team", "player", "player_snapshot", "fixture")
        }


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
