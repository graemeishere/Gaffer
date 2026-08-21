# Gaffer

A Fantasy Premier League squad engine. It reads the public FPL API, projects
points, and — from Phase 2 — picks the squad, the eleven, the captain and the
transfers that maximise them. From Phase 5 it also plays Last Man Standing off
the same fixture list.

No AI API, no inference costs, no keys. Fantasy football is a constrained
optimisation problem, not a language problem: the engine is a points model
feeding an integer solver.

## Status

**Phase 5 — done.** Last Man Standing, off the same fixture list and the same
team ratings. Expected goals become a distribution over scorelines, which becomes
a win probability; the season is then solved as an assignment of clubs to rounds
rather than a weekly pick, because the constraint that a club may be used once is
the entire game. Every candidate is priced by re-planning the season behind it,
and the route is reported against what most entries actually do — take the best
available team every week — so the planning has to earn its place.

**Phase 4 — done.** Chip timing, deadline-aware scheduling, and the mini-league
engine: it reads your rivals' actual squads, simulates the run against them
rather than averaging it, and says whether to be taking risk on or squeezing it
out. Given Phase 3's verdict, this is the part the whole argument now rests on.

**Phase 3 — the verdict is "not yet".** The backtest scores the model
against the obvious alternatives on completed seasons. On the three seasons
where its inputs actually exist, the model and "pick last season's highest
scorers" finish level: 4871 points against 4881. The model ranks players
slightly better (mean rank correlation 0.430 against 0.418) and still does not
build better squads.

Tuning appeared to fix that — a parameter sweep found settings worth +126 points.
Leave-one-season-out says otherwise: each fold chose different parameters and two
of three held-out margins were negative. That is noise, not signal, so nothing
was tuned. The sweep stays in the repo for when more seasons exist.

| Phase | What it adds | State |
|---|---|---|
| 0 | Ingest, cache, SQLite history, JSON contract, ranking board | done |
| 1 | Minutes model, team strength ratings, expected points | done |
| 2 | MILP optimiser — squad, eleven, captain, transfer path | done |
| 3 | Backtest harness, benchmarks and calibration | done |
| 4 | Chip timing, deadline-aware scheduling, mini-league strategy | done |
| 5 | Last Man Standing — match odds and the season-long route | done |

### How a projection is built

1. **Team strength** — every club gets an attack and a defence multiplier around
   a league average of 1.0, so a fixture's expected goals is attack x opponent
   defence x home advantage. This replaces FPL's own 1-to-5 difficulty rating,
   which is static and hand-set: the crudest number in the dataset and the
   easiest edge to take.
2. **Minutes** — the chance he appears, the chance he lasts an hour, and his
   expected minutes. The largest single source of error in any projection: a
   perfect talent model with a naive minutes model loses to the reverse.
3. **Points** — appearance, goals, assists, clean sheet, goals conceded, saves,
   defensive contribution, bonus and cards, each scaled by expected minutes and
   by how the fixture looks for his team. Each also carries a variance, so a
   blank-or-haul forward is distinguishable from a steady defender on the same
   average.
4. **Optimise** — an integer program picks the best *combination* of fifteen
   under £100.0m, 2/5/5/3 and three-per-club, choosing the eleven separately for
   every gameweek in the horizon so the squad is judged on how it can be used.

## Run it

```bash
pip install -e ".[dev]"
python -m gaffer.run --top 20
```

Writes three files:

- `data/latest.json` — the contract. The only thing anything downstream reads.
- `data/report.html` — the same run as a standalone page, no server needed.
- `data/lastman.html` — the Last Man Standing route, on a page of its own.

Options: `--horizon N` (gameweeks to look ahead, default 6), `--refresh`
(ignore the cache), `--top N` (print a table), `--quiet`, `--no-optimise`
(board only), `--no-lms` (skip the Last Man Standing route), `--entry
YOUR_TEAM_ID` to price transfers against your actual squad, and `--league
YOUR_LEAGUE_ID` to simulate yourself against your rivals' real squads.

## Layout

```
gaffer/
  ingest/    FPL API client, disk cache, graceful degradation
  store/     SQLite — appends a snapshot per run, for backtesting
  model/     scoring table, team strength, minutes, expected points
  rank/      assembles projections into the ranked board
  optimise/  squad selection, lineup and captain, transfer pricing
  backtest/  historical dataset, benchmark strategies, calibration sweeps
  league/    rival squads, season simulation, effective ownership and stance
  lms/       match odds, the used list, and the season-long route
  schedule   what work is due, derived from the next deadline
  publish/   writes latest.json, report.html and lastman.html
web/         static pages: the sortable board, and what CI publishes
tests/       ranking maths
```

The engine and the website are deliberately strangers. The engine's only job is
to write the JSON; the page's only job is to read it. That keeps hosting
swappable and lets the model be developed against the same file the server
produces.

## Is it working *this* season?

```bash
python -m gaffer.score           # what it predicted vs what happened
python -m gaffer.score --json
```

Every run writes its projections to `record/predictions.csv` before the
gameweek, and results are pulled from the live endpoint after it. Scoring uses
the last projection made *before* the deadline — the one you could actually have
acted on; marking a later one would be showing the model the answers first.

`record/` is committed on purpose. The SQLite store is local and disposable, CI
runs in a fresh container each time, and a prediction that only exists on a
machine about to disappear is the same as no prediction. Finished gameweeks are
pruned to the single projection that gets scored, which keeps the log at a few
thousand rows a season rather than a million.

This is the measurement that matters. The historical backtest below could only
reach half the model, because past team assignments are not exposed and fixtures
never entered it. This half is scored on real fixtures, real injuries and real
rotation, and it is the only thing that can settle whether the engine beats
intuition.

## Does it work historically?

```bash
python -m gaffer.backtest            # score the model against naive strategies
python -m gaffer.backtest --json     # same, as JSON
```

Read the caveats before the numbers. The backtest can only test the half of the
model that history supports:

- **It cannot test fixtures.** Which club a player turned out for in a past
  season is not exposed by the API, so historical projections run against a
  neutral opponent. The team-strength layer — the part that replaces FPL's crude
  difficulty rating, and the most likely source of an edge — is untested. The
  verdict is therefore "unproven", not "disproven".
- **It cannot test the things the season actually turns on:** captaincy timing,
  transfer paths, chip weeks, or differentiating against a specific mini-league.
- **Survivor bias.** Only players still in the game have fetchable histories, so
  anyone who dropped out of the Premier League is invisible.
- **Seasons before 2022/23 are excluded automatically** because FPL carries no
  expected-goals data for them. Testing there scores a model with its inputs
  removed, loses to everything, and means nothing — an earlier run of this
  harness did exactly that and made the model look far worse than it is.

## Playing a mini-league

National ownership is close to irrelevant when a handful of colleagues decide
your table. Only the *difference* between your squad and theirs moves you —
points scored on a player everybody owns move the whole table together.

Classic and head-to-head leagues are detected automatically, because they need
opposite advice. A classic league accumulates points all season, so every extra
point counts and the aim is to outscore the field. A head-to-head league draws
you against one manager each week for three league points, and **margin pays
nothing** — beating them by one is beating them by fifty. There, a player you
both own cannot affect the result at all, and risk belongs to the underdog: a
predictable gameweek loses a match you were expected to lose, while variance is
the only thing that can rob you of one you should win.

```bash
python -m gaffer.run --entry YOUR_TEAM_ID --league YOUR_LEAGUE_ID
```

On a deployed box put them in `.env` beside the checkout instead, and every run
picks them up however it was started:

```
GAFFER_ENTRY=1234567
GAFFER_LEAGUE=987654
```

Rival squads are public once a deadline has passed, so the engine reads all
fourteen, simulates the coming gameweeks by drawing outcomes rather than
averaging them, and reports how often you finish top. Then it takes a stance:
ahead, match the field and kill variance; behind and late, take players they do
not own, because playing the percentages from behind loses slowly and losing
slowly is still losing. People reliably get this backwards.

**Read the win probability for what it is.** Every squad in the simulation is
scored with the same projections that chose yours, so it answers "if this model
is right, how often do I finish top" — not "is this model right". Phase 3 is
unflattering about the second question. Treat the number as a comparison of
squad *shapes* under a shared assumption, never as a forecast of the table.

## Last man standing

Same fixture list, different competition. Back one club a week; if it wins you go
through, and you may never use the same club twice.

```bash
python -m gaffer.lms
```

The engine already has what this needs. Team strength gives every fixture a pair
of expected-goals rates, a pair of rates is a distribution over scorelines, and a
distribution over scorelines is a win probability. Nothing about players,
prices or points enters it, which is why it is a separate command that happens to
share a repository — and why it also runs as part of `gaffer.run`, since it costs
one solve on data already fetched.

It gets **a page of its own**, `lastman.html`, rather than a section on the
fantasy board. It is a different competition, read by people who may not play the
fantasy game at all, and putting it under a squad they do not have made it look
like an appendix to something else. The board keeps a one-line pointer; the page
carries the argument. Both are written from the same `lms` block in
`latest.json`, which stays the contract.

**It is a route, not a weekly pick.** This is the whole thing. Asked to choose a
team for Saturday, almost everyone takes the shortest-priced home favourite
available, which is locally right and globally wrong: it spends the best clubs on
weeks where the second-best club would have survived anyway, and leaves you in
November holding nothing but the sides you were avoiding in August. The right
question is which *assignment* of clubs to rounds maximises the chance of
surviving all of them — one club per round, each club once — and that is an
assignment problem the same integer solver already in the repo does in
milliseconds.

So every candidate is priced by re-planning the entire season behind it, exactly
as transfers are priced by re-optimising the whole squad. The number to read is
the cost: the share of the run you give up by taking that club now rather than
the one the plan wants.

```
  club              fixture                win  draw  survive   route    cost  field  holds for
  ------------------------------------------------------------------------------------------------
  Arsenal           v Coventry City        72%   20%      72%    1.8%     -0%    36%
  Man City          v Bournemouth          70%   18%      70%    1.6%     -9%    26%  GW5
  Chelsea           at Fulham              53%   26%      53%    1.5%    -13%     4%  GW6
```

**A draw is a defeat**, so the draw probability is not a rounding error — it is
half the reason favourites go out. Independent Poisson is known to under-count
draws, especially 0-0 and 1-1, because goals in a real match are not independent.
The Dixon-Coles correction reweights the four lowest scorelines to fix it;
without it every recommendation here would read as safer than it is — a 2.4 v 0.7
mismatch draws 17.9% of the time with the correction against 15.9% without.
That fixture is not a formality either way: 74% to win, which is three-to-one,
and a quarter of the time you are out. Treating it as a certainty is the most
common way out of a pool.

**Read the survival numbers honestly.** Surviving eight straight rounds at 60-70%
a time is a couple of percent, and the engine prints that rather than hiding it.
The margin that matters is not the absolute number but the one against the naive
alternative, which is reported on every run: how much more often the planned
route survives than taking the best available team every week.

### Your pool's rules, and your used list

No two pools agree on the rules, and nothing about a pool is public — it is a
spreadsheet in somebody's inbox. Both have to be told to the engine.

```bash
python -m gaffer.lms --pick Arsenal            # record this round's pick
python -m gaffer.lms --used "Arsenal,Man City" # or just name what is spent
python -m gaffer.lms --draw-survives --lives 2 # pools vary; these change the pick
python -m gaffer.lms --forget Arsenal          # correct the record
python -m gaffer.lms --json                    # the same, as JSON
```

Recorded picks live in `record/lms.json`, committed for the same reason the
prediction log is: the machines this runs on are disposable. Results are never
entered by hand — a pick is a club and a round, the fixture list says what that
club did, so the engine settles its own record and can tell you that you are out
rather than planning a route for someone eliminated on Saturday.

Rules can go in `gaffer.env` or `.env` instead, so every run picks them up:

```
GAFFER_LMS_DRAW_SURVIVES=false
GAFFER_LMS_LIVES=1
GAFFER_LMS_HORIZON=8
```

### The part that is modelled rather than measured

The pool pays one person, so who else is on your club matters. Backing the
crowd's pick means surviving together or going out together, and a round that
eliminates everybody is usually void and replayed — which makes the crowd pick
far safer than its price early on, and close to worthless at the end, when what
you need is the field going out without you.

Nothing publishes pool picks, so the `field` column is a **model of the crowd**,
not a measurement: a softmax over win probability, flattened to account for
everyone else having their own used list. It is labelled that way everywhere it
appears, and it is the one number here that should not be treated as evidence.


## Deploying

The engine runs to completion and exits — it is not a server, so there is
nothing to keep alive, no socket, and no restart. Deployment is a timer, a
virtualenv, and a directory of static files.

```bash
git clone git@github.com:graemeishere/Gaffer.git
cd Gaffer
sudo bash deploy/setup.sh
```

The clone is over https and needs no credentials while the repository is public.
If it is private, `deploy/README.md` covers the options — and note that a host
blocking outbound port 22 will defeat any deploy key, so check that before
blaming the key.

Then `sudo bash deploy/setup.sh --serve` to publish the board over http.

The first command installs Python and the CBC solver, creates a service user,
and enables a systemd timer that fires hourly — hourly because the engine reads the next
deadline and decides what is due, and FPL deadlines land on four weekdays at six
clock times. See `deploy/README.md`, including how to have GitHub Actions deploy
over SSH instead of doing it by hand.

GitHub Actions also runs the engine twice a day and commits the board and the
prediction log, so the current picture stays visible with or without a VPS.

## Notes on the data

- The FPL API is undocumented and unsupported. Every call is cached; failures
  fall back to the last good copy rather than crashing the run.
- Pre-season, `bootstrap-static` carries **last** season's totals against each
  player's **new** club. 102 players changed club this summer, so the engine
  flags them rather than quietly crediting old output to a new team.
- Per-90 rates on small minutes are noise. Projections are scaled by expected
  playing time, without which the top of the table fills with substitutes who
  scored twice in four cameos.
- Newly promoted clubs have almost no Premier League minutes to aggregate. Left
  alone their ratings collapse to the clamp floor, which both buries their
  players and inflates every opponent's clean sheet, so they fall back to a
  promoted-side prior until results arrive.
- The scoring table is not in the API and is transcribed by hand in
  `gaffer/model/scoring.py`. Goalkeeper goals are worth **10**, not the 6 they
  were historically.
- Fixtures are the only input Last Man Standing takes, and they move. A club with
  two fixtures in one round is settled on the first, because a pool names one
  match — treating a double gameweek as two chances to survive would invent a
  safety net the rules do not give you.
