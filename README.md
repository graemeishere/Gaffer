# Gaffer

A Fantasy Premier League squad engine. It reads the public FPL API, projects
points, and — from Phase 2 — picks the squad, the eleven, the captain and the
transfers that maximise them.

No AI API, no inference costs, no keys. Fantasy football is a constrained
optimisation problem, not a language problem: the engine is a points model
feeding an integer solver.

## Status

**Phase 0 — done.** Fetch, cache, store, and a heuristic ranking board.
The ranking is *not* an expected-points model; it is last season's scoring rate,
shrunk toward a positional baseline, scaled by expected playing time and nudged
by fixture difficulty. It exists to prove the pipeline end to end.

| Phase | What it adds | State |
|---|---|---|
| 0 | Ingest, cache, SQLite history, JSON contract, ranking board | done |
| 1 | Minutes model, team strength ratings, real expected points | next |
| 2 | MILP optimiser — squad, eleven, captain, transfer path | |
| 3 | Backtest harness against last season | |
| 4 | Chip timing, deadline-aware scheduling, mini-league strategy | |

## Run it

```bash
pip install -e ".[dev]"
python -m gaffer.run --top 20
```

Writes two files:

- `data/latest.json` — the contract. The only thing anything downstream reads.
- `data/report.html` — the same run as a standalone page, no server needed.

Options: `--horizon N` (gameweeks to look ahead, default 6), `--refresh`
(ignore the cache), `--top N` (print a table), `--quiet`.

## Layout

```
gaffer/
  ingest/    FPL API client, disk cache, graceful degradation
  store/     SQLite — appends a snapshot per run, for backtesting
  rank/      Phase 0 heuristic ranking
  publish/   writes latest.json and report.html
web/         static page that reads latest.json
tests/       ranking maths
```

The engine and the website are deliberately strangers. The engine's only job is
to write the JSON; the page's only job is to read it. That keeps hosting
swappable and lets the model be developed against the same file the server
produces.

## Deploying

The engine runs to completion and exits — it is not a server, so there is
nothing to keep alive. On a VPS:

```bash
# hourly; the engine decides whether work is due
0 * * * * cd /srv/gaffer && .venv/bin/python -m gaffer.run --quiet
```

Point nginx at `web/` and copy `data/latest.json` beside `index.html`.
GitHub Actions runs the same thing twice a day and commits the output, so the
current board is always visible without a server.

## Notes on the data

- The FPL API is undocumented and unsupported. Every call is cached; failures
  fall back to the last good copy rather than crashing the run.
- Pre-season, `bootstrap-static` carries **last** season's totals against each
  player's **new** club. 102 players changed club this summer, so the engine
  flags them rather than quietly crediting old output to a new team.
- Per-90 rates on small minutes are noise. Rates are shrunk toward a positional
  baseline, and projections are scaled by expected playing time — without both,
  the top of the table fills with substitutes who scored twice in four cameos.
