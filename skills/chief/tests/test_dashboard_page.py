"""Page tests for the chief dashboard (skills/chief/dashboard.html).

The page is one self-contained file, so these tests read its <style> text and run its <script> under Node with a
tiny fake DOM (PRELUDE). Later page tasks reuse page_css, run_page, sample_run and sample_board.
"""
import json
import math
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAGE = Path(__file__).resolve().parent.parent / "dashboard.html"

PRELUDE = r"""
const __reg = {}   // selector -> fake elements returned by querySelector(All); tests seed it
const __byId = {}
const __fetches = []
function __el(id = '') {
  const handlers = {}
  const el = {
    id, dataset: {}, style: {}, hidden: true, textContent: '', scrollTop: 0, scrollLeft: 0, _html: '',
    get innerHTML() { return this._html },
    set innerHTML(v) { this._html = String(v); if (id === 'score') Object.values(__reg).flat().forEach((e) => { e.scrollTop = 0; e.scrollLeft = 0 }) },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false } },
    setAttribute(k, v) { this['@' + k] = String(v) }, getAttribute(k) { return this['@' + k] ?? null },
    addEventListener(type, fn) { (handlers[type] ||= []).push(fn) },
    __fire(type, extra = {}) { (handlers[type] || []).forEach((fn) => fn({ type, target: el, preventDefault() {}, ...extra })) },
    focus() { document.activeElement = el }, contains() { return true }, closest() { return null }, matches() { return false },
    querySelector(s) { return (__reg[s] || [])[0] || null }, querySelectorAll(s) { return __reg[s] || [] },
  }
  return el
}
globalThis.window = globalThis
globalThis.document = Object.assign(__el('document'), {
  activeElement: null,
  getElementById: (id) => (__byId[id] ||= __el(id)),
})
window.addEventListener = () => {}
window.getSelection = () => ({ rangeCount: 0, isCollapsed: true, toString: () => '' })
globalThis.location = { search: '', pathname: '/' }
globalThis.history = { replaceState() {} }
Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText: async () => {} } }, configurable: true })
globalThis.MouseEvent = class { constructor(type, init) { Object.assign(this, init, { type }) } }
globalThis.fetch = (url, init) => { __fetches.push([String(url), init || {}]); return new Promise(() => {}) }
globalThis.setInterval = () => 0
"""


def page_css() -> str:
    """The text of the page's <style> element."""
    m = re.search(r"<style>(.*?)</style>", PAGE.read_text(), re.S)
    assert m, "dashboard.html has no <style> element"
    return m.group(1)


def page_script() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(), re.S)
    assert scripts, "dashboard.html has no inline <script>"
    return scripts[-1]


def run_page(snippet: str, timeout: float = 20, before: str = "") -> Any:
    """Run the page's script under Node with PRELUDE, then SNIPPET; return its JSON result.

    BEFORE runs between PRELUDE and the page's script: for what the page reads while it loads (storage, timers)."""
    tail = (
        ";(async () => { " + snippet + " })()"
        ".then((v) => console.log(JSON.stringify(v === undefined ? null : v)))"
        ".catch((e) => { console.error(e); process.exit(1) })\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "page.js"
        js.write_text(PRELUDE + "\n" + before + "\n" + page_script() + "\n" + tail)
        proc = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def sample_run(**over) -> dict:
    run = {
        "key": "r1@abc", "id": "r1", "title": "CSV export", "source": "chat", "phase": "implement", "phases": [],
        "repo": "/x/repo", "started": "2026-11-17T00:00:00Z", "ended": None, "agents": [], "tasks": [],
        "quiet": False, "archived": None, "session_id": None, "chat_title": None, "cwd": "/x/repo",
    }
    run.update(over)
    return run


def sample_board(runs) -> dict:
    return {
        "generated": "2026-11-17T00:00:00Z", "runs": runs, "repos": [], "accounts": {},
        "spend": {"all_runs": 0, "last_24h": 0, "burn_per_hour": 0, "buckets": [0, 0]},
        "archived_count": 0, "prices_note": "x",
    }


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LayoutTest(unittest.TestCase):
    def test_the_kit_runs_the_page(self):
        self.assertEqual(run_page("return money(1.5)"), "$1.50")

    def test_cards_and_columns_cap_at_650px(self):
        css = page_css()
        self.assertRegex(css, r"\.col\s*\{[^}]*max-width:\s*650px")
        self.assertRegex(css, r"\.card\s*\{[^}]*max-width:\s*650px")
        self.assertRegex(css, r"\.grid-archived\s*\{[^}]*repeat\(auto-fill,\s*minmax\(min\(260px,\s*100%\),\s*1fr\)\)")
        self.assertNotIn("360px", css)

    def test_there_is_no_page_wide_cap(self):
        css = page_css()
        self.assertNotIn("1600px", css)
        self.assertNotRegex(css, r"\.hall\s*\{[^}]*max-width")  # the stage's hall is a backdrop, not a cap

    def test_the_programme_wraps_and_never_scrolls(self):
        css = page_css()
        self.assertRegex(css, r"\.prog\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap[^}]*gap:\s*10px")
        self.assertRegex(css, r"\.col\s*\{[^}]*flex:\s*1 1 260px[^}]*min-width:\s*0[^}]*max-width:\s*650px")
        self.assertRegex(css, r"\.col\.rail\s*\{[^}]*flex:\s*0 0 34px")
        prog = css.split("/* programme */")[1].split("/* the score */")[0]
        for banned in ("overflow-x", "scroll-snap", "@container", "var(--n)", "var(--a)", "var(--b)"):
            self.assertNotIn(banned, prog)
        small = css[css.index("@media (max-width: 1100px)"):css.index("@media (max-width: 620px)")]
        self.assertNotRegex(small, r"\.(prog|col|cards)\b")

    def test_the_git_notice_wraps_inside_a_narrow_window(self):
        css = page_css()
        notice = re.search(r"\.notice\s*\{([^}]*)\}", css).group(1)
        self.assertRegex(notice, r"flex-wrap:\s*wrap")
        self.assertRegex(notice, r"min-width:\s*0")
        self.assertRegex(css, r"\.notice ul\s*\{[^}]*min-width:\s*0")
        self.assertRegex(css, r"\.notice li\s*\{[^}]*overflow-wrap:\s*anywhere")
        label = re.search(r"\.notice b\s*\{([^}]*)\}", css).group(1)
        self.assertNotIn("nowrap", label)                       # a long repo or branch name wrapped nowhere and scrolled the page sideways
        self.assertRegex(label, r"overflow-wrap:\s*anywhere")

    def test_empty_columns_keep_their_vertical_title(self):
        css = page_css()
        self.assertRegex(css, r"\.col\.rail \.col-head\s*\{[^}]*writing-mode:\s*vertical-rl")
        self.assertNotIn("horizontal-tb", css)  # no breakpoint turns the title sideways again
        html = run_page(f"active = {json.dumps(sample_board([sample_run(phase='implement')]))}; renderProgramme(); return $('prog').innerHTML")
        self.assertIn('class="col rail"', html)  # the empty columns still render as rails

    def test_the_archived_view_uses_the_capped_grid(self):
        board = sample_board([sample_run(phase="done")])
        html = run_page(f"view = 'archived'; active = archived = {json.dumps(board)}; renderProgramme(); return $('prog').innerHTML")
        self.assertIn('class="grid-archived"', html)
        self.assertNotIn("minmax(260px,1fr)", html)

    def test_the_staff_places_notes_by_percentage_with_fixed_heads(self):
        tasks = [{"id": "1", "status": "build"}, {"id": "2", "status": "done"}]
        svg = run_page(f"return cardStaff({json.dumps(sample_run(phase='implement', tasks=tasks))})")
        for banned in ("preserveAspectRatio", "viewBox", "drop-shadow"):
            self.assertNotIn(banned, svg)
        self.assertRegex(svg, r'<line x1="0" x2="100%"')
        self.assertRegex(svg, r'<svg x="[\d.]+%" y="[\d.]+" width="1" height="1" overflow="visible">')
        heads = lambda s: set(re.findall(r'<ellipse rx="([\d.]+)" ry="([\d.]+)"', s))  # glow ellipses differ; hollow and solid heads match
        wide = [{"id": str(n), "status": "done"} for n in range(9)]
        self.assertEqual(heads(run_page(f"return cardStaff({json.dumps(sample_run(phase='implement', tasks=wide))})")), {("3.12", "2.2")})
        self.assertRegex(page_css(), r"\.staff\s*\{[^}]*width:\s*100%[^}]*height:\s*30px")

    def test_measures_never_scale_above_one_and_scroll_when_narrow(self):
        css = page_css()
        self.assertRegex(css, r"\.measures\s*\{[^}]*overflow-x:\s*auto")
        self.assertRegex(css, r"\.measures svg\s*\{[^}]*min-width:\s*760px")
        phases = [{"phase": "intake", "at": "2026-11-17T00:00:00Z"}, {"phase": "plan", "at": "2026-11-17T00:10:00Z"}]
        svg = run_page(f"return measures({json.dumps(sample_run(phases=phases))})")
        width = re.search(r'viewBox="0 0 (\d+) 60"', svg).group(1)
        self.assertGreaterEqual(int(width), 760)
        self.assertIn(f'style="max-width:{width}px"', svg)

    def test_measures_and_sparkline_do_not_distort(self):
        phases = [{"phase": "intake", "at": "2026-11-17T00:00:00Z"}, {"phase": "plan", "at": "2026-11-17T00:10:00Z"}]
        self.assertNotIn("preserveAspectRatio", run_page(f"return measures({json.dumps(sample_run(phases=phases))})"))
        spark = run_page("return sparkline([0, 1, 2])")
        self.assertNotIn("<circle", spark)
        self.assertIn("non-scaling-stroke", spark)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HeaderTest(unittest.TestCase):
    def test_gauges_come_first_in_one_wrapping_row(self):
        html = PAGE.read_text()
        self.assertLess(html.index('id="meters"'), html.index('id="ledger"'))
        self.assertRegex(page_css(), r"\.right\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap")

    METER = ("Claude", {"5h": 11, "week": 29}, "#D4703C")

    @staticmethod
    def rule(css, sel):
        m = re.search(r"(?<![\w-])" + re.escape(sel) + r"\s*\{([^}]*)\}", css)
        assert m, f"no rule for {sel}"
        return m.group(1)

    @staticmethod
    def media(query):
        """The body of the page's `@media (<query>) { ... }` block, brace-matched."""
        css = page_css()
        start = css.index("@media (" + query + ")")
        open_at = css.index("{", start)
        depth = 0
        for i in range(open_at, len(css)):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            if depth == 0:
                return css[open_at + 1:i]
        raise AssertionError("unbalanced @media " + query)

    def test_a_gauge_is_a_dial_beside_its_readings_as_tall_as_the_ledger(self):
        css = page_css()
        meter = self.rule(css, ".meter")
        for want in (r"display:\s*flex", r"align-items:\s*center", r"justify-content:\s*center", r"gap:\s*8px", r"padding:\s*8px 10px"):
            self.assertRegex(meter, want)
        self.assertNotIn("grid-template-columns", meter)
        meters = self.rule(css, ".meters")
        self.assertRegex(meters, r"display:\s*grid")
        self.assertRegex(meters, r"grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)")   # 50 / 50 at any width
        self.assertRegex(meters, r"align-items:\s*stretch")   # the gauges take the ledger's height
        self.assertRegex(self.rule(css, ".right"), r"align-self:\s*flex-start")  # else the row stretches to the headline block
        dial = self.rule(css, ".meter svg")
        for want in (r"flex:\s*0 1 104px", r"min-width:\s*0", r"width:\s*104px", r"height:\s*auto", r"aspect-ratio:\s*132\s*/\s*76"):
            self.assertRegex(dial, want)
        self.assertNotRegex(dial, r"height:\s*76px")
        self.assertRegex(css, r"\.headline\s*\{[^}]*clamp\(26px,\s*3\.4vw,\s*40px\)")
        svg = run_page("return meter(...%s)" % json.dumps(self.METER))
        self.assertIn('viewBox="0 0 132 76"', svg)    # the drawing is unchanged; only its box is

    def test_each_reading_has_a_swatch_keyed_to_its_needle(self):
        css = page_css()
        self.assertRegex(self.rule(css, ".rd"), r"display:\s*flex[^}]*align-items:\s*baseline[^}]*gap:\s*5px")
        self.assertRegex(self.rule(css, ".rd b"), r"font:\s*400 20px/1\.05 var\(--f-display\)")
        sw = self.rule(css, ".rd .sw")
        for want in (r"width:\s*11px", r"height:\s*2px", r"background:\s*var\(--c\)"):
            self.assertRegex(sw, want)
        wk = self.rule(css, ".rd.wkr .sw")                # the week needle is drawn faint and a little thicker
        self.assertRegex(wk, r"height:\s*2\.4px")
        self.assertRegex(wk, r"opacity:\s*\.5")

    def test_the_ledger_keeps_its_1_1_0_size_and_padding(self):
        css = page_css()
        cell = self.rule(css, ".ledger > div")
        self.assertRegex(cell, r"padding:\s*8px 14px")
        self.assertNotIn("align-content", cell)           # the cells are no longer padded out to the gauges' height
        self.assertRegex(self.rule(css, ".ledger"), r"grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)")   # equal thirds at any width
        self.assertRegex(self.rule(css, ".ledger .v"), r"font:\s*400 26px/1")
        spark = self.rule(css, ".ledger svg")              # fills its cell without sizing it, else it widens every third
        self.assertRegex(spark, r"width:\s*0")
        self.assertRegex(spark, r"min-width:\s*100%")

    def test_the_60_percent_line_is_back_with_its_flag(self):
        svg = run_page("return meter(...%s)" % json.dumps(self.METER))
        line = re.search(r'<path d="M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+)" stroke="#FFC45E" stroke-width="2\.6"/>', svg)
        self.assertTrue(line, "no 60% line of width 2.6")
        x1, y1, x2, y2 = (float(n) for n in line.groups())
        self.assertAlmostEqual(math.hypot(x1 - 66, y1 - 66), 57, places=2)   # R + 3
        self.assertAlmostEqual(math.hypot(x2 - 66, y2 - 66), 40, places=2)   # R - 14
        self.assertAlmostEqual(math.degrees(math.atan2(66 - y1, x1 - 66)), 72, places=2)   # 60% of the half circle
        flag = re.search(r"<text ([^>]*)>60%</text>", svg)
        self.assertTrue(flag, "no 60% flag")
        for want in ('x="89"', 'y="14"', 'font-size="13"', 'font-weight="500"', 'fill="#FFC45E"', "IBM Plex Mono"):
            self.assertIn(want, flag.group(1))
        self.assertEqual(svg.count("<text"), 1)           # the dial shows no number of its own

    def test_the_row_fills_the_width_the_moment_it_wraps_under_the_headline(self):
        css = page_css()
        # No fixed width decides it. The headline block soaks up the free space while it shares a line with the row, so the row
        # keeps its natural size; once the row wraps it is alone on its line and its flex-grow fills it.
        self.assertNotIn("1280px", css)
        self.assertNotIn("1 1 400px", css)
        self.assertRegex(self.rule(css, ".stage-top > :first-child"), r"flex:\s*1000 1 auto")
        right = self.rule(css, ".right")
        self.assertRegex(right, r"flex:\s*1 1 auto")
        self.assertNotIn("100%", right)                    # never forced to a full row by a width
        self.assertRegex(right, r"min-width:\s*0")
        for sel in (".meters", ".ledger"):               # both blocks share what the row gains, and may shrink below their natural size
            body = self.rule(css, sel)
            self.assertRegex(body, r"flex:\s*1 1 auto")
            self.assertRegex(body, r"min-width:\s*0")
        self.assertRegex(self.rule(css, ".ledger > div"), r"min-width:\s*0")

    def test_gauges_alone_on_a_row_grow_their_dial(self):
        # 900, not 880: the 1.5px stage border snaps to 1px at 1x density, which moved the 880 edge by a pixel
        css = self.media("max-width: 900px")
        self.assertRegex(self.rule(css, ".meter"), r"gap:\s*14px")
        self.assertRegex(self.rule(css, ".meter svg"), r"flex-basis:\s*124px[^}]*width:\s*124px")
        self.assertNotIn("880px", page_css())

    def test_on_narrow_phones_the_dial_sits_above_its_readings_in_each_half(self):
        # Side by side the dial would fall under ~56px just below 410px; stacked it takes the card's width, capped at 124px.
        css = self.media("max-width: 410px")
        self.assertRegex(self.rule(css, ".meter"), r"flex-direction:\s*column")
        dial = self.rule(css, ".meter svg")
        for want in (r"flex:\s*none", r"width:\s*100%", r"max-width:\s*124px"):
            self.assertRegex(dial, want)
        self.assertNotRegex(css, r"grid-template-columns")   # still two gauges across: 50 / 50 holds down to 320px

    def test_phone_sizes_from_520px(self):
        css = self.media("max-width: 520px")
        meter = self.rule(css, ".meter")
        self.assertRegex(meter, r"gap:\s*6px")
        self.assertRegex(meter, r"padding:\s*8px")
        self.assertRegex(self.rule(css, ".ledger > div"), r"padding:\s*8px 10px")
        self.assertRegex(self.rule(css, ".ledger .v"), r"font-size:\s*22px")
        self.assertRegex(self.rule(css, ".ledger .label, .ledger .mix"), r"font-size:\s*10px")   # captions
        self.assertRegex(self.rule(css, ".rd b"), r"font-size:\s*18px")

    def test_the_ledger_shrinks_to_fit_the_narrowest_phones(self):
        # At 320px each third is about 83px wide. With the 520px sizes (10px padding, 22px figures) "$12,345.67" is 74px wide
        # against 63px of room, so the text ran into the next cell. From 380px down the cells give up padding and the figures
        # a little size instead; three across stays, and a caption that cannot fit wraps (it breaks at the hyphen).
        css = page_css()
        phone = self.media("max-width: 380px")
        self.assertRegex(self.rule(phone, ".ledger > div"), r"padding:\s*8px 6px")
        self.assertRegex(self.rule(phone, ".ledger .v"), r"font-size:\s*18px")
        self.assertRegex(self.rule(phone, ".ledger .v small"), r"font-size:\s*10px")
        self.assertNotRegex(phone, r"grid-template-columns")                # no 2 + 1 split: the thirds stay equal
        self.assertNotRegex(phone, r"white-space:\s*nowrap")                 # the caption may wrap
        self.assertGreater(css.index("@media (max-width: 380px)"), css.index("@media (max-width: 520px)"))   # it must win the cascade
        sizes = [float(n) for n in re.findall(r"\.ledger \.v\s*\{[^}]*?font(?:-size)?:\s*(?:\d+ )?([\d.]+)px", css)]
        self.assertEqual(len(sizes), 3)                                      # desktop, 520px, 380px
        self.assertGreaterEqual(min(sizes), 15)                              # the figures stay legible at every width

    def test_the_header_row_is_one_gap_under_the_headline_at_every_width(self):
        # A zero-size spacer between the headline and the row wrapped onto a line of its own once the headline filled the line
        # (at 375px and under), which added a second 16px row gap. The headline block takes the free space beside the row
        # instead, so there is nothing left to wrap alone: the gap is the one 16px row gap at every width.
        css = page_css()
        self.assertNotRegex(css, r"\.stage-top::(?:after|before)")
        lead = self.rule(css, ".stage-top > :first-child")
        self.assertRegex(lead, r"flex:\s*1000 1 auto")
        right = self.rule(css, ".right")
        self.assertRegex(right, r"flex:\s*1 1 auto")                         # 1 : 1000, so beside the headline the row keeps its natural size
        self.assertNotRegex(right, r"order:")
        self.assertRegex(self.rule(css, ".stage-top"), r"gap:\s*16px")

    def test_the_ledger_never_falls_back_to_two_columns(self):
        css = page_css()
        self.assertNotIn(".ledger > div:nth-child(3)", css)   # the old 620px 2 + 1 split
        self.assertNotRegex(css, r"\.meter svg\s*\{[^}]*(112px|66px)")
        self.assertNotRegex(self.media("max-width: 1100px"), r"\.right")

    def test_the_gauge_script_fills_both_readings(self):
        html = run_page("return meter('ChatGPT', { '5h': 0, week: 90 }, '#74B9A7')")
        self.assertIn('<b>0%</b><small>5 h</small>', html)     # 0 is a reading, not a missing one
        self.assertIn('<b>90%</b><small>week</small>', html)
        self.assertEqual(html.count('style="--c:#74B9A7"'), 2)  # both swatches carry the account colour
        self.assertRegex(html, r'<span class="rd wkr"')
        self.assertIn('aria-label="ChatGPT: 0% of the 5-hour window, 90% of the week; line at 60%"', html)
        self.assertEqual(html.count('stroke-linecap="round"'), 2)   # both needles are drawn
        self.assertIn('stroke-opacity=".5"', html)

    def test_both_gauges_are_filled_from_the_board(self):
        board = sample_board([sample_run()])
        board["accounts"] = {"claude": {"5h": 11, "week": 29}, "chatgpt": {"5h": 0, "week": 90}}
        html = run_page(f"active = {json.dumps(board)}; renderStage(); return $('meters').innerHTML")
        self.assertEqual(html.count('class="meter"'), 2)
        for want in ("<b>11%</b>", "<b>29%</b>", "<b>0%</b>", "<b>90%</b>", "Claude", "ChatGPT"):
            self.assertIn(want, html)

    def test_an_unreadable_account_draws_dashes_no_needles_and_the_hint(self):
        for reading in ("undefined", "{ '5h': null, week: null }", "{}"):
            html = run_page(f"return meter('Claude', {reading}, '#D4703C')")
            self.assertIn('aria-label="no reading"', html)
            self.assertIn("<b>—</b><small>5 h</small>", html)
            self.assertIn("run the usage check", html)
            self.assertNotIn('stroke-linecap="round"', html)    # no needle without a reading
            for junk in ("NaN", "null", "undefined"):
                self.assertNotIn(junk, html)
            self.assertIn("60%", html)                           # the line and its flag stay
        half = run_page("return meter('Claude', { '5h': 40, week: null }, '#D4703C')")
        self.assertIn("<b>40%</b>", half)
        self.assertIn("run the usage check", half)
        self.assertEqual(half.count('stroke-linecap="round"'), 1)

    def test_no_line_between_the_header_and_the_stages(self):
        hall = re.search(r"\.hall\s*\{([^}]*)\}", page_css()).group(1)
        self.assertNotIn("border", hall)
        self.assertNotIn("background", hall)   # the stage's own gradient runs on, as in 1.1.0


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RefreshTest(unittest.TestCase):
    BOARD = sample_board([sample_run()])
    SERVE = """fetch = async (url, init) => {{ __fetches.push([String(url), init || {{}}]); return {{ ok: true, text: async () => JSON.stringify({{ ...{board}, generated: String(Math.random()) }}) }} }}"""

    def test_generated_alone_does_not_count_as_a_change(self):
        a, b = self.BOARD, {**self.BOARD, "generated": "later"}
        self.assertTrue(run_page(f"return boardSig({json.dumps(json.dumps(a))}) === boardSig({json.dumps(json.dumps(b))})"))

    def test_an_unchanged_board_is_drawn_once(self):
        n = run_page(self.SERVE.format(board=json.dumps(self.BOARD)) + """
          let calls = 0; const real = renderProgramme; renderProgramme = () => { calls++; real() }
          await poll(); await poll(); await poll(); return calls""")
        self.assertEqual(n, 1)

    def test_polls_send_the_dashboard_header(self):
        sent = run_page(self.SERVE.format(board=json.dumps(self.BOARD)) + "; await poll(); return __fetches.map(([u, i]) => [u, (i.headers || {})['X-Chief-Dashboard']])")
        self.assertTrue(sent)
        self.assertTrue(all(h == "1" for _, h in sent))

    def test_scroll_survives_a_redraw(self):
        top = run_page(f"""active = {json.dumps(self.BOARD)}; openKey = 'r1@abc'
          const body = __el(); body.scrollTop = 500; __reg['.s-body'] = [body]
          renderScore({{ force: true }}); return body.scrollTop""")
        self.assertEqual(top, 500)

    def test_a_redraw_waits_while_the_user_scrolls(self):
        out = run_page(f"""active = {json.dumps(self.BOARD)}; openKey = 'r1@abc'; renderScore({{ force: true }})
          active.runs[0].title = 'Changed'; $('score').__fire('wheel'); $('score').innerHTML = 'MARK'
          renderScore(); const during = $('score').innerHTML
          lastInteract -= 5000; renderScore(); return [during, pendingScore, $('score').innerHTML.includes('Changed')]""")
        self.assertEqual(out, ["MARK", False, True])

    def test_a_held_pointer_defers_the_redraw(self):
        out = run_page(f"""active = {json.dumps(self.BOARD)}; openKey = 'r1@abc'; renderScore({{ force: true }})
          active.runs[0].title = 'Changed'; $('score').__fire('pointerdown'); lastInteract -= 5000; $('score').innerHTML = 'MARK'
          renderScore(); return [$('score').innerHTML, pendingScore]""")
        self.assertEqual(out, ["MARK", True])

    def test_a_stale_or_released_pointer_no_longer_defers(self):
        out = run_page(f"""active = {json.dumps(self.BOARD)}; openKey = 'r1@abc'; renderScore({{ force: true }})
          active.runs[0].title = 'Changed'; $('score').__fire('pointerdown'); lastInteract -= 5000; pointerDownAt -= 11000
          renderScore(); const stale = $('score').innerHTML.includes('Changed')
          active.runs[0].title = 'Again'; $('score').__fire('pointerdown'); lastInteract -= 5000; document.__fire('mouseup')
          renderScore(); return [stale, $('score').innerHTML.includes('Again')]""")
        self.assertEqual(out, [True, True])

    def test_a_deferred_redraw_lands_on_a_quiet_poll_with_an_unchanged_board(self):
        fixed = json.dumps(self.BOARD)
        out = run_page(f"""fetch = async () => ({{ ok: true, status: 200, text: async () => JSON.stringify({fixed}) }})
          await poll(); openKey = 'r1@abc'; renderScore({{ force: true }})
          active.runs[0].title = 'Changed'; $('score').__fire('wheel'); $('score').innerHTML = 'MARK'
          await poll(); const during = $('score').innerHTML
          lastInteract -= 5000; await poll(); return [during, pendingScore, $('score').innerHTML.includes('CSV export')]""")
        self.assertEqual(out, ["MARK", False, True])

    def test_a_403_asks_for_a_reload(self):
        text = run_page("""fetch = async () => ({ ok: false, status: 403, text: async () => 'This page is out of date — reload it.' })
          await poll(); return $('status').textContent""")
        self.assertEqual(text, "This page is out of date — reload it.")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LinkTest(unittest.TestCase):
    def serve(self, active, archived=None):
        a, b = json.dumps(active), json.dumps(archived or sample_board([]))
        return f"fetch = async (u, i) => {{ __fetches.push([String(u), i || {{}}]); return {{ ok: true, status: 200, text: async () => JSON.stringify(String(u).includes('archived') ? {b} : {a}) }} }};"

    def test_links_resolve_like_the_server(self):
        a, b = sample_run(key="r1@aaa"), sample_run(key="r1@bbb", repo="/y/other")
        out = run_page(f"const rs = {json.dumps([a, b])}; return [resolveLinked('r1@bbb', rs).run.key, resolveLinked('r1', rs).ambiguous.length, !!resolveLinked('zz', rs).missing, resolveLinked('r1', [rs[0]]).run.key]")
        self.assertEqual(out, ["r1@bbb", 2, True, "r1@aaa"])

    def test_an_unknown_link_says_run_not_found(self):
        out = run_page(f"params.set('run', 'nope@x'); {self.serve(sample_board([sample_run()]))} await poll(); return [$('missing').innerHTML, $('prog').innerHTML.includes('CSV export')]")
        self.assertIn("run not found", out[0])
        self.assertIn("nope@x", out[0])
        self.assertTrue(out[1])

    def test_an_archived_link_is_found(self):
        key, sent = run_page(f"params.set('run', 'old@x'); {self.serve(sample_board([]), sample_board([sample_run(key='old@x', archived=True)]))} await poll(); return [openKey, __fetches.filter(([u]) => u.includes('view=archived')).map(([, i]) => (i.headers || {{}})['X-Chief-Dashboard'])]")
        self.assertEqual(key, "old@x")
        self.assertEqual(sent, ["1"])

    def test_an_ambiguous_id_offers_each_checkout(self):
        runs = [sample_run(key="r1@aaa"), sample_run(key="r1@bbb", repo="/y/other")]
        html = run_page(f"params.set('run', 'r1'); {self.serve(sample_board(runs))} await poll(); return $('missing').innerHTML")
        self.assertIn('data-run="r1@aaa"', html)
        self.assertIn('data-run="r1@bbb"', html)

    def test_the_notice_is_written_into_missing_only(self):
        out = run_page(f"params.set('run', 'nope@x'); {self.serve(sample_board([sample_run()]))} await poll(); return [$('headline').innerHTML, $('notices').innerHTML, $('missing').innerHTML.includes('data-dismiss-missing')]")
        self.assertNotIn("nope@x", out[0])
        self.assertNotIn("nope@x", out[1])
        self.assertTrue(out[2])

    def test_dismiss_clears_the_notice_and_the_query(self):
        out = run_page(f"""params.set('run', 'nope@x'); {self.serve(sample_board([sample_run()]))} await poll()
          let url = null; history.replaceState = (a, b, u) => {{ url = u }}
          const btn = __el(); btn.dataset.dismissMissing = ''
          document.__fire('click', {{ target: Object.assign(__el(), {{ closest: (s) => (s === '[data-dismiss-missing]' ? btn : null) }}) }})
          await new Promise((r) => setTimeout(r, 0)); return [$('missing').innerHTML, url]""")
        self.assertEqual(out, ["", "/"])

    def test_the_link_is_tried_once_after_success_and_retried_after_failure(self):
        board = json.dumps(sample_board([sample_run()]))
        out = run_page(f"""params.set('run', 'nope@x'); {self.serve(sample_board([sample_run()]))} await poll()
          $('missing').innerHTML = ''; lastActive = ''; await poll(); const afterSuccess = $('missing').innerHTML
          linkedOnce = false; lastActive = ''; let fail = true
          fetch = async (u) => {{ if (String(u).includes('archived') && fail) return {{ ok: true, status: 200, text: async () => 'not json' }}; return {{ ok: true, status: 200, text: async () => String(u).includes('archived') ? '{json.dumps(sample_board([]))}' : JSON.stringify({board}) }} }}
          await poll(); const afterFailure = [linkedOnce, $('missing').innerHTML]
          fail = false; await poll(); return [afterSuccess, afterFailure, linkedOnce, $('missing').innerHTML.includes('run not found')]""")
        self.assertEqual(out, ["", [False, ""], True, True])

    def test_a_403_on_the_archive_check_asks_for_a_reload(self):
        board = json.dumps(sample_board([sample_run()]))
        out = run_page(f"""fetch = async (u) => String(u).includes('archived') ? {{ ok: false, status: 403, text: async () => 'no' }} : {{ ok: true, status: 200, text: async () => JSON.stringify({board}) }}
          params.set('run', 'nope@x'); await poll(); return [$('status').textContent, $('missing').innerHTML]""")
        self.assertEqual(out, ["This page is out of date — reload it.", ""])

    def test_the_count_is_plural_aware_and_absent_without_repos(self):
        def notice(repos):
            board = {**sample_board([sample_run()]), "repos": repos}
            return run_page(f"params.set('run', 'nope@x'); {self.serve(board)} await poll(); return $('missing').innerHTML")
        self.assertIn("1 checkout on this machine", notice([{"repo": "/x/repo", "name": "repo", "git": None}]))
        self.assertIn("2 checkouts on this machine", notice([{"repo": "/x/repo", "name": "repo", "git": None}, {"repo": "/y/o", "name": "o", "git": None}]))
        bare = {k: v for k, v in sample_board([sample_run()]).items() if k != "repos"}  # the rest of the page assumes repos, so call openLinked directly
        absent = run_page(f"active = {json.dumps(bare)}; {self.serve(bare)} await openLinked('nope@x'); return $('missing').innerHTML")
        self.assertIn("run not found", absent)
        self.assertNotRegex(absent, r"\d+ checkout")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ContextTest(unittest.TestCase):
    def test_token_counts_are_short(self):
        self.assertEqual(run_page("return [kTokens(950), kTokens(84210), kTokens(1250000), kTokens(null)]"), ["950", "84k", "1.25M", "—"])

    def test_context_sits_before_the_cost(self):
        agent = {"id": "claude-a", "kind": "implement", "role": "implement", "task": "1", "provider": "claude", "model": "claude-sonnet-5-5",
                 "status": "done", "cost_usd": 1.5, "context": {"last": 84210, "peak": 120000}, "acts": []}
        html = run_page(f"active = {json.dumps(sample_board([sample_run(agents=[agent])]))}; openKey = 'r1@abc'; renderScore({{ force: true }}); return $('score').innerHTML")
        row = html[html.index('data-agent="claude-a"'):]
        self.assertLess(row.index("84k ctx"), row.index("$1.50"))
        self.assertIn("(peak 120k)", html)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SessionTest(unittest.TestCase):
    CHIEF = {"id": "chief", "kind": "chief", "role": "chief", "task": "-", "provider": "claude", "model": "claude-opus-5-5",
             "status": "running", "session_id": "1a2b3c4d-aaaa", "has_transcript": True, "acts": []}
    WORKER = {"id": "claude-w1", "kind": "implement", "role": "implement", "task": "2", "provider": "claude", "model": "claude-sonnet-5-5",
              "status": "running", "session_id": "1a2b3c4d-aaaa", "agent_id": "w1", "has_transcript": True, "acts": []}

    def programme(self, runs):
        return run_page(f"active = {json.dumps(sample_board(runs))}; renderProgramme(); return $('prog').innerHTML")

    def score(self, agent_id, **run):
        board = sample_board([sample_run(agents=[self.CHIEF, self.WORKER], **run)])
        return run_page(f"active = {json.dumps(board)}; openKey = 'r1@abc'; openAgent = '{agent_id}'; renderScore({{ force: true }}); return $('score').innerHTML")

    def test_cards_from_one_chat_are_marked_as_siblings(self):
        html = self.programme([sample_run(key="a@x", session_id="1a2b3c4d-aaaa"),
                               sample_run(key="b@x", session_id="1a2b3c4d-aaaa", title="Follow-up"),
                               sample_run(key="c@x", session_id="99999999-bbbb")])
        self.assertEqual(html.count("1a2b3c4d · 2 runs"), 2)
        self.assertIn("◆ 99999999", html)
        self.assertNotIn("99999999 · ", html)

    def test_session_counts_cover_the_given_runs(self):
        self.assertEqual(run_page("return sessionCounts([{session_id: 'a'}, {session_id: 'a'}, {session_id: 'b'}, {session_id: null}])"),
                         {"a": 2, "b": 1})

    def test_siblings_share_a_color(self):
        html = self.programme([sample_run(key="a@x", session_id="1a2b3c4d-aaaa"), sample_run(key="b@x", session_id="1a2b3c4d-aaaa")])
        colors = re.findall(r'--sess:(#[0-9A-Fa-f]{6})', html)
        self.assertEqual(len(colors), 2)
        self.assertEqual(colors[0], colors[1])

    def test_a_run_without_a_session_has_no_badge(self):
        self.assertNotIn('class="sess', self.programme([sample_run(session_id=None)]))

    def test_a_differing_chat_title_is_shown(self):
        self.assertIn("chat: Fix filed issues", self.programme([sample_run(session_id="1a2b3c4d", chat_title="Fix filed issues")]))
        self.assertNotIn("chat: ", self.programme([sample_run(session_id="1a2b3c4d", chat_title="CSV export")]))

    def test_chat_title_and_session_are_escaped(self):
        html = self.programme([sample_run(session_id='1"><b>x', chat_title="<img src=x onerror=1>")])
        self.assertNotIn("<img", html)
        self.assertNotIn('"><b>', html)

    def test_resume_command_quotes_the_cwd(self):
        self.assertEqual(run_page("return resumeCommand({cwd: \"/a/My Project/it's\"}, 'abc')"),
                         "cd '/a/My Project/it'\\''s' && claude --resume abc")
        self.assertEqual(run_page("return resumeCommand({cwd: null, repo: '/r'}, 'abc')"), "cd '/r' && claude --resume abc")

    def test_the_page_accepts_the_session_ids_the_server_accepts(self):
        # the server's pattern is ^[A-Za-z0-9-]{1,80}$: no underscore, at most 80 characters
        self.assertEqual(run_page("return resumeCommand({cwd: '/r'}, 'a'.repeat(80))"), "cd '/r' && claude --resume " + "a" * 80)
        self.assertIsNone(run_page("return resumeCommand({cwd: '/r'}, 'a'.repeat(81))"))
        self.assertIsNone(run_page("return resumeCommand({cwd: '/r'}, 'a_b')"))
        self.assertIsNone(run_page("return resumeCommand({cwd: '/r'}, '')"))

    def test_a_session_id_that_is_not_a_plain_token_gets_no_command_or_link(self):
        self.assertIsNone(run_page("return resumeCommand({cwd: '/r'}, 'a;rm -rf ~')"))
        chief = {**self.CHIEF, "session_id": "a;rm -rf ~"}
        board = sample_board([sample_run(agents=[chief], cwd="/x/repo")])
        html = run_page(f"active = {json.dumps(board)}; openKey = 'r1@abc'; openAgent = 'chief'; renderScore({{ force: true }}); return $('score').innerHTML")
        for banned in ("claude --resume", "vscode://", "data-copy", "rm -rf ~"):
            self.assertNotIn(banned, html)

    def test_the_chief_row_links_to_its_chat(self):
        board = sample_board([sample_run(agents=[{**self.CHIEF, "status": "stopped"}, self.WORKER], cwd="/x/My repo")])
        html = run_page(f"active = {json.dumps(board)}; openKey = 'r1@abc'; openAgent = 'chief'; renderScore({{ force: true }}); return $('score').innerHTML")
        self.assertNotIn("vscode://", html)
        self.assertIn('data-open-chat="r1@abc"', html)
        self.assertIn("claude --resume 1a2b3c4d-aaaa", html)
        self.assertIn('data-transcript="chief"', html)

    def test_a_running_worker_says_how_to_steer_it(self):
        html = self.score("claude-w1")
        self.assertIn("SendMessage to w1", html)
        self.assertIn('data-transcript="claude-w1"', html)
        self.assertNotIn("vscode://", html)

    def test_the_transcript_is_fetched_with_the_header(self):
        sent = run_page("loadTranscript('r1@abc', 'claude-w1'); return __fetches.filter(([u]) => u.startsWith('/transcript')).map(([u, i]) => [u, i.headers['X-Chief-Dashboard']])")
        self.assertEqual(sent, [["/transcript?run=r1%40abc&agent=claude-w1", "1"]])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpenChatPageTest(unittest.TestCase):
    NOW = 1_800_000_000
    CHIEF = {**SessionTest.CHIEF, "status": "stopped", "active": NOW - 3600}

    def control(self, chief=None, **run):
        r = sample_run(**{"session_id": "1a2b3c4d-aaaa", **run})
        return run_page(f"return openChatControl({json.dumps(r)}, {json.dumps(chief or self.CHIEF)}, {self.NOW})")

    def test_a_quiet_chief_gets_the_button(self):
        html = self.control()
        self.assertIn('data-open-chat="r1@abc"', html)
        self.assertIn(">open chat</button>", html)

    def test_a_recent_chat_gets_a_hint_whatever_its_status(self):
        for status in ("running", "stopped", "done"):
            html = self.control({**self.CHIEF, "status": status, "active": self.NOW - 60})
            self.assertIn("chat may be open elsewhere", html)
            self.assertNotIn("data-open-chat", html)

    def test_no_openable_chat_no_control(self):
        self.assertEqual(self.control({**self.CHIEF, "session_id": None}), "")
        for bad in ("rm -rf ~", "-abcd1234"):
            self.assertEqual(self.control({**self.CHIEF, "session_id": bad}), "")

    def test_the_chief_panel_carries_it_and_no_vscode_link(self):
        board = sample_board([sample_run(session_id="1a2b3c4d-aaaa", agents=[self.CHIEF])])
        html = run_page(f"Date.now = () => {self.NOW * 1000}; active = {json.dumps(board)}; openKey = 'r1@abc'; openAgent = 'chief'; renderScore({{ force: true }}); return $('score').innerHTML")
        side = html.split('class="s-side"')[1]
        self.assertIn('data-open-chat="r1@abc"', side)
        self.assertIn("copy resume command", side)
        self.assertNotIn("vscode://", html)
        self.assertNotIn("data-open-chat", html.split('class="s-side"')[0])   # not in the header

    def test_it_posts_only_the_key_and_disables_the_button_meanwhile(self):
        out = run_page("const b = { dataset: { openChat: 'r1@abc' }, disabled: false, textContent: 'open chat' }; openChat(b); return [__fetches.filter(([u]) => u === '/open-chat'), b.disabled]")
        self.assertEqual(out[0], [["/open-chat", {"method": "POST", "headers": {"Content-Type": "application/json", "X-Chief-Dashboard": "1"}, "body": '{"key":"r1@abc"}'}]])
        self.assertTrue(out[1])

    def test_a_redraw_during_the_request_keeps_the_button_disabled(self):
        html = run_page(f"const b = {{ dataset: {{ openChat: 'r1@abc' }}, disabled: false }}; openChat(b); return openChatControl({json.dumps(sample_run(session_id='1a2b3c4d-aaaa'))}, {json.dumps(self.CHIEF)}, {self.NOW})")
        self.assertRegex(html, r'<button[^>]*data-open-chat="r1@abc"[^>]*disabled')

    def open_panel(self):
        """JS that opens the chief panel of run r1@abc (its open-chat button drawn) and defines the clicked button B."""
        board = sample_board([sample_run(session_id="1a2b3c4d-aaaa", agents=[self.CHIEF])])
        return (f"Date.now = () => {self.NOW * 1000}; active = {json.dumps(board)}; openKey = 'r1@abc'; openAgent = 'chief'; "
                "renderScore({ force: true }); const b = { dataset: { openChat: 'r1@abc' }, disabled: false }; ")

    @staticmethod
    def links(panel):
        """The chief panel's .links area: the command, the buttons and the open-chat note."""
        return panel.split('class="s-side"')[1].split('class="links"')[1].split("</div>")[0]

    @staticmethod
    def reply(status, body):
        return f"fetch = async () => ({{ ok: {str(status == 200).lower()}, status: {status}, text: async () => {json.dumps(body)} }}); "

    def test_a_409_shows_the_servers_error_in_the_panel_and_a_redraw_keeps_it(self):
        out = run_page(self.open_panel() + self.reply(409, '{"error":"live <i>"}')
                       + "await openChat(b); const after = $('score').innerHTML; renderScore({ force: true }); return [after, $('score').innerHTML, $('status').textContent, b.disabled, openingChat]")
        for panel in out[:2]:
            links = self.links(panel)
            self.assertIn("may be open elsewhere", links)
            self.assertIn("live &lt;i&gt;", links)     # the server's error, escaped
            self.assertNotIn("<i>", links)
        self.assertEqual(out[2], "")                    # nothing under the toolbar, which the modal covers
        self.assertFalse(out[3])
        self.assertIsNone(out[4])

    def test_every_failure_shows_the_servers_error_beside_the_button(self):
        for status, body, shown in ((404, '{"error":"run not found"}', "Open chat failed: run not found"),
                                    (500, '{"error":"Terminal did not open: boom"}', "Open chat failed: Terminal did not open: boom"),
                                    (501, '{"error":"Open chat needs macOS Terminal"}', "Open chat failed: Open chat needs macOS Terminal"),
                                    (415, "plain words", "Open chat failed: plain words")):
            with self.subTest(status=status):
                links = self.links(run_page(self.open_panel() + self.reply(status, body) + "await openChat(b); renderScore({ force: true }); return $('score').innerHTML"))
                self.assertIn(shown, links)
                self.assertIn('data-open-chat="r1@abc"', links)

    def test_a_stopped_server_and_a_success_are_shown_in_the_panel(self):
        out = run_page(self.open_panel() + "fetch = async () => { throw new TypeError('Failed to fetch') }; await openChat(b); const down = $('score').innerHTML; "
                       + self.reply(200, "{}") + "await openChat(b); return [down, $('score').innerHTML, $('status').textContent, openingChat]")
        self.assertIn("Open chat failed: Failed to fetch", self.links(out[0]))
        self.assertIn("Opened the chief chat in Terminal.", self.links(out[1]))
        self.assertNotIn("Failed to fetch", out[1])    # the new attempt replaced the old note
        self.assertEqual(out[2], "")
        self.assertIsNone(out[3])

    def test_a_new_attempt_or_closing_the_panel_clears_the_note(self):
        out = run_page(self.open_panel() + self.reply(500, '{"error":"boom"}') + "await openChat(b); "
                       "fetch = () => new Promise(() => {}); openChat(b); renderScore({ force: true }); const retrying = $('score').innerHTML; "
                       "openingChat = null; closeScore(); openScore('r1@abc'); return [retrying, $('score').innerHTML]")
        self.assertNotIn("boom", out[0])
        self.assertNotIn("boom", out[1])
        self.assertIn('data-open-chat="r1@abc"', out[1])

    def test_the_visible_button_is_enabled_once_the_request_settles_after_a_redraw(self):
        out = run_page(self.open_panel() + "let release; fetch = () => new Promise((r) => { release = r }); const done = openChat(b); "
                       "renderScore({ force: true }); const during = $('score').innerHTML; "
                       "release({ ok: true, status: 200, text: async () => '{}' }); await done; return [during, $('score').innerHTML]")
        self.assertRegex(self.links(out[0]), r'<button[^>]*data-open-chat="r1@abc"[^>]*disabled')
        self.assertNotRegex(self.links(out[1]), r'<button[^>]*data-open-chat="r1@abc"[^>]*disabled')
        self.assertIn("Opened the chief chat in Terminal.", self.links(out[1]))

    def test_an_answer_after_the_panel_closed_is_dropped(self):
        out = run_page(self.open_panel() + "let release; fetch = () => new Promise((r) => { release = r }); const done = openChat(b); closeScore(); "
                       "release({ ok: false, status: 500, text: async () => '{\"error\":\"late\"}' }); await done; openScore('r1@abc'); return [$('score').innerHTML, b.disabled, openingChat]")
        self.assertNotIn("late", out[0])
        self.assertFalse(out[1])
        self.assertIsNone(out[2])

    def test_a_disabled_chip_looks_inactive_and_does_not_light_up_on_hover(self):
        css = page_css()
        self.assertRegex(css, r"\.chip:disabled\s*\{[^}]*opacity:[^}]*cursor:\s*default")
        self.assertRegex(css, r"\.chip:not\(:disabled\):hover\s*\{[^}]*border-color:\s*var\(--gold\)")
        self.assertNotRegex(css, r"\.chip:hover\s*\{")
        self.assertRegex(css, r"\.links \.note\s*\{[^}]*flex:\s*1 1 100%")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class CacheBadgeTest(unittest.TestCase):
    NOW = 1_800_000_000_000  # ms

    def state(self, last_ago, ttl, **run):
        """cacheState for a cache last touched `last_ago` seconds before NOW."""
        cache = None if ttl is None else {"last_at": self.NOW // 1000 - last_ago, "ttl_s": ttl}
        return run_page(f"return cacheState({json.dumps(sample_run(cache=cache, **run))}, {self.NOW})")

    def test_a_cache_inside_its_ttl_is_hot_with_the_minutes_left(self):
        self.assertEqual(self.state(3600 - 42 * 60, 3600), {"state": "hot", "text": "cache hot · 42m left"})
        self.assertEqual(self.state(300 - 150, 300), {"state": "hot", "text": "cache hot · 3m left"})

    def test_minutes_round_up_and_never_read_zero(self):
        self.assertEqual(self.state(300 - 1, 300)["text"], "cache hot · 1m left")
        self.assertEqual(self.state(300 - 61, 300)["text"], "cache hot · 2m left")
        self.assertEqual(self.state(300 - 120, 300)["text"], "cache hot · 2m left")  # exactly 2 minutes
        self.assertEqual(self.state(300 - 121, 300)["text"], "cache hot · 3m left")

    def test_a_cache_past_its_ttl_is_expired(self):
        self.assertEqual(self.state(300, 300), {"state": "expired", "text": "cache expired"})
        self.assertEqual(self.state(9000, 3600)["state"], "expired")

    def test_no_cache_data_or_a_finished_run_shows_nothing(self):
        self.assertIsNone(self.state(10, None))
        self.assertIsNone(run_page(f"return cacheState({json.dumps(sample_run(cache={'last_at': None, 'ttl_s': 300}))}, {self.NOW})"))
        self.assertIsNone(run_page(f"return cacheState({json.dumps(sample_run(cache={'last_at': 1, 'ttl_s': None}))}, {self.NOW})"))
        self.assertIsNone(run_page(f"return cacheState({json.dumps(sample_run())}, {self.NOW})"))  # no cache key at all
        self.assertIsNone(self.state(10, 300, phase="done"))
        self.assertIsNone(self.state(10, 300, ended="2026-11-17T01:00:00Z"))
        self.assertEqual(run_page("return cacheBadge({key: 'k', phase: 'done', cache: {last_at: 1, ttl_s: 300}})"), "")

    def test_the_badge_is_on_the_card_and_in_the_score_header(self):
        cache = {"last_at": int(self.NOW // 1000) - 60, "ttl_s": 3600}
        board = json.dumps(sample_board([sample_run(cache=cache)]))
        card = run_page(f"Date.now = () => {self.NOW}; active = {board}; renderProgramme(); return $('prog').innerHTML")
        score = run_page(f"Date.now = () => {self.NOW}; active = {board}; openKey = 'r1@abc'; renderScore({{ force: true }}); return $('score').innerHTML")
        for html in (card, score):
            self.assertEqual(html.count('data-cache="r1@abc"'), 1)
            self.assertIn("cache hot · 59m left", html)
            self.assertIn("full price", html)
        self.assertNotIn("data-cache", run_page(f"active = {json.dumps(sample_board([sample_run()]))}; renderProgramme(); return $('prog').innerHTML"))

    def test_the_tick_patches_the_text_in_place_and_redraws_nothing(self):
        out = run_page(f"""const cache = {{ last_at: {self.NOW // 1000}, ttl_s: 300 }}
          active = {json.dumps(sample_board([sample_run()]))}; active.runs[0].cache = cache
          const badge = __el(); badge.setAttribute('data-cache', 'r1@abc'); __reg['[data-cache]'] = [badge]
          let redraws = 0; for (const name of ['renderScore', 'renderProgramme', 'renderStage']) {{ const real = globalThis[name]; globalThis[name] = (...a) => {{ redraws++; return real(...a) }} }}
          $('score').innerHTML = 'MARK'
          tickCache({self.NOW}); const hot = [badge.textContent, badge.className]
          tickCache({self.NOW + 200_000}); const later = badge.textContent
          tickCache({self.NOW + 301_000}); const gone = [badge.textContent, badge.className]
          active.runs[0].phase = 'done'; tickCache({self.NOW}); const hidden = badge.hidden
          return [hot, later, gone, hidden, redraws, $('score').innerHTML]""")
        self.assertEqual(out, [["cache hot · 5m left", "cache hot"], "cache hot · 2m left", ["cache expired", "cache expired"], True, 0, "MARK"])

    def test_poll_refreshes_the_badge_on_an_unchanged_board(self):
        cache = {"last_at": self.NOW // 1000, "ttl_s": 3600}
        fixed = json.dumps(sample_board([sample_run(cache=cache)]))
        out = run_page(f"""fetch = async () => ({{ ok: true, status: 200, text: async () => JSON.stringify({fixed}) }})
          const badge = __el(); badge.setAttribute('data-cache', 'r1@abc'); __reg['[data-cache]'] = [badge]
          let calls = 0; const real = renderProgramme; renderProgramme = () => {{ calls++; real() }}
          Date.now = () => {self.NOW}; await poll(); const first = badge.textContent
          Date.now = () => {self.NOW} + 30 * 60 * 1000; await poll(); return [first, badge.textContent, calls]""")
        self.assertEqual(out, ["cache hot · 60m left", "cache hot · 30m left", 1])

    def test_the_badge_adds_no_timer(self):
        self.assertEqual(PAGE.read_text().count("setInterval("), 1)
        self.assertNotIn("setTimeout(", PAGE.read_text().split("function tickCache")[1].split("\nfunction ")[0])


ROBOTS = PAGE.parent / "robots.svg"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BoardColumnsTest(unittest.TestCase):
    def test_a_busy_column_stacks_its_cards(self):
        css, page = page_css(), PAGE.read_text()
        self.assertRegex(css, r"\.cards\s*\{\s*display:\s*grid;\s*gap:\s*10px;?\s*\}")
        self.assertNotIn("prog-wrap", page)
        self.assertEqual(run_page("return typeof colVars"), "undefined")
        runs = [sample_run(key=f"k{n}@x", id=f"k{n}", phase="implement") for n in range(4)]
        html = run_page(f"active = {json.dumps(sample_board(runs))}; renderProgramme(); return $('prog').innerHTML")
        self.assertNotIn("--n:", html)
        self.assertEqual(html.count('<div class="cards">'), 1)
        self.assertIn('class="col rail"', html)


def agent(id, role, status="running", provider="claude", model="claude-sonnet-5-5", kind=None):
    return {"id": id, "role": role, "kind": kind or role, "provider": provider, "model": model, "status": status}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StageFrameTest(unittest.TestCase):
    def rule(self, sel):
        return re.search(re.escape(sel) + r"\s*\{([^}]*)\}", page_css()).group(1)

    def test_the_stage_box_is_a_dashboard_panel(self):
        ens = self.rule(".ens")
        self.assertRegex(ens, r"border:\s*1\.5px solid var\(--line\)")
        self.assertRegex(ens, r"border-radius:\s*13px;")          # one value: even corners
        self.assertIn("linear-gradient(180deg, #21160D, var(--night-2))", ens)
        self.assertIn("overflow: hidden", ens)
        self.assertRegex(self.rule(".ens .arch"), r"border-bottom:\s*1\.5px solid var\(--line\)")
        self.assertRegex(self.rule(".ens .apron"), r"border-top:\s*1\.5px solid var\(--line\)")
        self.assertRegex(self.rule(".ens .plate"), r"border:\s*1\.5px solid var\(--line\)")
        for old in ("#7A5520", "#8A6224; }", "#9A6F2A", "18px 18px 5px 5px"):
            self.assertNotIn(old, page_css())

    def test_notes_fill_only_heads_and_flags(self):
        shapes = run_page("return NOTE_SHAPES")
        self.assertEqual(len(shapes), 3)
        for s in shapes:
            self.assertNotRegex(s["line"], r"[aAZz]")          # stems only: no heads, nothing closed
            for part in (p.strip() for p in re.split(r"(?=M)", s["fill"]) if p.strip()):
                self.assertTrue(part.endswith("Z") or re.search(r"\ba\b", part), part)  # closed, or a head drawn with arcs
        emit = PAGE.read_text().split("function emit(")[1].split("\n  function ")[0]
        self.assertIn("fill: 'none'", emit)
        self.assertIn(".line", emit)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StageCollapseTest(unittest.TestCase):
    """The stage can be hidden: collapsed means no animation work at all, and the rest of the page carries on."""
    NOW = 1_800_000_000_000  # ms
    KEY = "chief.stage.collapsed"
    # Runs before the page loads. Counts every timer, frame request, interval and Web Animation the page asks for (nothing
    # fires by itself; a test fires what it wants) and gives the stage a lenient fake DOM that counts its writes.
    KIT = r"""
const __probe = { timeout: 0, raf: 0, interval: 0, animate: 0, cancelled: 0, touched: 0, ro: 0 }
const __timers = new Map(), __anims = new Set(), __seen = new Set()
let __next = 1
globalThis.setTimeout = (fn) => { __probe.timeout++; __timers.set(__next, fn); return __next++ }
globalThis.clearTimeout = (id) => { __timers.delete(id) }
globalThis.setInterval = () => { __probe.interval++; return __next++ }
globalThis.requestAnimationFrame = () => { __probe.raf++; return __next++ }
globalThis.matchMedia = () => ({ matches: false })
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
globalThis.ResizeObserver = class { constructor() { __probe.ro++ } observe(n) { __seen.add(n) } unobserve(n) { __seen.delete(n) } }
const __mk = () => {
  const attrs = {}, classes = new Set(), subs = {}, touch = () => { __probe.touched++ }
  let html = '', text = ''
  const n = {
    style: { setProperty() {} }, dataset: {}, children: [], parentNode: null, isConnected: false, title: '',
    get innerHTML() { return html }, set innerHTML(v) { if (v !== html) touch(); html = v },
    get textContent() { return text }, set textContent(v) { if (v !== text) touch(); text = v },
    getAttribute: (k) => attrs[k] ?? null, setAttribute: (k, v) => { touch(); attrs[k] = String(v) },
    classList: { contains: (c) => classes.has(c), toggle: (c, on) => { touch(); if (on) classes.add(c); else classes.delete(c) },
      add: (...c) => { touch(); c.forEach((x) => classes.add(x)) }, remove: (...c) => { touch(); c.forEach((x) => classes.delete(x)) } },
    querySelector: (sel) => (subs[sel] ||= __mk()), appendChild(c) { n.insertBefore(c, null) },
    insertBefore(c, ref) { touch(); if (c.parentNode) c.remove(); const i = ref ? n.children.indexOf(ref) : n.children.length; n.children.splice(i, 0, c); c.parentNode = n; c.isConnected = true },
    remove() { touch(); if (n.parentNode) { n.parentNode.children.splice(n.parentNode.children.indexOf(n), 1); n.parentNode = null; n.isConnected = false } },
    getBoundingClientRect: () => ({ left: 0 }), getBBox() {}, insertAdjacentHTML() {},
    animate() { __probe.animate++; const a = { cancel() { __probe.cancelled++; __anims.delete(a) } }; __anims.add(a); return a },
  }
  return n
}
document.createElement = __mk; document.createElementNS = __mk
const __row = __mk()
__row.getAnimations = () => [...__anims]
__byId['stage-row'] = __row
"""
    STORAGE = {
        "none": "",  # Node has no localStorage: the page's first read throws a ReferenceError
        "empty": "const __s = new Map()",
        "collapsed": "const __s = new Map([['chief.stage.collapsed', '1']])",
        "open": "const __s = new Map([['chief.stage.collapsed', '0']])",
        "junk": "const __s = new Map([['chief.stage.collapsed', 'maybe']])",
    }
    STORE = """Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: (k) => (__s.has(k) ? __s.get(k) : null), setItem: (k, v) => { __s.set(k, String(v)) }, removeItem: (k) => { __s.delete(k) } } })"""
    BROKEN = {
        "denied": "Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('SecurityError') } })",
        "full": "Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null, setItem() { throw new Error('QuotaExceededError') } } })",
        "unreadable": "Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem() { throw new Error('boom') }, setItem() { throw new Error('boom') } } })",
    }
    # Runs after the page loads: board feed, a click on the toggle, and a snapshot of everything the stage costs.
    SETUP = """
const NOW = 1800000000000; Date.now = () => NOW
let current = null
fetch = async () => ({ ok: true, status: 200, text: async () => JSON.stringify(current) })
const click = () => $('stage-toggle').__fire('click')
const state = () => ({ hidden: $('stage-hall').hidden, expanded: $('stage-toggle').getAttribute('aria-expanded'), label: $('stage-toggle').textContent })
const snap = () => ({ kids: __row.children.length, timers: __timers.size, anims: __anims.size, observed: __seen.size, ...__probe })
const titles = () => __row.children.map((c) => c.querySelector('h3').textContent)
"""

    def iso(self, ago_s):
        return datetime.fromtimestamp(self.NOW / 1000 - ago_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def runs(self, n, players=2):
        return [sample_run(key=f"r{i}@x", id=f"r{i}", title=f"Run {i}", last_activity=self.iso(10),
                           agents=[agent("chief", "chief", kind="chief")] + [agent(f"r{i}p{k}", "implement") for k in range(players)])
                for i in range(n)]

    def board(self, n, players=2):
        return json.dumps(sample_board(self.runs(n, players)))

    def load(self, snippet, storage="empty"):
        if storage in self.BROKEN:
            before = self.KIT + self.BROKEN[storage]
        else:
            before = self.KIT + self.STORAGE[storage] + ("\n" + self.STORE if storage != "none" else "")
        return run_page(self.SETUP + snippet, before=before)

    def test_the_stage_is_open_by_default(self):
        for storage in ("none", "empty", "junk", "open", "denied"):
            with self.subTest(storage=storage):
                out = self.load("return state()", storage)
                self.assertEqual(out, {"hidden": False, "expanded": "true", "label": "hide stage"})

    def test_the_toggle_collapses_the_stage_and_remembers_it(self):
        out = self.load("""const open = state(); click(); const shut = state(); const saved = __s.get('chief.stage.collapsed')
          click(); return [open, shut, saved, state(), __s.get('chief.stage.collapsed')]""")
        self.assertEqual(out[0], {"hidden": False, "expanded": "true", "label": "hide stage"})
        self.assertEqual(out[1], {"hidden": True, "expanded": "false", "label": "show stage"})
        self.assertEqual(out[2], "1")
        self.assertEqual(out[3], {"hidden": False, "expanded": "true", "label": "hide stage"})
        self.assertEqual(out[4], "0")

    def test_the_stored_choice_is_honoured_on_load(self):
        self.assertEqual(self.load("return state()", "collapsed"), {"hidden": True, "expanded": "false", "label": "show stage"})
        self.assertEqual(self.load("return state()", "open"), {"hidden": False, "expanded": "true", "label": "hide stage"})

    def test_a_stage_collapsed_on_load_is_never_built(self):
        out = self.load(f"""current = {self.board(2)}; await poll(); await poll()
          return [snap(), Stage.mounted(), $('headline').innerHTML]""", "collapsed")
        snap, mounted, headline = out
        self.assertFalse(mounted)
        self.assertEqual((snap["kids"], snap["timers"], snap["anims"], snap["observed"], snap["ro"]), (0, 0, 0, 0, 0))
        self.assertEqual((snap["touched"], snap["timeout"], snap["animate"], snap["raf"]), (0, 0, 0, 0))
        self.assertIn("4 players", headline)  # the header still counts what is on stage

    def test_storage_that_throws_leaves_the_toggle_working(self):
        for storage in self.BROKEN:
            with self.subTest(storage=storage):
                out = self.load(f"""current = {self.board(1)}; await poll(); click(); const shut = state(); const kids = __row.children.length
                  click(); return [shut, kids, state(), __row.children.length]""", storage)
                self.assertEqual(out[0], {"hidden": True, "expanded": "false", "label": "show stage"})
                self.assertEqual(out[1], 0)
                self.assertEqual(out[2], {"hidden": False, "expanded": "true", "label": "hide stage"})
                self.assertEqual(out[3], 1)

    def test_while_collapsed_a_board_refresh_leaves_the_stage_alone(self):
        out = self.load(f"""current = {self.board(1)}; await poll(); const built = snap()
          let renders = 0; const real = Stage.render; Stage.render = (l) => {{ renders++; return real(l) }}
          click(); const shut = snap()
          current = {self.board(3, 3)}; await poll(); await poll(); await poll()
          return {{ built, shut, after: snap(), renders, headline: $('headline').innerHTML, prog: $('prog').innerHTML, where: $('where').textContent }}""")
        self.assertEqual(out["built"]["kids"], 1)
        self.assertGreater(out["built"]["observed"], 0)
        self.assertEqual((out["shut"]["kids"], out["shut"]["timers"], out["shut"]["anims"], out["shut"]["observed"]), (0, 0, 0, 0))
        self.assertEqual(out["after"], out["shut"])  # no stage DOM write, timer, frame, animation or observer after three polls
        self.assertEqual(out["renders"], 0)
        self.assertEqual(out["after"]["raf"], 0)
        self.assertIn("3 ensembles", out["headline"])  # the header, programme and status line keep following the board
        self.assertIn("Run 2", out["prog"])
        self.assertIn("3 runs", out["where"])

    def test_expanding_rebuilds_the_stage_from_the_current_board(self):
        out = self.load(f"""current = {self.board(1)}; await poll(); click()
          current = {self.board(3)}; await poll(); const shut = titles(); click(); const first = snap(); const names = titles()
          return [shut, names, first]""")
        self.assertEqual(out[0], [])
        self.assertEqual(out[1], ["Run 0", "Run 1", "Run 2"])
        self.assertGreater(out[2]["timers"], 0)  # the curtains are opening again
        self.assertGreater(out[2]["observed"], 0)

    def test_collapsing_cancels_every_timer_and_animation_the_stage_had_pending(self):
        a = json.dumps(sample_board([sample_run(key="r0@x", id="r0", title="Run 0", last_activity=self.iso(10),
                                                agents=[agent("chief", "chief", kind="chief"), agent("p0", "implement")])]))
        b = json.dumps(sample_board([sample_run(key="r0@x", id="r0", title="Run 0", last_activity=self.iso(10),
                                                agents=[agent("chief", "chief", kind="chief"), agent("p0", "implement"), agent("p1", "research")])]))
        c = json.dumps(sample_board([sample_run(key="r0@x", id="r0", title="Run 0", last_activity=self.iso(10),
                                                agents=[agent("chief", "chief", kind="chief"), agent("p1", "research")])]))
        out = self.load(f"""current = {a}; await poll()
          current = {b}; await poll()                      // a join: the chief turns to the house (timers, animations)
          current = {c}; await poll()                      // a player walks off (the walk-off timer)
          const busy = snap(); click(); const shut = snap(); const fired = [...__timers.values()].length
          return [busy, shut, fired]""")
        busy, shut, fired = out
        self.assertGreater(busy["timers"], 2)
        self.assertGreater(busy["anims"], 0)
        self.assertEqual((shut["timers"], shut["anims"], shut["observed"], shut["kids"], fired), (0, 0, 0, 0, 0))
        self.assertEqual(shut["animate"], busy["animate"])  # nothing was started on the way out

    def test_expand_collapse_expand_leaves_exactly_one_loop(self):
        out = self.load(f"""current = {self.board(2)}; await poll()
          let renders = 0; const real = Stage.render; Stage.render = (l) => {{ renders++; return real(l) }}
          const first = snap(), shown = titles()
          for (let i = 0; i < 5; i++) {{ click(); click() }}
          const again = snap(); renders = 0
          await poll(); const afterPoll = snap()
          return {{ first, shown, again, names: titles(), renders, afterPoll }}""")
        self.assertEqual(out["shown"], ["Run 0", "Run 1"])
        self.assertEqual(out["names"], ["Run 0", "Run 1"])  # five collapse/expand rounds later: still two stages, not twelve
        for key in ("kids", "timers", "observed"):
            self.assertEqual(out["again"][key], out["first"][key], key)
        self.assertEqual(out["afterPoll"]["timers"], out["again"]["timers"])
        self.assertEqual(out["renders"], 1)  # one stage tick per poll
        self.assertEqual(out["afterPoll"]["interval"], 1)  # the page's own poll timer, and nothing else repeating
        self.assertEqual(out["afterPoll"]["raf"], 0)

    def test_every_timer_the_stage_sets_is_tracked_so_it_can_be_stopped(self):
        stage = PAGE.read_text().split("const Stage = (() => {")[1].split("\n})()")[0]
        self.assertEqual(len(re.findall(r"\bsetTimeout\(", stage)), 1)  # the tracked wrapper; every other call goes through it
        for banned in ("setInterval(", "requestAnimationFrame("):
            self.assertNotIn(banned, stage)

    def test_the_toggle_is_a_real_button_between_the_header_and_the_hall(self):
        stage = PAGE.read_text().split('<section class="stage"')[1].split("</section>")[0]
        button = re.search(r'<button\b[^>]*\bid="stage-toggle"[^>]*>', stage).group(0)
        for attr in ('type="button"', 'class="chip"', 'aria-expanded="true"', 'aria-controls="stage-hall"'):
            self.assertIn(attr, button)
        self.assertIn('>hide stage</button>', stage)
        before, after = stage.split(button)
        self.assertIn('id="ledger"', before)  # below the gauge row ...
        self.assertEqual(len(re.findall(r"<div\b", before)) - len(re.findall(r"</div>", before)), 1)  # ... in a row of its own, not inside the header
        self.assertIn('id="stage-hall"', after)  # and ahead of the hall it controls
        self.assertIn('id="stage-row"', after)

    def test_the_toggle_row_sits_in_the_flow_so_it_never_covers_the_gauges(self):
        css = page_css()
        bar = re.search(r"\.stage-bar\s*\{([^}]*)\}", css).group(1)
        self.assertNotRegex(bar, r"position:\s*(absolute|fixed|sticky)")
        self.assertNotRegex(bar, r"(^|[;\s])(top|bottom|transform|margin-top)\s*:")
        self.assertRegex(css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important")  # what hides the hall: no animation can run in it


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StageTest(unittest.TestCase):
    NOW = 1_800_000_000_000  # ms

    def iso(self, ago_s):
        return datetime.fromtimestamp(self.NOW / 1000 - ago_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def ens(self, **run):
        return run_page(f"return ensembleOf({json.dumps(sample_run(**run))}, {self.NOW})")

    def test_roles_map_to_the_pipeline_sections(self):
        keys = run_page("return ['research', 'Implementer', 'plan review', 'task-review', 'fix-check', 'steward', 'reviewer', 'fixer', 'weird'].map((r) => sectionOf(r).key)")
        self.assertEqual(keys, ["research", "implement", "plan-review", "task-review", "fix-check", "git", "review", "fix", "weird"])
        self.assertEqual(run_page("return [sectionOf('weird').label, sectionOf('weird').k, sectionOf('plan-review').label]"), ["Weird", 4, "Plan review"])
        self.assertEqual(run_page("return ROLES.map((r) => r.key)"),
                         ["research", "plan", "plan-review", "implement", "task-review", "review", "fix", "fix-check", "git"])

    def test_model_letters(self):
        self.assertEqual(run_page("return ['claude-opus-5-5', 'claude-haiku-4-5', 'gpt-5.5-sol', 'gpt-5.5-astra', null].map(modelLetter)"),
                         ["O", "H", "S", "A", "?"])

    def test_the_chief_state_comes_from_the_board(self):
        chief = agent("chief", "chief", kind="chief")
        working = self.ens(agents=[chief, agent("c1", "implement")], last_activity=self.iso(10))
        self.assertEqual((working["chiefState"], [a["id"] for a in working["agents"]]), ("working", ["c1"]))
        self.assertEqual(self.ens(agents=[chief, agent("c1", "implement", status="done")], last_activity=self.iso(60))["chiefState"], "thinking")
        idle = self.ens(agents=[chief], last_activity=self.iso(360))
        self.assertEqual((idle["chiefState"], idle["idleFor"]), ("idle", 360_000))
        self.assertEqual(self.ens(phase="done")["chiefState"], "done")
        self.assertEqual(self.ens(phase="report", ended="2026-11-17T01:00:00Z")["chiefState"], "done")
        self.assertEqual(self.ens(phase="paused", agents=[agent("c1", "implement")])["chiefState"], "paused")
        self.assertEqual(self.ens()["idleFor"], 0)  # no last_activity

    def test_which_runs_leave_the_stage(self):
        gone = lambda **e: run_page(f"return isGone({json.dumps(e)})")
        self.assertTrue(gone(chiefState="done"))
        self.assertTrue(gone(chiefState="paused"))
        self.assertTrue(gone(chiefState="idle", idleFor=30 * 60_000 + 1))
        self.assertFalse(gone(chiefState="idle", idleFor=29 * 60_000))
        self.assertFalse(gone(chiefState="thinking", idleFor=10 ** 9))

    def test_the_stage_list_follows_the_repo_filter(self):
        runs = [sample_run(key="a@1", repo="/x"), sample_run(key="b@1", repo="/y", phase="done")]
        out = run_page(f"active = {json.dumps(sample_board(runs))}; const all = stageList({self.NOW}).map((e) => e.id); filter = '/x'; return [all, stageList({self.NOW}).map((e) => e.id)]")
        self.assertEqual(out, [["a@1", "b@1"], ["a@1"]])

    def test_a_batch_spreads_by_role_and_nobody_moves_later(self):
        out = run_page("""
          const P = (id, role) => ({ id, role, status: 'running' })
          const first = seatPlan({}, [], [P('i1', 'implement'), P('r1', 'research'), P('i2', 'implementer'), P('g1', 'git')])
          const again = seatPlan(first.seats, [], [P('i1', 'implement'), P('r1', 'research'), P('i2', 'implementer'), P('g1', 'git')])
          const joined = seatPlan(first.seats, [], [P('i1', 'implement'), P('r1', 'research'), P('i2', 'implementer'), P('g1', 'git'), P('i3', 'implement')])
          return [first, again, joined]""")
        first, again, joined = out
        self.assertEqual(first["seats"], {"r1": 1, "i1": 3, "i2": 6, "g1": 8})
        self.assertEqual(first["entered"], ["r1", "i1", "i2", "g1"])
        self.assertEqual((again["seats"], again["entered"], again["exited"]), (first["seats"], [], []))
        self.assertEqual(joined["seats"], {**first["seats"], "i3": 4})  # beside its section, nearest its home chair

    def test_past_ten_players_wait_and_a_walking_chair_stays_reserved(self):
        out = run_page("""
          const P = (id) => ({ id, role: 'implement', status: 'running' })
          const rest = Array.from({ length: 10 }, (_, k) => P('a' + (k + 1)))
          const full = seatPlan({}, [], [P('a0'), ...rest])
          const left = seatPlan(full.seats, [], rest)                    // a0 walks off: its chair is not free this poll
          const held = seatPlan(left.seats, [full.seats.a0], rest)      // still walking
          const freed = seatPlan(left.seats, [], rest)                   // walked off
          return [Object.keys(full.seats).length, full.waiting, left.exited, left.waiting, held.waiting, freed.seats.a10, freed.seats.a1]""")
        self.assertEqual(out, [10, ["a10"], ["a0"], ["a10"], ["a10"], 0, 1])

    def test_face_turns(self):
        t = lambda prev, **e: run_page(f"return faceTrigger({json.dumps(prev)}, {json.dumps({'phase': 'implement', 'chiefState': 'working', 'agents': [], **e})})")
        running = lambda *ids: [{"id": i, "role": "implement", "status": "running"} for i in ids]
        self.assertIsNone(t(None, agents=running("a")))
        self.assertEqual(t({"phase": "plan", "known": ["a"]}, agents=running("a", "b")), "announce")
        self.assertEqual(t({"phase": "implement", "known": ["a", "b"]}, agents=running("b", "c")), "thank")
        self.assertEqual(t({"phase": "implement", "known": ["a"]}, agents=running("a", "b", "c")), "greet")
        self.assertIsNone(t({"phase": "implement", "known": ["a"]}, agents=running("a")))
        self.assertIsNone(t({"phase": "plan", "known": []}, chiefState="idle"))
        gate = lambda st, kind, now: run_page(f"return faceGate({json.dumps(st)}, '{kind}', {now})")
        self.assertEqual(gate({"facing": None, "readyAt": 0}, "greet", 1), "show")
        self.assertEqual(gate({"facing": "greet", "readyAt": 0}, "announce", 1), "upgrade")
        self.assertEqual(gate({"facing": "announce", "readyAt": 0}, "thank", 1), "skip")
        self.assertEqual(gate({"facing": None, "readyAt": 3500}, "thank", 1000), "skip")
        self.assertEqual(gate({"facing": None, "readyAt": 3500}, "announce", 1000), "queue")
        self.assertEqual(run_page("return [COOLDOWN, FACE.greet, FACE.thank, FACE.announce, IDLE_AFTER, IDLE_LEAVE, BOW_HOLD, CAPACITY]"),
                         [3500, 1900, 2400, 2600, 300_000, 1_800_000, 4000, 10])

    def test_every_poll_moves_the_stage_but_the_programme_is_drawn_once(self):
        fixed = json.dumps(sample_board([sample_run(last_activity=self.iso(0))]))
        out = run_page(f"""fetch = async () => ({{ ok: true, status: 200, text: async () => JSON.stringify({fixed}) }})
          const seen = []; Stage.render = (list) => seen.push(list.map((e) => e.chiefState))
          let calls = 0; const real = renderProgramme; renderProgramme = () => {{ calls++; real() }}
          Date.now = () => {self.NOW}; await poll()
          Date.now = () => {self.NOW} + 6 * 60e3; await poll()
          return [seen, calls]""")
        self.assertEqual(out, [[["thinking"], ["idle"]], 1])

    def test_an_unchanged_board_triggers_no_stage_measurement(self):
        # A lenient fake DOM: just enough for Stage.mount/render with chief-only ensembles (no seats are built).
        out = run_page("""
          let rects = 0
          const node = () => {
            const attrs = {}, classes = new Set(), subs = {}
            const n = { style: { setProperty() {} }, dataset: {}, children: [], parentNode: null, isConnected: false, innerHTML: '', textContent: '', title: '',
              getAttribute: (k) => attrs[k] ?? null, setAttribute: (k, v) => { attrs[k] = String(v) },
              classList: { contains: (c) => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), add: (...c) => c.forEach((x) => classes.add(x)), remove: (...c) => c.forEach((x) => classes.delete(x)) },
              querySelector: (sel) => (subs[sel] ||= node()), appendChild(c) { n.insertBefore(c, null) },
              insertBefore(c, ref) { if (c.parentNode) c.remove(); const i = ref ? n.children.indexOf(ref) : n.children.length; n.children.splice(i, 0, c); c.parentNode = n; c.isConnected = true },
              remove() { if (n.parentNode) { n.parentNode.children.splice(n.parentNode.children.indexOf(n), 1); n.parentNode = null; n.isConnected = false } },
              getBoundingClientRect: () => { rects++; return { left: 0 } }, getBBox() {}, animate() {} }
            return n
          }
          document.createElement = node; document.createElementNS = node
          globalThis.matchMedia = () => ({ matches: false })
          globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
          globalThis.ResizeObserver = class { observe() {} unobserve() {} }
          const later = []
          globalThis.setTimeout = (fn) => { later.push(fn); return 0 }; globalThis.clearTimeout = () => {}
          const row = node()
          Stage.mount(row, { curtains: false })
          const E = (id) => ({ id, title: id, phase: 'plan', chiefState: 'thinking', idleFor: 0, agents: [] })
          const step = (list) => { const before = rects; Stage.render(list); return rects - before }
          const out = { ghost: step([]), ghostAgain: step([]) }
          out.added = step([E('a')])
          out.addedMore = step([E('a'), E('b')])
          out.unchanged = step([E('a'), E('b')])
          out.updated = step([{ ...E('a'), phase: 'implement', title: 'renamed' }, E('b')])
          out.reordered = step([E('b'), { ...E('a'), phase: 'implement', title: 'renamed' }])
          out.removing = step([E('b')])                                   // a stage that left the list is still on stage while it bows out
          const before = rects; later.splice(0).forEach((fn) => fn()); out.removed = rects - before
          out.order = row.children.map((c) => c.querySelector('h3').textContent)
          return out""")
        self.assertEqual((out["ghost"], out["ghostAgain"]), (0, 0))  # the ghost: the row had no stage to measure
        self.assertEqual(out["unchanged"], 0)
        self.assertEqual(out["updated"], 0)  # a title or phase change moves nothing
        self.assertEqual(out["removing"], 0)
        for moved in ("added", "addedMore", "reordered", "removed"):
            self.assertGreater(out[moved], 0, moved)
        self.assertEqual(out["order"], ["b"])  # the bowing stage was taken out of the row when it finished leaving

    def test_the_ensemble_count_follows_the_clock_on_an_unchanged_board(self):
        run = sample_run(agents=[agent("chief", "chief", kind="chief")], last_activity=self.iso(360))
        out = run_page(f"""active = {json.dumps(sample_board([run]))}
          const heads = []
          stageTick({self.NOW}); heads.push([$('headline').innerHTML, $('sub').textContent])
          stageTick({self.NOW} + 31 * 60e3); heads.push([$('headline').innerHTML, $('sub').textContent])
          return heads""")
        self.assertEqual(out[0][0], "1 chief at the podium.")
        self.assertEqual(out[1], ["The stage is quiet.", "The next chief run lights the stage."])

    def test_the_stage_is_never_redrawn_wholesale(self):
        html = PAGE.read_text()
        self.assertNotIn('id="scene"', html)
        self.assertIn('id="stage-row"', html)
        render = html.split("function renderStage")[1].split("\nfunction ")[0]
        self.assertNotIn("stage-row", render)
        self.assertEqual(html.count("setInterval("), 1)
        flip = html.split("function flip")[1].split("\n  function ")[0]
        self.assertIn("translateX", flip)
        self.assertNotIn("scaleX", flip)
        self.assertTrue(run_page("return Stage.mounted() === false && Stage.render([]) === undefined"))

    def test_stage_css(self):
        css = page_css()
        self.assertRegex(css, r"\.ens\s*\{[^}]*flex:\s*1 1 0[^}]*min-width:\s*236px[^}]*max-width:\s*486px")
        self.assertRegex(css, r"\.row\s*\{[^}]*gap:\s*3px[^}]*overflow-x:\s*auto")
        self.assertRegex(css, r"prefers-reduced-motion[^{]*\{[^@]*\.ens \.cur\s*\{\s*display:\s*none")
        self.assertRegex(css, r"\.pit \.role\s*\{[^}]*10\.5px")
        self.assertNotRegex(css, r"\.pit \.role\s*\{[^}]*transition:[^;}]*(left|top)")
        plate = float(re.search(r"\.ens \.plate h3\s*\{[^}]*?(\d+(?:\.\d+)?)px", css).group(1))
        card = float(re.search(r"\.card h3\s*\{[^}]*?(\d+(?:\.\d+)?)px", css).group(1))
        self.assertLessEqual(plate, card)
        for name, body in re.findall(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^}]*\})*)\s*\}", css):
            props = set(re.findall(r"([\w-]+)\s*:", body))
            self.assertLessEqual(props, {"transform", "opacity"}, name)

    def test_the_robots_sprite_has_no_id_clash_and_the_front_chief(self):
        svg = ROBOTS.read_text()
        self.assertEqual(svg.count('id="podium"'), 1)  # the symbol only
        self.assertEqual(svg.count('id="podiumWood"'), 1)
        self.assertNotIn("url(#podium)", svg)
        self.assertIn('<symbol id="r-chief-legs"', svg)
        self.assertIn('<symbol id="r-chief-torso"', svg)
        gen = (PAGE.parents[2] / "assets" / "generate.py").read_text()
        self.assertIn("podiumWood", gen)
        self.assertIn("r-chief-legs", gen)


if __name__ == "__main__":
    unittest.main()
