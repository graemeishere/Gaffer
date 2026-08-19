"""Team attack and defence ratings.

Every club gets two multipliers against a league average of 1.0: how many goals
it scores relative to an average side, and how many it concedes. A fixture's
expected goals is then attack x opponent defence x home advantage.

This replaces FPL's own fixture difficulty rating, which is a static, hand-set
integer from 1 to 5 — the crudest thing in the dataset, and the easiest edge to
take. Beating it is most of the value in this layer.

The awkward part is the start of a season, when there are no results to fit to.
Rather than pretend all clubs are equal, we build a prior from the squads
themselves: each club's players carry Opta expected-goals rates from last
season, so aggregating the current squad estimates the current squad's quality.
Players who changed club bring their numbers with them, which is exactly right
here — we want to know how good this squad is now, not how the club did before.

As results arrive, an iterative Poisson fit takes over. The blend is weighted by
matches played, so by roughly ten games the prior has faded out.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# How many matches of real results it takes for the fit to outweigh the prior.
PRIOR_MATCHES = 8

# Goals per team per game in a typical Premier League season, used to put the
# ratings on an interpretable scale.
LEAGUE_GOALS_PER_GAME = 1.45

_FIT_ITERATIONS = 40
_MIN_RATING = 0.35
_MAX_RATING = 2.60

# Newly promoted clubs have almost no Premier League minutes in their squad, so
# there is nothing to aggregate. Left alone they collapse to the clamp floor and
# the model decides they cannot score at all — which would both bury their
# players and inflate every opponent's clean sheet. Promoted sides are weaker
# than average, not hopeless, so we fall back to this instead.
PROMOTED_ATTACK = 0.80
PROMOTED_DEFENCE = 1.18

# How many players with real minutes it takes to trust a squad-derived rating.
SQUAD_EVIDENCE_PLAYERS = 6


@dataclass
class TeamStrength:
    attack: dict[int, float]
    defence: dict[int, float]
    home_advantage: float
    matches_fitted: int
    source: str  # "prior", "blended" or "fitted"

    def expected_goals(self, home_id: int, away_id: int) -> tuple[float, float]:
        """Expected goals for (home, away) in a fixture between these two."""
        base = LEAGUE_GOALS_PER_GAME
        home = base * self.attack[home_id] * self.defence[away_id] * self.home_advantage
        away = base * self.attack[away_id] * self.defence[home_id] / self.home_advantage
        return max(0.15, home), max(0.15, away)

    def difficulty(self, team_id: int, opponent_id: int, at_home: bool) -> float:
        """How hard this fixture is for `team_id`, on a 1 (easy) to 5 (hard)
        scale so it can be read next to FPL's own numbers."""
        if at_home:
            scored, conceded = self.expected_goals(team_id, opponent_id)
        else:
            conceded, scored = self.expected_goals(opponent_id, team_id)
        # More goals conceded and fewer scored means a harder afternoon.
        raw = (conceded - scored) / LEAGUE_GOALS_PER_GAME
        return max(1.0, min(5.0, 3.0 + raw * 1.6))


def _squad_prior(bootstrap: dict) -> tuple[dict[int, float], dict[int, float]]:
    """Estimate attack and defence from the players currently at each club.

    Squad-derived rather than club-derived on purpose: a player who moved this
    summer brings his numbers with him, which is what we want when the question
    is how good *this* squad is now.

    Clubs promoted from the Championship have no Premier League minutes to
    aggregate, so their estimate is blended toward a promoted-side prior in
    proportion to how little evidence there is.
    """
    attack_raw: dict[int, float] = {t["id"]: 0.0 for t in bootstrap["teams"]}
    defence_raw: dict[int, list[tuple[float, float]]] = {t["id"]: [] for t in bootstrap["teams"]}
    evidence: dict[int, int] = {t["id"]: 0 for t in bootstrap["teams"]}

    positions = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}

    for p in bootstrap["elements"]:
        minutes = p.get("minutes") or 0
        if minutes < 450:
            continue  # too little evidence to move a team rating
        weight = min(1.0, minutes / (38 * 90))
        team_id = p["team"]
        evidence[team_id] += 1

        # Attacking: sum expected goal involvement across the squad, weighted by
        # how much each player actually plays.
        xgi = _as_float(p.get("expected_goal_involvements_per_90"))
        attack_raw[team_id] += xgi * weight

        # Defensive: goalkeepers and defenders carry the expected-goals-conceded
        # signal for the side they played in.
        if positions[p["element_type"]] in ("GKP", "DEF"):
            xgc = _as_float(p.get("expected_goals_conceded_per_90"))
            if xgc > 0:
                defence_raw[team_id].append((xgc, weight))

    # Normalise only over clubs we actually have evidence for, so a promoted side
    # with nothing in it cannot drag the league average around.
    established = {t for t, n in evidence.items() if n >= SQUAD_EVIDENCE_PLAYERS}
    attack = _normalise(attack_raw, over=established)
    defence = _normalise(
        {
            t: (sum(x * w for x, w in rows) / sum(w for _, w in rows)) if rows else 0.0
            for t, rows in defence_raw.items()
        },
        over=established,
    )

    for team_id, count in evidence.items():
        w = min(1.0, count / (count + SQUAD_EVIDENCE_PLAYERS)) if count else 0.0
        attack[team_id] = attack[team_id] * w + PROMOTED_ATTACK * (1 - w)
        defence[team_id] = defence[team_id] * w + PROMOTED_DEFENCE * (1 - w)

    return attack, defence


def _normalise(values: dict[int, float], over: set[int] | None = None) -> dict[int, float]:
    """Scale so the league average is 1.0, then clamp the extremes.

    `over` restricts which clubs set the average — used to keep clubs we have no
    data for from distorting the baseline everyone else is measured against.
    """
    pool = [v for k, v in values.items() if v > 0 and (over is None or k in over)]
    mean = sum(pool) / len(pool) if pool else 1.0
    if mean <= 0:
        return {k: 1.0 for k in values}
    return {k: _clamp(v / mean) if v > 0 else 1.0 for k, v in values.items()}


def _results(fixtures: list[dict]) -> list[tuple[int, int, int, int]]:
    """Finished fixtures as (home, away, home goals, away goals)."""
    return [
        (f["team_h"], f["team_a"], f["team_h_score"], f["team_a_score"])
        for f in fixtures
        if f.get("finished") and f.get("team_h_score") is not None and f.get("team_a_score") is not None
    ]


def _fit_poisson(
    results: list[tuple[int, int, int, int]],
    team_ids: list[int],
    prior_attack: dict[int, float],
    prior_defence: dict[int, float],
) -> tuple[dict[int, float], dict[int, float], float]:
    """Iteratively solve for attack and defence ratings that reproduce the goals
    actually scored. This is the standard alternating fit for a Poisson goals
    model — it needs no optimiser library and converges in a few dozen passes.
    """
    attack = dict(prior_attack)
    defence = dict(prior_defence)

    total_home = sum(r[2] for r in results)
    total_away = sum(r[3] for r in results)
    home_advantage = math.sqrt(max(total_home, 1) / max(total_away, 1))

    for _ in range(_FIT_ITERATIONS):
        scored: dict[int, float] = {t: 0.0 for t in team_ids}
        attack_exposure: dict[int, float] = {t: 0.0 for t in team_ids}
        conceded: dict[int, float] = {t: 0.0 for t in team_ids}
        defence_exposure: dict[int, float] = {t: 0.0 for t in team_ids}

        for home, away, hg, ag in results:
            scored[home] += hg
            scored[away] += ag
            conceded[home] += ag
            conceded[away] += hg
            base = LEAGUE_GOALS_PER_GAME
            attack_exposure[home] += base * defence[away] * home_advantage
            attack_exposure[away] += base * defence[home] / home_advantage
            defence_exposure[away] += base * attack[home] * home_advantage
            defence_exposure[home] += base * attack[away] / home_advantage

        for t in team_ids:
            if attack_exposure[t] > 0:
                attack[t] = _clamp(scored[t] / attack_exposure[t])
            if defence_exposure[t] > 0:
                defence[t] = _clamp(conceded[t] / defence_exposure[t])

        attack = _rescale(attack)
        defence = _rescale(defence)

    return attack, defence, home_advantage


def _clamp(value: float) -> float:
    return max(_MIN_RATING, min(_MAX_RATING, value))


def _rescale(values: dict[int, float]) -> dict[int, float]:
    mean = sum(values.values()) / len(values)
    return {k: _clamp(v / mean) for k, v in values.items()} if mean > 0 else values


def fit(fixtures: list[dict], bootstrap: dict) -> TeamStrength:
    """Build team ratings from whatever evidence exists."""
    team_ids = [t["id"] for t in bootstrap["teams"]]
    prior_attack, prior_defence = _squad_prior(bootstrap)
    results = _results(fixtures)

    if not results:
        return TeamStrength(prior_attack, prior_defence, home_advantage=1.12,
                            matches_fitted=0, source="prior")

    fitted_attack, fitted_defence, home_advantage = _fit_poisson(
        results, team_ids, prior_attack, prior_defence
    )

    played: dict[int, int] = {t: 0 for t in team_ids}
    for home, away, _, _ in results:
        played[home] += 1
        played[away] += 1

    # Blend per team: a side that has played twice keeps most of its prior.
    attack, defence = {}, {}
    for t in team_ids:
        w = played[t] / (played[t] + PRIOR_MATCHES)
        attack[t] = fitted_attack[t] * w + prior_attack[t] * (1 - w)
        defence[t] = fitted_defence[t] * w + prior_defence[t] * (1 - w)

    mean_played = sum(played.values()) / len(played)
    source = "fitted" if mean_played >= PRIOR_MATCHES * 2 else "blended"
    return TeamStrength(_rescale(attack), _rescale(defence), home_advantage,
                        matches_fitted=len(results), source=source)


TeamStrength.fit = staticmethod(fit)


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
