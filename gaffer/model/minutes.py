"""How much of a match do we expect this player to be on the pitch for?

This is the single largest source of error in any fantasy projection. A perfect
model of a player's talent paired with a naive minutes model loses to the
reverse — a brilliant forward who starts on the bench scores nothing, and no
amount of expected-goals precision fixes that.

Three numbers come out, and the points model needs all three: the chance he
appears at all, the chance he lasts an hour (which gates both the second
appearance point and clean-sheet points), and his expected minutes.
"""
from __future__ import annotations

from dataclasses import dataclass

# A typical start lasts most of the match; a substitute appearance is short.
MINUTES_PER_START = 82.0
MINUTES_PER_SUB = 18.0

# Share of starts that reach the hour mark.
START_SURVIVES_60 = 0.86

# Shrinkage for start rate, in games. A player with a handful of appearances is
# pulled toward the squad-wide base rate rather than trusted outright.
START_RATE_PRIOR_GAMES = 6.0
BASE_START_RATE = 0.45

SEASON_GAMES = 38


@dataclass
class MinutesModel:
    p_appear: float       # plays at all
    p_60: float           # reaches 60 minutes
    expected_minutes: float
    p_available: float    # fit and in the squad
    note: str = ""

    @property
    def share_of_match(self) -> float:
        return self.expected_minutes / 90.0


def _availability(player: dict) -> tuple[float, str]:
    """Fitness, straight from the flags FPL populates off club and press
    conference news. `chance_of_playing_next_round` is the club's own number."""
    status = player.get("status", "a")
    chance = player.get("chance_of_playing_next_round")
    news = (player.get("news") or "").strip()

    if chance is not None:
        return chance / 100.0, news
    if status == "a":
        return 1.0, news
    return {"d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}.get(status, 1.0), news


def estimate(player: dict, *, games_played: int = SEASON_GAMES) -> MinutesModel:
    """Build the minutes picture for one player.

    `games_played` is how many games the evidence covers. Callers hand this a
    record already expressed over a full season — see `gaffer.model.carryover`,
    which blends last season with this one before anything gets here, because
    the API zeroes these fields at the rollover and the model would otherwise
    have no evidence at all in August.
    """
    p_available, news = _availability(player)

    starts = float(player.get("starts") or 0)
    minutes = float(player.get("minutes") or 0)
    games = float(max(games_played, 1))

    # Start rate, shrunk toward the base rate by how much evidence we have.
    raw_start_rate = starts / games
    weight = games / (games + START_RATE_PRIOR_GAMES)
    start_rate = raw_start_rate * weight + BASE_START_RATE * (1 - weight)
    start_rate = max(0.0, min(1.0, start_rate))

    # Minutes not explained by starts came off the bench. This keeps regular
    # substitutes — who do score points — from being modelled as never playing.
    minutes_per_game = minutes / games
    sub_minutes = max(0.0, minutes_per_game - start_rate * MINUTES_PER_START)
    sub_rate = max(0.0, min(1.0 - start_rate, sub_minutes / MINUTES_PER_SUB))

    p_appear = p_available * min(1.0, start_rate + sub_rate)
    p_60 = p_available * start_rate * START_SURVIVES_60
    expected_minutes = p_available * (start_rate * MINUTES_PER_START + sub_rate * MINUTES_PER_SUB)

    return MinutesModel(
        p_appear=round(p_appear, 4),
        p_60=round(p_60, 4),
        expected_minutes=round(expected_minutes, 2),
        p_available=round(p_available, 3),
        note=news,
    )


MinutesModel.estimate = staticmethod(estimate)


# Eleven players, ninety minutes: a side gets exactly this many minutes in a
# fixture, whatever the model believes about who takes them. It is a rule of the
# game rather than a quantity to estimate.
TEAM_MINUTES_PER_FIXTURE = 11 * 90.0


def _scale(model: MinutesModel, factor: float) -> MinutesModel:
    """Move a player's whole minutes picture together.

    Scaling expected minutes alone would leave a promoted-club starter down for
    eighty minutes but still 6% likely to appear, and the points model reads
    both — appearance points off `p_appear`, clean sheets off `p_60`.
    """
    minutes = min(90.0, model.expected_minutes * factor)
    reach = minutes / model.expected_minutes if model.expected_minutes > 0 else 0.0
    p_appear = min(model.p_available, model.p_appear * reach)
    return MinutesModel(
        p_appear=round(p_appear, 4),
        p_60=round(min(p_appear, model.p_60 * reach), 4),
        expected_minutes=round(minutes, 2),
        p_available=model.p_available,
        note=model.note,
    )


def normalise_team(models: dict[int, MinutesModel],
                   fallback_weight: dict[int, float] | None = None,
                   target: float = TEAM_MINUTES_PER_FIXTURE) -> dict[int, MinutesModel]:
    """Scale one club's expected minutes so they add up to a real match.

    Modelling each player independently lets a club's total drift a long way
    from the 990 minutes it will actually play. In GW1 Tottenham came out at
    1004 and Hull at 118 — every Hull player who went the full ninety was
    projected at five minutes, because none of them had a Premier League record
    to be projected from. Somebody has to play those minutes.

    Redistribution, not invention: the total is fixed by the rules in advance,
    so this cannot be fitted to a result. Relative order is preserved, so the
    first-choice keeper stays ahead of his understudy — and where the model has
    no ordering at all, `fallback_weight` (price, in practice) stands in for the
    club's own opinion of who is first choice.

    Players who cannot play are excluded, so their share goes to team-mates
    rather than to someone already ruled out.
    """
    pool = {pid: m for pid, m in models.items() if m.p_available > 0}
    if not pool:
        return models

    out = dict(models)
    live = dict(pool)
    remaining = target

    # Water-filling: scale the pool, freeze anyone who hits a full match, and
    # share what is left among the rest. A club with two plausible players
    # cannot cover 990 minutes between them, so this terminates on the pool
    # emptying as well as on convergence.
    for _ in range(12):
        total = sum(m.expected_minutes for m in live.values())
        if total <= 0:
            # No signal whatsoever — a promoted squad before a ball is kicked.
            # Fall back to the club's own valuation of its players.
            weights = {pid: max(fallback_weight.get(pid, 0.0), 0.0) for pid in live} \
                if fallback_weight else {}
            if not sum(weights.values()):
                weights = {pid: 1.0 for pid in live}
            scale = remaining / sum(weights.values())
            for pid in live:
                share = min(90.0, weights[pid] * scale)
                base = models[pid]
                out[pid] = MinutesModel(
                    p_appear=round(min(base.p_available, share / 90.0), 4),
                    p_60=round(min(base.p_available, share / 90.0) * START_SURVIVES_60, 4),
                    expected_minutes=round(share, 2),
                    p_available=base.p_available,
                    note=base.note,
                )
            return out

        factor = remaining / total
        capped: list[int] = []
        for pid, model in live.items():
            scaled = _scale(model, factor)
            out[pid] = scaled
            if scaled.expected_minutes >= 90.0 - 1e-9:
                capped.append(pid)

        if not capped:
            return out
        for pid in capped:
            live.pop(pid)
            remaining -= 90.0
        if not live or remaining <= 0:
            return out
    return out
