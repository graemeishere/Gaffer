"""From expected goals to who actually wins.

Fantasy points and Last Man Standing ask different questions of the same model.
FPL wants a distribution over one player's contributions; LMS wants a single
number per fixture — the chance a club wins it — and cares about nothing else.
The team-strength layer already produces the input for both: attack x opponent
defence x home advantage gives each side an expected-goals rate, and a pair of
rates is a distribution over scorelines.

Two details matter more here than they do for fantasy points:

**Draws.** In most pools a draw eliminates you exactly as a defeat does, so the
draw probability is not a rounding error — it is half the reason favourites go
out. Independent Poisson is known to under-count draws, particularly 0-0 and
1-1, because goals in a real match are not independent: a side that scores first
changes how both teams play. The Dixon-Coles correction reweights the four
lowest scorelines to fix it, and without it this engine would quietly overstate
every recommendation it makes.

**Nothing else counts.** A 5-0 and a 1-0 are the same result. That sounds
obvious and it is the single most common mistake in an LMS pick: people back the
team they expect to *play* best rather than the team least likely to drop the
match, which is why away trips to stubborn defences keep knocking people out.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

# Scorelines above this contribute nothing worth summing.
MAX_GOALS = 10

# Dixon-Coles dependence parameter. Negative values push probability into 0-0
# and 1-1 and out of 1-0 and 0-1, which is the direction real results miss
# independent Poisson by. Around -0.13 is the usual fit for recent Premier
# League seasons; the correction is small but it lands entirely on the draw,
# which is the outcome this module exists to get right.
RHO = -0.13


@dataclass(frozen=True)
class MatchOdds:
    """One club's chances in one fixture."""

    gameweek: int
    team: int
    opponent: int
    home: bool
    win: float
    draw: float
    loss: float
    expected_for: float
    expected_against: float
    kickoff: str | None = None
    doubled: bool = False   # the club plays twice this round; this is the first

    def survival(self, draw_survives: bool = False) -> float:
        """The chance this pick keeps you in, under the pool's draw rule."""
        return self.win + (self.draw if draw_survives else 0.0)

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("win", "draw", "loss"):
            data[key] = round(data[key], 4)
        for key in ("expected_for", "expected_against"):
            data[key] = round(data[key], 2)
        return data


def _poisson(k: int, rate: float) -> float:
    return math.exp(-rate) * rate ** k / math.factorial(k)


def _tau(home_goals: int, away_goals: int, home_rate: float, away_rate: float,
         rho: float) -> float:
    """Dixon-Coles low-score correction.

    Only the four scorelines where both sides are on nought or one are touched;
    everywhere else independent Poisson is left alone.
    """
    if home_goals == 0 and away_goals == 0:
        return 1.0 - home_rate * away_rate * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + home_rate * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + away_rate * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def outcome_probabilities(home_rate: float, away_rate: float,
                          rho: float = RHO) -> tuple[float, float, float]:
    """(home win, draw, away win) for a fixture with these expected goals."""
    home_pmf = [_poisson(k, home_rate) for k in range(MAX_GOALS + 1)]
    away_pmf = [_poisson(k, away_rate) for k in range(MAX_GOALS + 1)]

    home_win = draw = away_win = total = 0.0
    for h, ph in enumerate(home_pmf):
        for a, pa in enumerate(away_pmf):
            # max() guards the correction against rates high enough to drive a
            # weight negative, which would produce a negative probability.
            p = ph * pa * max(0.0, _tau(h, a, home_rate, away_rate, rho))
            total += p
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p

    # The correction is not normalised by construction, and the matrix is
    # truncated, so renormalise rather than shipping three numbers that do not
    # sum to one.
    if total <= 0:
        return 0.0, 1.0, 0.0
    return home_win / total, draw / total, away_win / total


def fixture_odds(fixtures: list[dict], strength) -> dict[int, list[MatchOdds]]:
    """Every upcoming fixture as one row per club, grouped by gameweek.

    A club playing twice in a round appears once, on its first kickoff. Pools
    settle a round on a single match and the earlier one is the one they name,
    so treating a double gameweek as two chances to survive would invent a
    safety net the rules do not give you.
    """
    upcoming = sorted(
        (f for f in fixtures
         if f.get("event") and not f.get("finished") and f.get("team_h") and f.get("team_a")),
        key=lambda f: (f["event"], f.get("kickoff_time") or ""),
    )

    appearances: dict[tuple[int, int], int] = {}
    for f in upcoming:
        for team in (f["team_h"], f["team_a"]):
            key = (f["event"], team)
            appearances[key] = appearances.get(key, 0) + 1

    rounds: dict[int, list[MatchOdds]] = {}
    seen: set[tuple[int, int]] = set()
    for f in upcoming:
        gameweek = f["event"]
        home_rate, away_rate = strength.expected_goals(f["team_h"], f["team_a"])
        home_win, draw, away_win = outcome_probabilities(home_rate, away_rate)

        for team, opponent, is_home, win, loss, xg_for, xg_against in (
            (f["team_h"], f["team_a"], True, home_win, away_win, home_rate, away_rate),
            (f["team_a"], f["team_h"], False, away_win, home_win, away_rate, home_rate),
        ):
            key = (gameweek, team)
            if key in seen:
                continue
            seen.add(key)
            rounds.setdefault(gameweek, []).append(MatchOdds(
                gameweek=gameweek, team=team, opponent=opponent, home=is_home,
                win=win, draw=draw, loss=loss,
                expected_for=xg_for, expected_against=xg_against,
                kickoff=f.get("kickoff_time"),
                doubled=appearances[key] > 1,
            ))

    for gameweek, rows in rounds.items():
        rows.sort(key=lambda o: -o.win)
    return rounds
