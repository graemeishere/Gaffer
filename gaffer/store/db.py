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
    starts          INTEGER,
    PRIMARY KEY (taken_at, player_id)
);

-- Last season's playing time, which is the only evidence the model has in
-- August. It has to live here because bootstrap-static does not keep it: those
-- `minutes` and `starts` fields carry last season's totals right up to the
-- rollover and are then zeroed, which silently emptied the model's entire
-- evidence base on the morning of GW1. A completed season's totals never
-- change, so this is written once and read forever.
CREATE TABLE IF NOT EXISTS player_history (
    player_id    INTEGER NOT NULL REFERENCES player(id),
    season_name  TEXT NOT NULL,
    minutes      INTEGER,
    starts       INTEGER,
    total_points INTEGER,
    goals        INTEGER,
    assists      INTEGER,
    clean_sheets INTEGER,
    goals_conceded INTEGER,
    saves        INTEGER,
    bps          INTEGER,
    yellow_cards INTEGER,
    red_cards    INTEGER,
    defensive_contribution INTEGER,
    PRIMARY KEY (player_id, season_name)
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

-- What the model said, before it could possibly know. Recorded every run so a
-- gameweek can be scored afterwards; once a deadline passes, what we thought
-- beforehand is unrecoverable, so this has to be written in advance or not at all.
CREATE TABLE IF NOT EXISTS prediction (
    made_at         TEXT NOT NULL,
    target_gameweek INTEGER NOT NULL,
    player_id       INTEGER NOT NULL REFERENCES player(id),
    horizon_index   INTEGER NOT NULL,   -- 0 is the next gameweek
    xp              REAL NOT NULL,
    variance        REAL,
    expected_minutes REAL,
    price           REAL,
    model_stage     TEXT,
    PRIMARY KEY (made_at, target_gameweek, player_id)
);

-- What actually happened, pulled from the live endpoint once a gameweek is done.
CREATE TABLE IF NOT EXISTS actual (
    gameweek    INTEGER NOT NULL,
    player_id   INTEGER NOT NULL REFERENCES player(id),
    points      INTEGER NOT NULL,
    minutes     INTEGER,
    goals       INTEGER,
    assists     INTEGER,
    clean_sheet INTEGER,
    bonus       INTEGER,
    PRIMARY KEY (gameweek, player_id)
);

CREATE INDEX IF NOT EXISTS idx_prediction_target ON prediction(target_gameweek);
CREATE INDEX IF NOT EXISTS idx_actual_gameweek ON actual(gameweek);
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
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns that CREATE TABLE IF NOT EXISTS will not add.

        A deployed box has a database from before the column existed, and
        executescript silently leaves it alone.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(player_snapshot)")}
        if "starts" not in have:
            self.conn.execute("ALTER TABLE player_snapshot ADD COLUMN starts INTEGER")

        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(player_history)")}
        for column in ("goals", "assists", "clean_sheets", "goals_conceded", "saves",
                       "bps", "yellow_cards", "red_cards", "defensive_contribution"):
            if column not in have:
                self.conn.execute(f"ALTER TABLE player_history ADD COLUMN {column} INTEGER")

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
            " form, status, chance_playing, news, starts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    taken_at, gameweek, p["id"], p["now_cost"],
                    _as_float(p.get("selected_by_percent")), p.get("total_points"), p.get("minutes"),
                    _as_float(p.get("form")), p.get("status"),
                    p.get("chance_of_playing_next_round"), p.get("news") or None,
                    p.get("starts"),
                )
                for p in players
            ],
        )
        self.conn.commit()
        return taken_at

    def record_history(self, player_id: int, seasons: list[dict]) -> int:
        """Store a player's completed seasons from `element-summary`."""
        rows = [
            (player_id, s["season_name"], s.get("minutes"), s.get("starts"),
             s.get("total_points"), s.get("goals_scored"), s.get("assists"),
             s.get("clean_sheets"), s.get("goals_conceded"), s.get("saves"),
             s.get("bps"), s.get("yellow_cards"), s.get("red_cards"),
             s.get("defensive_contribution"))
            for s in seasons if s.get("season_name")
        ]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO player_history "
            "(player_id, season_name, minutes, starts, total_points, goals, assists, "
            " clean_sheets, goals_conceded, saves, bps, yellow_cards, red_cards, "
            " defensive_contribution) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows)
        self.conn.commit()
        return len(rows)

    def latest_history(self) -> dict[int, dict]:
        """Each player's most recent completed season, by player id.

        Seasons sort correctly as strings — "2025/26" > "2024/25" — so the
        newest row per player is the one to carry into the new campaign.
        """
        out: dict[int, dict] = {}
        columns = ("minutes", "starts", "total_points", "goals", "assists",
                   "clean_sheets", "goals_conceded", "saves", "bps",
                   "yellow_cards", "red_cards", "defensive_contribution")
        for r in self.conn.execute(
                f"SELECT player_id, season_name, {', '.join(columns)} "
                "FROM player_history ORDER BY player_id, season_name"):
            row = {"season_name": r["season_name"]}
            row.update({c: r[c] or 0 for c in columns})
            out[r["player_id"]] = row
        return out

    def players_missing_history(self, player_ids: list[int]) -> list[int]:
        """Players whose stored history is absent or incomplete.

        Incomplete counts as missing. A row written before the scoring columns
        existed reads as all-zero output, which is indistinguishable from a
        player who genuinely never scored — so it has to be refetched rather
        than trusted. This is what lets a deployed box heal itself after a
        schema change instead of quietly projecting nobody to score.
        """
        known = {r["player_id"] for r in self.conn.execute(
            "SELECT player_id FROM player_history "
            "GROUP BY player_id HAVING SUM(goals IS NULL) = 0")}
        return [pid for pid in player_ids if pid not in known]

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

    def record_predictions(self, rows, first_gameweek: int, stage: str = "") -> str:
        """Write this run's projections, one row per player per gameweek ahead."""
        made_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = [
            (made_at, first_gameweek + index, row.id, index, xp,
             row.var[index] if index < len(row.var) else None,
             row.minutes, row.price, stage)
            for row in rows
            for index, xp in enumerate(row.xp)
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO prediction "
            "(made_at, target_gameweek, player_id, horizon_index, xp, variance, "
            " expected_minutes, price, model_stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        self.conn.commit()
        return made_at

    def record_actuals(self, gameweek: int, live: dict) -> int:
        """Store what players really scored in a completed gameweek."""
        rows = []
        for element in live.get("elements", []):
            stats = element.get("stats", {})
            rows.append((
                gameweek, element["id"], stats.get("total_points") or 0,
                stats.get("minutes") or 0, stats.get("goals_scored") or 0,
                stats.get("assists") or 0, stats.get("clean_sheets") or 0,
                stats.get("bonus") or 0,
            ))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO actual "
                "(gameweek, player_id, points, minutes, goals, assists, clean_sheet, bonus) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self.conn.commit()
        return len(rows)

    def prune_predictions(self, deadlines: dict[int, str]) -> int:
        """Keep one prediction per player per gameweek: the last valid one.

        Two jobs, and the first is correctness. A run that happens after a
        deadline but before the gameweek is marked finished would otherwise
        record a projection made with knowledge of the team news — hindsight
        that must never be scored. Anything stamped at or after its target
        gameweek's deadline is deleted outright.

        The second is size. Every run projects six gameweeks ahead, so the log
        grows by thousands of rows a day while only the final pre-deadline
        projection is ever used. Collapsing to that one keeps a season's record
        near twenty-five thousand rows instead of a million.
        """
        removed = 0
        for gameweek, deadline in deadlines.items():
            removed += self.conn.execute(
                "DELETE FROM prediction WHERE target_gameweek = ? AND made_at >= ?",
                (gameweek, deadline)).rowcount

        removed += self.conn.execute(
            "DELETE FROM prediction WHERE made_at NOT IN ("
            "  SELECT MAX(made_at) FROM prediction p2 "
            "  WHERE p2.target_gameweek = prediction.target_gameweek)").rowcount
        self.conn.commit()
        return removed

    def export_predictions(self, path: Path | None = None) -> Path:
        """Write the prediction log to CSV so it outlives the machine.

        The SQLite file is local and disposable — CI runs in a fresh container
        every time and this session's disk is reclaimed when it ends. A
        prediction that only exists on a machine that is about to disappear is
        the same as no prediction at all, so the log is exported to a committed
        text file and read back on the next run.
        """
        import csv

        path = path or (self.path.parent / "predictions.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(
            "SELECT made_at, target_gameweek, player_id, horizon_index, xp, variance, "
            "expected_minutes, price, model_stage FROM prediction "
            "ORDER BY made_at, target_gameweek, player_id")
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["made_at", "target_gameweek", "player_id", "horizon_index",
                             "xp", "variance", "expected_minutes", "price", "model_stage"])
            writer.writerows(tuple(r) for r in rows)
        return path

    def import_predictions(self, path: Path | None = None) -> int:
        """Load a previously exported log back in. Existing rows win."""
        import csv

        path = path or (self.path.parent / "predictions.csv")
        if not path.exists():
            return 0
        with path.open(newline="") as handle:
            rows = [
                (r["made_at"], int(r["target_gameweek"]), int(r["player_id"]),
                 int(r["horizon_index"]), float(r["xp"]),
                 float(r["variance"]) if r["variance"] else None,
                 float(r["expected_minutes"]) if r["expected_minutes"] else None,
                 float(r["price"]) if r["price"] else None, r["model_stage"])
                for r in csv.DictReader(handle)
            ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO prediction "
            "(made_at, target_gameweek, player_id, horizon_index, xp, variance, "
            " expected_minutes, price, model_stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows)
        self.conn.commit()
        return len(rows)

    def export_actuals(self, path: Path | None = None) -> Path:
        """Results, exported for the same reason as the predictions."""
        import csv

        path = path or (self.path.parent / "actuals.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(
            "SELECT gameweek, player_id, points, minutes, goals, assists, clean_sheet, bonus "
            "FROM actual ORDER BY gameweek, player_id")
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["gameweek", "player_id", "points", "minutes",
                             "goals", "assists", "clean_sheet", "bonus"])
            writer.writerows(tuple(r) for r in rows)
        return path

    def import_actuals(self, path: Path | None = None) -> int:
        import csv

        path = path or (self.path.parent / "actuals.csv")
        if not path.exists():
            return 0
        with path.open(newline="") as handle:
            rows = [
                (int(r["gameweek"]), int(r["player_id"]), int(r["points"]),
                 int(r["minutes"] or 0), int(r["goals"] or 0), int(r["assists"] or 0),
                 int(r["clean_sheet"] or 0), int(r["bonus"] or 0))
                for r in csv.DictReader(handle)
            ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO actual "
            "(gameweek, player_id, points, minutes, goals, assists, clean_sheet, bonus) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.conn.commit()
        return len(rows)

    def scored_gameweeks(self) -> list[int]:
        """Gameweeks where both a prediction and a result exist."""
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT a.gameweek FROM actual a "
            "JOIN prediction p ON p.target_gameweek = a.gameweek ORDER BY a.gameweek")]

    def prediction_vs_actual(self, gameweek: int, *, before: str | None = None) -> list[tuple]:
        """Paired (player, predicted, actual, minutes) for one gameweek.

        Scores the *last prediction made before the deadline* — the one you could
        actually have acted on. Taking a later one would be marking the model's
        homework after seeing the answers.
        """
        cutoff = before or "9999"
        return list(self.conn.execute(
            "SELECT pl.web_name, p.xp, a.points, a.minutes, p.price, pl.position "
            "FROM prediction p "
            "JOIN actual a ON a.gameweek = p.target_gameweek AND a.player_id = p.player_id "
            "JOIN player pl ON pl.id = p.player_id "
            "WHERE p.target_gameweek = ? AND p.made_at = ("
            "  SELECT MAX(made_at) FROM prediction "
            "  WHERE target_gameweek = ? AND made_at < ?)",
            (gameweek, gameweek, cutoff)))

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
