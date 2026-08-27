"""Central settings. Everything tunable lives here rather than scattered through the code."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Read KEY=value lines into the environment, without adding a dependency.

    Real environment variables win, so anything set on the command line or by
    systemd overrides the file rather than being silently replaced by it.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


# Settings live beside the checkout so a run picks them up however it was
# started — by systemd, by cron, or by hand.
#
# Order matters: .env first, then the committed defaults. Since neither
# overwrites a value already present, this makes the per-machine file win over
# the shared one, and a real environment variable win over both.
_load_env_file(ROOT / ".env")
_load_env_file(ROOT / "gaffer.env")


def env_int(name: str) -> int | None:
    """An integer setting, or None when unset or not a number."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return None


def env_bool(name: str, default: bool = False) -> bool:
    """A yes/no setting, tolerant of how people actually write one."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on", "survives")


ENTRY_ID = env_int("GAFFER_ENTRY")
LEAGUE_ID = env_int("GAFFER_LEAGUE")
DATA = ROOT / "data"
CACHE = DATA / "cache"
DB_PATH = DATA / "gaffer.sqlite"
JSON_OUT = DATA / "latest.json"
# The prediction log is committed, unlike the SQLite file, because the machines
# this runs on are all disposable.
#
# On a deployed box it has to live OUTSIDE the checkout. Writing into a tracked
# file means every run leaves the working tree dirty, and the next update fails
# with "local changes would be overwritten" — hourly. GAFFER_STATE_DIR moves it
# somewhere git does not care about; unset, it stays in the repo, which is what
# CI wants.
STATE_DIR = Path(os.environ.get("GAFFER_STATE_DIR") or (ROOT / "record"))
PREDICTIONS_CSV = STATE_DIR / "predictions.csv"
ACTUALS_CSV = STATE_DIR / "actuals.csv"
# Last Man Standing keeps its own record beside them, for the same reason: no
# pool publishes anything, so the list of clubs you have spent exists only where
# you put it, and the planner is useless without it.
LMS_STATE = STATE_DIR / "lms.json"
# The team you have picked but the public API will not admit to until the
# deadline locks it. Recorded here so the board can show — and advise against —
# the side you are actually going to field, not the one it last saw. Same
# persistence reasoning: it must outlive a disposable database and a redeploy,
# so it sits in the state directory beside the other records, not in git.
MYTEAM_OVERRIDE = STATE_DIR / "myteam.json"

# Where the published copy is written. Same reasoning: on a deployed box it must
# not be inside the checkout.
PUBLISH_DIR = Path(os.environ.get("GAFFER_PUBLISH_DIR") or (ROOT / "web"))
HTML_OUT = DATA / "report.html"
# Last Man Standing gets a page rather than a section. It is a different
# competition, read by people who do not play the fantasy game at all, and
# burying it under a squad they do not have made it look like an appendix to
# something else.
LASTMAN_OUT = DATA / "lastman.html"

API = "https://fantasy.premierleague.com/api"
USER_AGENT = "gaffer/0.1 (+https://github.com/graemeishere)"

# How many gameweeks ahead we look when scoring fixtures.
HORIZON = 6

# Cache lifetime in seconds. Prices settle overnight, so an hour is plenty
# during the day and stops us hammering an API that owes us nothing.
CACHE_TTL = 3600

# A full season is 38 games of 90 minutes.
SEASON_GAMES = 38

# Below this, the board is not a close call — it is a broken evidence base.
# A real league always has someone expected to play most of a match; when the
# best-projected player in it is on a handful of minutes, every number has
# collapsed to the same floor and the "recommendation" is an arbitrary
# tie-break. Withhold advice rather than publish it.
MINIMUM_CREDIBLE_MINUTES = 45.0

# Players who joined their club on or after this date have prior-season stats
# that belong to a different team. We flag them rather than guess.
TRANSFER_WINDOW_START = "2026-06-01"

# Last Man Standing. Pools differ on all three of these, and getting them wrong
# does not produce slightly worse advice — it produces advice for another game.
LMS_HORIZON = env_int("GAFFER_LMS_HORIZON") or 8
LMS_LIVES = env_int("GAFFER_LMS_LIVES") or 1
LMS_DRAW_SURVIVES = env_bool("GAFFER_LMS_DRAW_SURVIVES")
# Clubs already spent, as a comma-separated list, for anyone who would rather
# set one variable than keep the record file.
LMS_USED = os.environ.get("GAFFER_LMS_USED", "")

# Squad rules, read from the API at runtime but defaulted here.
SQUAD_SIZE = 15
BUDGET = 1000  # tenths of a million
MAX_PER_CLUB = 3
