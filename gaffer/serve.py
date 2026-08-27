"""A small, locked-down endpoint for recording the team you have picked.

The board is a static file with no server in the request path, and this does
not change that: it is a separate, minimal service whose only job is to accept
one team and write it to the override file the run already reads. It never
serves the board, never touches anything else on disk, and never runs a shell.

The security posture is the whole point, because the URL is reachable from the
public internet:

- **Writing needs a bearer token.** A long random secret, set in the service's
  environment, compared in constant time. No token configured means writing is
  switched off entirely, not left open. Reading the current team is unguarded —
  it is already on the public board.
- **The body is bounded and parsed defensively**, and the team is validated
  against the real player list and FPL's own squad rules before it is written —
  fifteen real players, the right shape, a legal eleven — so the endpoint cannot
  be used to store anything but a fieldable team.
- **It binds to localhost.** The reverse proxy in front of the box forwards the
  one path here; the service itself is not exposed directly.

After a successful write it asks the engine to republish so the board reflects
the change without waiting for the next hourly run. That hook is injected, so a
test drives the whole request path without starting anything.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gaffer import config, overrides
from gaffer.ingest import FplClient
from gaffer.overrides import MyTeam

# A picked team is a few hundred bytes. Anything past this is not a team.
MAX_BODY = 64 * 1024


def _unauthorised(token: str, header: str | None) -> bool:
    """True when the request may not write. Constant-time, and closed by default.

    No configured token means writing is off, so every write is refused rather
    than waved through — the failure mode is 'nobody can', never 'anybody can'.
    """
    if not token:
        return True
    if not header or not header.startswith("Bearer "):
        return True
    return not hmac.compare_digest(header[len("Bearer "):], token)


def handle_request(method: str, path: str, headers: dict, body: bytes, *,
                   token: str, bootstrap: dict,
                   store=None, on_saved=lambda: None) -> tuple[int, dict]:
    """The whole endpoint as one pure function: (status, json-able body).

    Split out from the HTTP plumbing so the request path — auth, size, parsing,
    validation, the write — is tested without a socket. `store` defaults to the
    real override file; a test passes a temp path.
    """
    path = path.split("?", 1)[0].rstrip("/") or "/"

    if path == "/api/team" and method == "GET":
        team = overrides.load(store)
        return 200, {"team": team.as_dict() if team else None}

    if path == "/api/team" and method in ("POST", "PUT"):
        if _unauthorised(token, headers.get("authorization")):
            # 503 when the server has no token at all — the client cannot fix a
            # 401 that is really 'the server was never given a key'.
            return (503, {"ok": False, "reason": "writing is not configured on the server"}) \
                if not token else (401, {"ok": False, "reason": "not authorised"})
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "reason": "request too large"}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {"ok": False, "reason": "body is not valid JSON"}
        if data.get("clear"):
            overrides.clear(store)
            on_saved()
            return 200, {"ok": True, "reason": "cleared"}
        try:
            team = MyTeam(
                gameweek=int(data["gameweek"]),
                players=[int(p) for p in data["players"]],
                captain=int(data["captain"]),
                vice=int(data["vice"]),
                bench=[int(p) for p in data["bench"]],
            )
        except (KeyError, TypeError, ValueError):
            return 400, {"ok": False, "reason": "missing or malformed team fields"}
        ok, reason = overrides.validate(team, bootstrap)
        if not ok:
            return 422, {"ok": False, "reason": reason}
        overrides.save(team, store)
        on_saved()
        return 200, {"ok": True, "reason": "saved"}

    return 404, {"ok": False, "reason": "not found"}


def _republish() -> None:
    """Ask the engine to run again so the board reflects the new team.

    Best effort: systemd serialises the unit, so this cannot collide with the
    hourly timer, and if it is not permitted the next timed run picks the team
    up anyway. A failure here must never fail the write that already succeeded.
    """
    try:
        subprocess.run(["systemctl", "start", "--no-block", "gaffer.service"],
                       check=False, timeout=10)
    except Exception:
        pass


def make_handler(token: str, bootstrap: dict):
    class GafferHandler(BaseHTTPRequestHandler):
        server_version = "gaffer"

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = handle_request(method, self.path, headers, body,
                                             token=token, bootstrap=bootstrap,
                                             on_saved=_republish)
            self._reply(status, payload)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *args):
            pass   # no request logging; a personal endpoint, and it can be noisy

    return GafferHandler


def serve(port: int | None = None, host: str = "127.0.0.1") -> None:
    """Run the endpoint until interrupted. Binds to localhost by default so the
    reverse proxy, not the open internet, is what reaches it."""
    port = port or int(os.environ.get("GAFFER_WRITE_PORT") or 8081)
    token = os.environ.get("GAFFER_WRITE_TOKEN", "")
    bootstrap = FplClient().bootstrap()
    if not token:
        print("  ! GAFFER_WRITE_TOKEN is not set — the endpoint will refuse every "
              "write. Set it to enable recording a team.")
    handler = make_handler(token, bootstrap)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"  gaffer write endpoint on http://{host}:{port} "
          f"({'writing enabled' if token else 'read-only'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    serve()
