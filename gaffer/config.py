"""Central settings. Everything tunable lives here rather than scattered through the code."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
DB_PATH = DATA / "gaffer.sqlite"
JSON_OUT = DATA / "latest.json"
HTML_OUT = DATA / "report.html"

API = "https://fantasy.premierleague.com/api"
USER_AGENT = "gaffer/0.1 (+https://github.com/graemeishere)"

# How many gameweeks ahead we look when scoring fixtures.
HORIZON = 6

# Cache lifetime in seconds. Prices settle overnight, so an hour is plenty
# during the day and stops us hammering an API that owes us nothing.
CACHE_TTL = 3600

# A player needs this many minutes last season before he counts toward the
# positional baseline we shrink everyone else toward.
MIN_MINUTES_FOR_RATE = 900

# Shrinkage strength, in 90-minute appearances. A player's scoring rate is pulled
# toward his position's baseline as though he had already played this many games
# at exactly that rate. Small samples get pulled hard; a 34-start season barely
# moves. Without this the table fills with cameo merchants who scored twice off
# the bench and finished the season on 10 points per 90.
SHRINKAGE_APPEARANCES = 8

# A full season is 38 games of 90 minutes.
SEASON_GAMES = 38

# Players who joined their club on or after this date have prior-season stats
# that belong to a different team. We flag them rather than guess.
TRANSFER_WINDOW_START = "2026-06-01"

# Squad rules, read from the API at runtime but defaulted here.
SQUAD_SIZE = 15
BUDGET = 1000  # tenths of a million
MAX_PER_CLUB = 3
