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
        return self.cache_dir / (endpoint.strip("/").replace("/", "_") + ".json")

    def get(self, endpoint: str, *, ttl: int | None = None) -> Any:
        """Fetch an endpoint, serving from cache when it is still fresh.

        On a network failure we fall back to a stale cache and say so, because a
        slightly old squad list beats no recommendation at all.
        """
        ttl = self.ttl if ttl is None else ttl
        path = self._cache_path(endpoint)

        if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
            return json.loads(path.read_text())

        url = f"{config.API}/{endpoint.strip('/')}/"
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
