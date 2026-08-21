"""The published page.

`render.py` had no coverage at all, which meant every other test could pass
with the board rendering as raw `{{PLACEHOLDER}}` text, a table whose headers
no longer matched its columns, or a fragment styled by a class the stylesheet
had dropped. Nothing throws when that happens — the page just comes out wrong.

These are the checks that were being run by hand after each change to the
template. They matter most around the tab layout: the panels are hidden by
CSS, so a section stranded outside a tab, or a nav button pointing at an id
that does not exist, is invisible rather than obviously broken.
"""
from __future__ import annotations

import html.parser
import re

import pytest

from gaffer.publish import render as R

TEMPLATE = R.TEMPLATE.read_text()
LASTMAN_TEMPLATE = R.LASTMAN_TEMPLATE.read_text()
# The stylesheet is shared by every page the engine writes and injected through
# {{STYLE}}, so it is read from its own file rather than sliced out of a
# template — slicing the template would only ever find the placeholder.
# Comments are stripped before anything is asserted about the rules. A comment
# that merely mentions `prefers-color-scheme`, or names a `.class`, must not
# read as a rule that exists — that would make these checks quietly lenient.
STYLE = re.sub(r"/\*.*?\*/", "", R.STYLESHEET.read_text(), flags=re.S)

VOID = {"br", "img", "meta", "link", "hr", "input", "col"}


def player(pid, name, position, team="ALP", price=5.0, xp=None):
    xp = xp or [4.0, 3.0, 5.0, 2.0, 4.5, 3.5]
    return {
        "id": pid, "name": name, "team": team, "position": position, "price": price,
        "owned": 12.3, "xp": xp, "var": [3.0] * len(xp), "projected": sum(xp),
        "per_million": sum(xp) / price, "minutes": 78.0, "fixture_score": 2.5,
        "availability": 1.0, "confidence": "high", "moved_club": False, "note": "",
    }


def payload(**over):
    """A payload with every optional block filled, so the busy paths render."""
    squad_ids = list(range(1, 16))
    positions = ["GKP", "GKP"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    players = [player(i, f"Player{i}", positions[i - 1], price=4.5 + i / 10)
               for i in squad_ids]
    starters = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    base = {
        "meta": {
            "generated": "2026-08-21T09:00:00+00:00",
            "gameweek": 1,
            "deadline": "2026-08-21T17:30:00Z",
            "horizon": 6,
            "stage": "phase-4",
            "method": "expected-points",
            "strength_source": "prior",
            "matches_fitted": 0,
            "warning": "Built from last season's rates.",
        },
        "counts": {"players_ranked": 15, "teams": 20, "flagged": 1, "moved_club": 2},
        "players": players,
        "fixtures": {
            "ALP": [{"gameweek": g, "opponent": "BET", "home": g % 2 == 0,
                     "difficulty": (g % 5) + 1} for g in range(1, 7)],
            "BET": [{"gameweek": g, "opponent": "ALP", "home": g % 2 == 1,
                     "difficulty": ((g + 2) % 5) + 1} for g in range(1, 7)],
        },
        "squad": {"players": squad_ids, "bench": [2, 6, 7, 12],
                  "starters_by_gameweek": {"0": starters}, "cost": 99.5},
        "lineup": {"formation": "3-4-3", "starters": starters, "bench": [2, 6, 7, 12],
                   "captain": 13, "vice": 9, "expected_points": 53.3},
        "transfers": [
            {"transfers": 1, "out": [12], "in": [14], "net_gain": 1.42,
             "uncertainty": 0.9, "note": "Better run of fixtures."},
            {"transfers": 0, "out": [], "in": [], "net_gain": 0.0,
             "uncertainty": 0.0, "note": "Bank it."},
            {"transfers": 2, "out": [6, 7], "in": [4, 5], "net_gain": -0.8,
             "uncertainty": 1.1, "note": "Costs more than it returns."},
        ],
        "chips": [
            {"chip": "wildcard", "action": "hold", "best_gameweek": 4,
             "best_value": 8.0, "value_now": 2.0, "reason": "Squad is fine."},
            {"chip": "bench boost", "action": "hold", "best_gameweek": 5,
             "best_value": 6.0, "value_now": 1.0, "reason": "Bench is thin."},
            {"chip": "triple captain", "action": "hold", "best_gameweek": 3,
             "best_value": 7.2, "value_now": 7.0, "reason": "Better week ahead."},
        ],
        "league": {
            "league_id": 1, "kind": "classic", "name": "Test League", "rivals": 14,
            "simulation": {"win_probability": 0.18, "my_mean": 320.0, "gameweeks": 6},
            "advice": {"stance": "protect", "differential_count": 3,
                       "reason": "You are ahead.", "suggested": "Hold the line.",
                       "biggest_exposure": ["Player13", "Player9"]},
        },
        "schedule": {"phase": "planning", "reason": "Deadline is not close.",
                     "hours_remaining": 8.5},
        "manager": {"name": "Worrall FC", "squad_readable": True},
    }
    return base | over


def render(tmp_path, data=None):
    out = R.write_report(data or payload(), tmp_path / "report.html")
    return out.read_text()


class Parser(html.parser.HTMLParser):
    """Tracks nesting, and which tab panel each element sits inside."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.errors: list[str] = []
        self.tab: str | None = None
        self.headings: dict[str | None, list[str]] = {}
        self.classes: set[str] = set()
        self.tab_ids: list[str] = []
        self._h2 = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in (attrs.get("class") or "").split():
            self.classes.add(name)
        if tag == "div" and "tab" in (attrs.get("class") or "").split():
            self.tab = attrs.get("id")
            self.tab_ids.append(self.tab)
        if tag == "h2":
            self._h2 = ""
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "h2" and self._h2 is not None:
            self.headings.setdefault(self.tab, []).append(self._h2.strip())
            self._h2 = None
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"stray </{tag}>")
            return
        if self.stack[-1][0] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1][0]}>")
            if tag in [t for t, _ in self.stack]:
                while self.stack and self.stack.pop()[0] != tag:
                    pass
            return
        name, attrs = self.stack.pop()
        if name == "div" and "tab" in (attrs.get("class") or "").split():
            self.tab = None

    def handle_data(self, data):
        if self._h2 is not None:
            self._h2 += data


def parse(doc) -> Parser:
    p = Parser()
    p.feed(doc)
    return p


class TestPlaceholders:
    def test_every_placeholder_is_substituted(self, tmp_path):
        """A missed token renders as literal {{NAME}} on the live page."""
        assert re.findall(r"\{\{[A-Z_]+\}\}", render(tmp_path)) == []

    def test_template_placeholders_are_all_known_to_the_renderer(self, tmp_path):
        """Guards the other direction: a token added to the template with no
        replacement wired up would leak, and the check above only catches it
        once someone renders."""
        assert re.findall(r"\{\{[A-Z_]+\}\}", render(tmp_path, payload(
            squad=None, lineup=None, transfers=[], chips=[], league=None,
            manager=None, schedule=None))) == []


class TestStructure:
    def test_the_encoding_is_declared(self, tmp_path):
        """Without this the browser sniffs, and the page is full of £ and ·
        characters — every price and every separator renders as mojibake."""
        assert '<meta charset="utf-8">' in render(tmp_path)

    def test_tags_nest_and_close(self, tmp_path):
        p = parse(render(tmp_path))
        assert p.errors == []
        assert [t for t, _ in p.stack] == []

    def test_all_seven_sections_are_present(self, tmp_path):
        doc = render(tmp_path)
        assert len(re.findall(r"<h2>", doc)) == 7

    def test_table_headers_match_their_body_cells(self, tmp_path):
        """A column added to a header without a matching cell silently shifts
        every value in the table one place to the left."""
        doc = render(tmp_path)
        tables = re.findall(r"<table>(.*?)</table>", doc, re.S)
        assert tables
        for table in tables:
            headers = len(re.findall(r"<th\b", table))
            for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
                cells = len(re.findall(r"<td\b", row))
                if cells:
                    assert cells == headers


class TestTabs:
    """The nav hides four panels in five, so a broken link is invisible."""

    def test_every_button_points_at_a_panel_that_exists(self, tmp_path):
        doc = render(tmp_path)
        buttons = set(re.findall(r'data-tab="([a-z-]+)"', doc))
        panels = set(parse(doc).tab_ids)
        assert buttons == panels

    def test_every_section_sits_inside_a_panel(self, tmp_path):
        p = parse(render(tmp_path))
        assert None not in p.headings, "a section is outside every tab and unreachable"

    def test_sections_are_grouped_as_designed(self, tmp_path):
        p = parse(render(tmp_path))
        assert p.headings["squad"] == ["The squad"]
        assert p.headings["transfers"] == ["Transfers"]
        assert p.headings["chips"] == ["Chips"]
        assert p.headings["league"] == ["Your mini-league"]
        assert len(p.headings["tables"]) == 3

    def test_panels_are_visible_without_javascript(self, tmp_path):
        """Progressive enhancement is what keeps 'no content stripped' true for
        a printout, for Ctrl+F, and for a reader whose scripts did not load.
        Hiding must be gated behind the class the script adds."""
        assert re.search(r"\.js-tabs\s+\.tab\s*\{[^}]*display:\s*none", STYLE)
        assert not re.search(r"(?<!\.js-tabs )\.tab\s*\{[^}]*display:\s*none", STYLE)

    def test_the_caveat_banner_is_not_hidden_behind_a_tab(self, tmp_path):
        """It warns against over-trusting the numbers in every panel."""
        doc = render(tmp_path)
        assert doc.index('class="banner"') < doc.index('class="tabs"')


class TestTheme:
    """Light is the only palette, by explicit decision."""

    def test_no_system_preference_block(self):
        assert "prefers-color-scheme" not in STYLE

    def test_no_dark_override(self):
        assert "data-theme" not in STYLE

    def test_body_background_comes_from_a_token(self):
        body = re.search(r"\bbody\s*\{(.*?)\}", STYLE, re.S).group(1)
        assert re.search(r"background:\s*var\(--paper\)", body)

    def test_the_fixture_scale_keeps_a_neutral_midpoint(self):
        """Diverging scales need a colourless middle. --d3 is the midpoint, and
        a hue there reads as a third category rather than 'average'."""
        midpoint = re.search(r"--d3:\s*oklch\(([\d.]+)\s+([\d.]+)", STYLE)
        assert float(midpoint.group(2)) <= 0.02


class TestClassContract:
    """Every class the renderer emits must be styled, or a fragment renders
    unstyled and nothing errors."""

    @pytest.mark.parametrize("data", [
        pytest.param(None, id="full"),
        pytest.param(payload(squad=None, lineup=None, transfers=[], chips=[],
                             league=None, manager=None), id="empty"),
        pytest.param(payload(transfers=[], manager={"name": "X", "reason": "Squads are private until the deadline."}),
                     id="waiting"),
        pytest.param(payload(league={"league_id": 1, "kind": "h2h", "name": "H2H",
                                     "waiting": True}), id="h2h-waiting"),
    ])
    def test_emitted_classes_are_styled(self, tmp_path, data):
        styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", STYLE))
        emitted = parse(render(tmp_path, data)).classes
        assert emitted <= styled, f"unstyled: {sorted(emitted - styled)}"


class TestEmptyStates:
    """These replaced messages that blamed configuration for what was timing."""

    def test_an_unlinked_squad_says_it_is_a_suggestion(self, tmp_path):
        doc = render(tmp_path, payload(manager=None))
        assert "not your team" in doc

    def test_waiting_on_the_deadline_is_explained_as_timing(self, tmp_path):
        doc = render(tmp_path, payload(
            transfers=[], manager={"name": "X", "reason": "Squads are private until then."}))
        assert "Waiting on the deadline" in doc
        assert "GAFFER_ENTRY" not in doc

    def test_a_missing_squad_does_not_crash_the_pitch(self, tmp_path):
        doc = render(tmp_path, payload(squad=None, lineup=None))
        assert "No squad selected" in doc


class TestNumbersStayLegible:
    def test_transfer_gains_carry_their_uncertainty(self, tmp_path):
        """The model only draws with a naive benchmark; a bare point estimate
        would overstate what it knows."""
        doc = render(tmp_path, payload())
        assert "±" in doc

    def test_the_hand_never_carries_data(self):
        """Caveat is for voice. A scrawled decimal is a number you cannot read
        at a glance, so no numeric class may set it."""
        for block in re.findall(r"\.(num|stat b|opt-r|spark)[^{]*\{([^}]*)\}", STYLE):
            assert "Caveat" not in block[1]


class TestLastManStandingPage:
    """The pool page shares the stylesheet with the board, so a restyle written
    against the board alone can leave it unstyled without failing anything.
    These are the design checks the board already gets, pointed at that page.
    """

    def lastman(self, tmp_path):
        from gaffer.lms.advise import advise
        from gaffer.lms.rules import Rules
        from gaffer.lms.state import LmsState
        from tests.test_lms import NAMES, TestAdvice

        result = advise(TestAdvice.ROUNDS, NAMES, LmsState(), Rules(horizon=2))
        data = {
            "meta": {"generated": "2026-08-21T09:00:00+00:00",
                     "deadline": "2026-08-21T17:30:00Z", "gameweek": 1,
                     "strength_source": "fitted", "matches_fitted": 40},
            "lms": result.as_dict(),
            "schedule": {"reason": "9h to the GW1 deadline"},
        }
        return R.write_lastman(data, tmp_path / "lastman.html").read_text()

    def test_it_is_readable_on_a_phone(self, tmp_path):
        """The board is mobile-first; a sister page with no viewport tag would
        render at desktop width and shrink to nothing."""
        doc = self.lastman(tmp_path)
        assert 'name="viewport"' in doc
        assert 'charset="utf-8"' in doc

    def test_it_asks_for_the_same_two_faces_as_the_board(self, tmp_path):
        doc = self.lastman(tmp_path)
        assert "family=Archivo" in doc and "family=Caveat" in doc
        # The faces the restyle replaced. Left behind, they load on every view
        # and never get used.
        assert "Source+Serif" not in doc and "JetBrains" not in doc

    def test_every_class_it_emits_is_styled(self, tmp_path):
        # Parsed rather than regexed: the fragment builders quote with ' and
        # the template with ", and anchoring on one would skip half the page.
        emitted = parse(self.lastman(tmp_path)).classes
        assert len(emitted) > 10, "the page under test emitted almost no classes"
        styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", STYLE))
        assert emitted <= styled, f"unstyled: {sorted(emitted - styled)}"

    def test_its_tags_are_balanced(self, tmp_path):
        p = parse(self.lastman(tmp_path))
        assert p.errors == []
        assert [t for t, _ in p.stack] == []


class TestLastManStandingIsSeparate:
    """It is a different competition off the same fixture list, not a section
    of the fantasy board. Two pages carrying the same recommendation drift in
    the reader's head into two different recommendations."""

    def test_the_board_carries_no_last_man_standing_section(self, tmp_path):
        p = parse(render(tmp_path))
        assert all("last man" not in h.lower()
                   for hs in p.headings.values() for h in hs)
        assert "lastman" not in p.tab_ids

    def test_the_board_still_links_to_its_page(self, tmp_path):
        """Separate must not mean unreachable — the pool page links back here,
        and this is the other half of that pair."""
        assert 'href="lastman.html"' in render(tmp_path)
