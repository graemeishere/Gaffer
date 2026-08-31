"""When to play the chips.

A chip is not a weekly decision. There are a fixed few and a whole season to
spend them in, so the real question is never "should I wildcard this week" but
"which single week between now and May is the best home for this". Answering the
weekly version greedily burns chips on the first above-average opportunity and
leaves nothing for the doubles later on.

So each chip is valued in every gameweek of the horizon and the best week is
reported alongside the value of playing it now. Holding is the default, and the
recommendation only flips when this week is close enough to the best week that
waiting is not worth the risk of never getting there.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from gaffer.optimise.lineup import best_lineup
from gaffer.optimise.squad import pick_squad
from gaffer.rank import PlayerRow

# Playing now requires this week to be the best week in view AND to stand out
# from the others by this many standard deviations. Requiring only "close to the
# best in view" is what makes a chip engine burn its wildcard in gameweek one:
# over a short window every week looks like every other week, so the best of them
# is barely better than the average of them, and greedy always fires.
PLAY_NOW_SIGMA = 1.0

# And by this much in relative terms. A sigma test alone is not enough: across a
# flat run of weeks the spread is tiny, so a trivial lead reads as many standard
# deviations and the chip fires on a 2% edge. A chip is worth one week of the
# season — it should only go when that week is properly better, not narrowly.
PLAY_NOW_MARGIN = 0.15


@dataclass
class ChipAdvice:
    chip: str
    action: str            # play | hold
    best_gameweek: int
    best_value: float
    value_now: float
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def _xp(rows_by_id: dict[int, PlayerRow], pid: int, gw: int) -> float:
    row = rows_by_id.get(pid)
    if not row or gw >= len(row.xp):
        return 0.0
    return row.xp[gw]


def triple_captain(squad: list[int], rows_by_id: dict[int, PlayerRow],
                   positions: dict[int, str], horizon: int) -> list[float]:
    """Worth one extra multiple of your captain, so it is worth exactly what your
    best player is worth that week."""
    values = []
    for gw in range(horizon):
        xp = {pid: _xp(rows_by_id, pid, gw) for pid in squad}
        lineup = best_lineup(squad, xp, positions)
        values.append(xp.get(lineup.captain, 0.0))
    return values


def bench_boost(squad: list[int], rows_by_id: dict[int, PlayerRow],
                positions: dict[int, str], horizon: int) -> list[float]:
    """Worth whatever your four substitutes would have scored."""
    values = []
    for gw in range(horizon):
        xp = {pid: _xp(rows_by_id, pid, gw) for pid in squad}
        lineup = best_lineup(squad, xp, positions)
        values.append(sum(xp.get(pid, 0.0) for pid in lineup.bench))
    return values


def free_hit(squad: list[int], rows: list[PlayerRow], rows_by_id: dict[int, PlayerRow],
             positions: dict[int, str], horizon: int, budget: float) -> list[float]:
    """One week with any squad you like, then everything reverts.

    Valued as the gap between the best eleven money could buy for that single
    week and the eleven you already have.
    """
    values = []
    for gw in range(horizon):
        single = [
            PlayerRow(**{**row.__dict__,
                         "xp": [row.xp[gw]] if gw < len(row.xp) else [0.0],
                         "var": [row.var[gw]] if gw < len(row.var) else [0.0],
                         "projected": row.xp[gw] if gw < len(row.xp) else 0.0})
            for row in rows
        ]
        best = pick_squad(single, budget=budget, bench_weight=0.0, time_limit=15)
        best_xp = {pid: _xp(rows_by_id, pid, gw) for pid in best.players}
        ideal = best_lineup(best.players, best_xp, positions)

        mine_xp = {pid: _xp(rows_by_id, pid, gw) for pid in squad}
        mine = best_lineup(squad, mine_xp, positions)
        values.append(max(0.0, ideal.expected_points - mine.expected_points))
    return values


def wildcard(squad: list[int], rows: list[PlayerRow], rows_by_id: dict[int, PlayerRow],
             positions: dict[int, str], horizon: int, budget: float) -> list[float]:
    """Rebuild the whole squad for free — and, unlike the free hit, keep it.

    Because it persists it is valued over the run of weeks it would serve, not a
    single one: the best squad money can buy across the horizon is priced once,
    and each week's value is how much more that squad's eleven is worth than
    yours that week. The week-to-week comparison is what matters, and `advise_chip`
    reads it relatively, so the constant gap between any real squad and the
    theoretical ideal does not fire the chip — only a week that genuinely stands
    out does. Holding stays the default: early on a well-drafted squad trails the
    ideal by about the same small margin every week, and the case to rebuild only
    appears once injuries or a fixture swing leave one stretch clearly behind.
    """
    best = pick_squad(rows, budget=budget, time_limit=20)
    values = []
    for gw in range(horizon):
        best_xp = {pid: _xp(rows_by_id, pid, gw) for pid in best.players}
        mine_xp = {pid: _xp(rows_by_id, pid, gw) for pid in squad}
        ideal = best_lineup(best.players, best_xp, positions)
        mine = best_lineup(squad, mine_xp, positions)
        values.append(max(0.0, ideal.expected_points - mine.expected_points))
    return values


def advise_chip(
    name: str,
    values: list[float],
    first_gameweek: int,
    *,
    gameweeks_remaining: int | None = None,
) -> ChipAdvice:
    """Play only when this week genuinely stands out, not merely when it leads a
    short window.

    `gameweeks_remaining` is how much season is left. When the horizon covers
    less than that, most of the season — including the double gameweeks a chip
    is usually saved for — is invisible, and the advice stays conservative.
    """
    if not values:
        return ChipAdvice(name, "hold", first_gameweek, 0.0, 0.0, "nothing to evaluate")

    best_index = max(range(len(values)), key=lambda i: values[i])
    best_value, value_now = values[best_index], values[0]
    best_gameweek = first_gameweek + best_index

    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    stands_out = (
        value_now >= mean + PLAY_NOW_SIGMA * spread
        and value_now >= mean * (1.0 + PLAY_NOW_MARGIN)
    )

    partial_view = (gameweeks_remaining is not None and len(values) < gameweeks_remaining)

    if best_index == 0 and stands_out and not partial_view:
        return ChipAdvice(
            name, "play", best_gameweek, best_value, value_now,
            f"worth {value_now:.1f} this week against a {mean:.1f} average — "
            f"the best week left and {value_now / mean - 1:.0%} clear of the rest")

    if best_index == 0 and stands_out and partial_view:
        return ChipAdvice(
            name, "hold", best_gameweek, best_value, value_now,
            f"best of the {len(values)} weeks in view at {value_now:.1f}, but "
            f"{gameweeks_remaining - len(values)} later weeks are unseen and that is "
            f"where the doubles are")

    if partial_view:
        return ChipAdvice(
            name, "hold", best_gameweek, best_value, value_now,
            f"GW{best_gameweek} is the best of {len(values)} weeks in view at "
            f"{best_value:.1f}; {gameweeks_remaining - len(values)} weeks still unseen")

    return ChipAdvice(
        name, "hold", best_gameweek, best_value, value_now,
        f"GW{best_gameweek} looks worth {best_value:.1f} against {value_now:.1f} now")


def evaluate_chips(
    squad: list[int],
    rows: list[PlayerRow],
    positions: dict[int, str],
    *,
    first_gameweek: int,
    horizon: int,
    budget: float = 100.0,
    gameweeks_remaining: int | None = None,
    available: tuple[str, ...] = ("wildcard", "triple captain", "bench boost", "free hit"),
) -> list[ChipAdvice]:
    """Value each chip across the horizon and say whether to spend it now.

    The horizon is the limit: a chip whose best week is beyond it cannot be seen,
    which is why holding stays the default and this gets re-run every week.
    """
    rows_by_id = {row.id: row for row in rows}
    remaining = gameweeks_remaining
    advice = []
    if "wildcard" in available:
        advice.append(advise_chip(
            "wildcard", wildcard(squad, rows, rows_by_id, positions, horizon, budget),
            first_gameweek, gameweeks_remaining=remaining))
    if "triple captain" in available:
        advice.append(advise_chip(
            "triple captain", triple_captain(squad, rows_by_id, positions, horizon),
            first_gameweek, gameweeks_remaining=remaining))
    if "bench boost" in available:
        advice.append(advise_chip(
            "bench boost", bench_boost(squad, rows_by_id, positions, horizon),
            first_gameweek, gameweeks_remaining=remaining))
    if "free hit" in available:
        advice.append(advise_chip(
            "free hit", free_hit(squad, rows, rows_by_id, positions, horizon, budget),
            first_gameweek, gameweeks_remaining=remaining))
    return advice
