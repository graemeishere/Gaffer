"""Why did I score what I scored?

`gaffer.score` marks the model's homework across every player in the league.
This is the other half: a post-mortem of the fifteen that were actually
fielded, which is the question a manager asks on a Monday morning.

The reason it exists is a specific failure of reasoning. GW1 finished 44 points
against a league mean of 49.1 and read, at a glance, like the model getting
something wrong. It was not. The spread of scores in that league was 23 to 74,
a standard deviation of 13.6, so the gap was 0.37 of one standard deviation —
comfortably inside the noise. The eleven fielded were the model's own top
eleven of those fifteen, in order; the captain was its clear first choice, and
both managers who beat the field captained the same player.

Without the spread beside it, a four-point gap invites a change to a model that
is working. So the spread is not decoration here — it is the number that stops
the reader drawing the wrong conclusion, and it is reported whether or not the
week went well.

What this cannot do is tell you the model is good. One gameweek says almost
nothing in either direction; a season of these, logged and read together, is
what settles it.
"""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass, field

# A pick owned by fewer than this share of the field is a differential: it can
# move you up the table on its own, and it can sink you on its own.
DIFFERENTIAL_OWNERSHIP = 10.0

# How far from the league mean counts as a real result rather than a week's
# variance. Two-thirds of gameweeks land inside one standard deviation by
# construction, so anything nearer than this is not evidence of anything.
NOTABLE_DEVIATIONS = 1.0


@dataclass
class Pick:
    player_id: int
    name: str
    position: str
    multiplier: int          # 0 on the bench, 2 for the captain
    points: int              # what he scored, before the armband
    minutes: int
    ownership: float
    projected: float | None = None

    @property
    def contributed(self) -> int:
        return self.points * self.multiplier

    @property
    def started(self) -> bool:
        return self.multiplier > 0


@dataclass
class GameweekReview:
    gameweek: int
    provisional: bool
    points: int
    picks: list[Pick] = field(default_factory=list)

    # Where that left you among the people you are actually playing.
    league_scores: list[int] = field(default_factory=list)
    league_position: int = 0
    league_size: int = 0

    captain: Pick | None = None
    captain_agreed: bool = True      # did the model want this captain too?
    best_starter: Pick | None = None
    points_on_bench: int = 0
    auto_subs: int = 0

    @property
    def league_mean(self) -> float:
        return st.mean(self.league_scores) if self.league_scores else 0.0

    @property
    def league_spread(self) -> float:
        """Standard deviation of the league's scores this week.

        The whole point of the section. A four-point gap against a spread of
        thirteen is noise; the same gap against a spread of three would not be.
        """
        return st.pstdev(self.league_scores) if len(self.league_scores) > 1 else 0.0

    @property
    def deviations_from_mean(self) -> float:
        spread = self.league_spread
        return (self.points - self.league_mean) / spread if spread else 0.0

    @property
    def within_normal_variation(self) -> bool:
        return abs(self.deviations_from_mean) < NOTABLE_DEVIATIONS

    @property
    def starters(self) -> list[Pick]:
        return [p for p in self.picks if p.started]

    @property
    def xi_points(self) -> int:
        """The eleven's raw return, before the armband doubles anyone."""
        return sum(p.points for p in self.starters)

    @property
    def xi_projected(self) -> float:
        return sum(p.projected or 0.0 for p in self.starters)

    @property
    def differentials(self) -> list[Pick]:
        return [p for p in self.starters if p.ownership < DIFFERENTIAL_OWNERSHIP]

    @property
    def captain_cost(self) -> int:
        """What the armband gave up against the best pick in your own eleven.

        Deliberately measured against your own eleven rather than the whole
        league: hindsight over 600 players is not a decision anyone could have
        made, and quoting it as a cost would make every week look like a blunder.
        """
        if not self.captain or not self.best_starter:
            return 0
        return max(0, self.best_starter.points - self.captain.points)

    @property
    def verdict(self) -> str:
        """One line, and the line has to be honest in both directions."""
        if not self.league_scores:
            return "No league to compare against this week."
        gap = self.points - self.league_mean
        where = (f"{self.points} points, {self.league_position} of {self.league_size}, "
                 f"{gap:+.1f} against a league average of {self.league_mean:.1f}")
        if self.within_normal_variation:
            return (f"{where}. The league's scores ran from {min(self.league_scores)} to "
                    f"{max(self.league_scores)} this week, so a gap this size is an "
                    f"ordinary week — not a verdict on the picks.")
        direction = "ahead of" if gap > 0 else "behind"
        return (f"{where}. That is further {direction} the field than a normal week "
                f"produces, so it is worth understanding rather than shrugging off.")


def review_gameweek(gameweek: int, picks: dict, live: dict, bootstrap: dict,
                    rivals: list | None = None,
                    projections: dict[int, float] | None = None,
                    model_captain: int | None = None,
                    provisional: bool = False) -> GameweekReview:
    """Build the post-mortem from data already fetched elsewhere in the run.

    `picks` is one `entry/{id}/event/{gw}/picks` payload, `live` the
    `event/{gw}/live` stats keyed by player id, `rivals` the league as
    `gaffer.league.standings.read_league` returns it, and `projections` the
    pre-deadline expected points from the prediction log.
    """
    names = {p["id"]: p["web_name"] for p in bootstrap["elements"]}
    types = {t["id"]: t["singular_name_short"] for t in bootstrap["element_types"]}
    position = {p["id"]: types.get(p["element_type"], "?") for p in bootstrap["elements"]}
    owned = {p["id"]: float(p.get("selected_by_percent") or 0)
             for p in bootstrap["elements"]}
    projections = projections or {}

    rows: list[Pick] = []
    for raw in sorted(picks.get("picks", []), key=lambda r: r.get("position", 0)):
        pid = raw["element"]
        stats = live.get(pid) or {}
        rows.append(Pick(
            player_id=pid,
            name=names.get(pid, "?"),
            position=position.get(pid, "?"),
            multiplier=raw.get("multiplier", 0),
            points=stats.get("total_points", 0),
            minutes=stats.get("minutes", 0),
            ownership=owned.get(pid, 0.0),
            projected=projections.get(pid),
        ))

    history = picks.get("entry_history") or {}
    captain = next((p for p in rows if p.multiplier > 1), None)
    starters = [p for p in rows if p.started]
    best = max(starters, key=lambda p: p.points, default=None)

    # An auto-sub fired wherever someone in the eleven never came on. FPL has
    # already applied it by the time picks are public, so this counts how often
    # the bench had to rescue the team rather than re-deriving the swap.
    auto_subs = sum(1 for p in starters if p.minutes == 0)

    scores = [r.gameweek_points for r in (rivals or []) if getattr(r, "gameweek_points", 0)]
    mine = history.get("points", 0)
    field_scores = sorted(scores + [mine], reverse=True)

    return GameweekReview(
        gameweek=gameweek,
        provisional=provisional,
        points=mine,
        picks=rows,
        league_scores=field_scores if scores else [],
        league_position=field_scores.index(mine) + 1 if scores else 0,
        league_size=len(field_scores) if scores else 0,
        captain=captain,
        captain_agreed=(model_captain is None or captain is None
                        or model_captain == captain.player_id),
        best_starter=best,
        points_on_bench=history.get("points_on_bench", 0),
        auto_subs=auto_subs,
    )


def summarise(review: GameweekReview) -> list[str]:
    """The CLI view, matching how `gaffer.score` prints its own summary."""
    out = [f"  gameweek {review.gameweek} review"
           + ("  (provisional — bonus not final)" if review.provisional else "")]
    out.append(f"    {review.verdict}")
    if review.xi_projected:
        out.append(f"    eleven returned {review.xi_points} against "
                   f"{review.xi_projected:.1f} projected")
    if review.captain:
        agreed = "the model's own pick" if review.captain_agreed else "not the model's pick"
        out.append(f"    captain {review.captain.name} scored {review.captain.points} "
                   f"({agreed}); best in your eleven was "
                   f"{review.best_starter.name if review.best_starter else '—'} on "
                   f"{review.best_starter.points if review.best_starter else 0}")
    out.append(f"    {review.points_on_bench} left on the bench"
               + (f", {review.auto_subs} auto-sub(s)" if review.auto_subs else ""))
    diffs = review.differentials
    if diffs:
        got = sum(p.points for p in diffs)
        noun = "differential" if len(diffs) == 1 else "differentials"
        out.append(f"    {len(diffs)} {noun} under "
                   f"{DIFFERENTIAL_OWNERSHIP:.0f}% ownership returned {got}")
    return out
