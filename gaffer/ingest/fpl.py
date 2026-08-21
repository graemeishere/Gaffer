"""Client for the public Fantasy Premier League API.

No key, no account. The API is undocumented and unsupported, so every call is
cached to disk and failures degrade to the last good copy rather than crashing
a run that happens to fall in a maintenance window.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from gaffer import config


class FplError(RuntimeError):
    """Raised when the API is unreachable and we have no cached copy to fall back on."""


class FplClient:
    def __init__(self, cache_dir: Path | None = None, ttl: int = config.CACHE_TTL):
        self.cache_dir = cache_dir or config.CACHE
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    # ---- plumbing -------------------------------------------------------

    def _cache_path(self, endpoint: str) -> Path:
        safe = endpoint.strip("/").replace("/", "_").replace("?", "_").replace("=", "_")
        return self.cache_dir / (safe + ".json")

    def get(self, endpoint: str, *, ttl: int | None = None) -> Any:
        """Fetch an endpoint, serving from cache when it is still fresh.

        On a network failure we fall back to a stale cache and say so, because a
        slightly old squad list beats no recommendation at all.
        """
        ttl = self.ttl if ttl is None else ttl
        path = self._cache_path(endpoint)

        if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
            return json.loads(path.read_text())

        # FPL wants a trailing slash on the path, which has to go before any
        # query string — appending it blindly yields "?page=1/" and a 400.
        route, _, query = endpoint.strip("/").partition("?")
        url = f"{config.API}/{route}/" + (f"?{query}" if query else "")
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            if path.exists():
                print(f"  ! {endpoint} unreachable ({exc.__class__.__name__}); using cached copy")
                return json.loads(path.read_text())
            raise FplError(f"{endpoint} unreachable and nothing cached: {exc}") from exc

        path.write_text(json.dumps(payload))
        return payload

    # ---- endpoints ------------------------------------------------------

    def bootstrap(self) -> dict:
        """Players, teams, gameweeks, prices, ownership, injury flags."""
        return self.get("bootstrap-static")

    def fixtures(self) -> list[dict]:
        """All 380 fixtures with per-side difficulty ratings."""
        return self.get("fixtures")

    def player_summary(self, player_id: int) -> dict:
        """One player's prior seasons, past gameweeks, and upcoming fixtures."""
        return self.get(f"element-summary/{player_id}")

    def event_live(self, gameweek: int) -> dict:
        """Every player's actual stats for one gameweek, in a single call.

        Empty until the gameweek starts, then filled as matches finish. This is
        what predictions get scored against, so it is never served from a stale
        cache once a gameweek is in progress.
        """
        return self.get(f"event/{gameweek}/live", ttl=600)

    def entry(self, entry_id: int) -> dict:
        """A manager's profile: bank, squad value, transfers made."""
        return self.get(f"entry/{entry_id}")

    def entry_history(self, entry_id: int) -> dict:
        """A manager's gameweek history and the chips they have played."""
        return self.get(f"entry/{entry_id}/history")

    def entry_picks(self, entry_id: int, gameweek: int) -> dict:
        """A manager's fifteen for a completed gameweek. 404s before the deadline."""
        return self.get(f"entry/{entry_id}/event/{gameweek}/picks")

    def league_standings(self, league_id: int) -> dict:
        """A classic league's table. Readable by ID without authentication —
        this is what lets us see every rival's squad in a mini-league."""
        return self.get(f"leagues-classic/{league_id}/standings")

    def league_h2h_standings(self, league_id: int) -> dict:
        """A head-to-head league's table.

        Separate endpoint, and the classic one 404s for an h2h league rather
        than saying so — which reads exactly like a league that does not exist.
        """
        return self.get(f"leagues-h2h/{league_id}/standings")

    def league_h2h_matches(self, league_id: int, page: int = 1) -> dict:
        """Who plays whom, gameweek by gameweek.

        In a head-to-head league this is the thing that matters: each week you
        are drawn against one manager, and beating them by a point counts the
        same as beating them by fifty.
        """
        return self.get(f"leagues-h2h-matches/league/{league_id}?page={page}")


def backfill_history(client: "FplClient", store, player_ids: list[int], *,
                     limit: int = 0, pause: float = 0.12,
                     log=lambda _msg: None) -> int:
    """Record each player's completed seasons, for players we have none for.

    A finished season's totals never change, so this runs once per player and
    then never again — the cost is one slow first run at the start of a season,
    not an ongoing tax on every wake-up.

    It exists because `bootstrap-static` cannot be trusted as a season store:
    its `minutes` and `starts` carry last season right up to the rollover and
    are then zeroed, taking the model's whole evidence base with them.
    Failures are per-player and non-fatal — a missing history costs one player
    his prior form, while aborting the run would cost the board entirely.
    """
    missing = store.players_missing_history(player_ids)
    if limit:
        missing = missing[:limit]
    if not missing:
        return 0

    log(f"  backfilling last season for {len(missing)} player(s) …")
    done = 0
    for index, pid in enumerate(missing):
        try:
            summary = client.player_summary(pid)
        except Exception:
            continue
        if store.record_history(pid, summary.get("history_past") or []):
            done += 1
        # Be a good citizen on an API that owes us nothing.
        if pause and index + 1 < len(missing):
            time.sleep(pause)
    log(f"    stored history for {done} player(s)")
    return done
