"""Page tests for the chief dashboard (skills/chief/dashboard.html).

The page is one self-contained file, so these tests read its <style> text and run its <script> under Node with a
tiny fake DOM (PRELUDE). Later page tasks reuse page_css, run_page, sample_run and sample_board.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
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


def run_page(snippet: str, timeout: float = 20) -> Any:
    """Run the page's script under Node with PRELUDE, then SNIPPET; return its JSON result."""
    tail = (
        ";(async () => { " + snippet + " })()"
        ".then((v) => console.log(JSON.stringify(v === undefined ? null : v)))"
        ".catch((e) => { console.error(e); process.exit(1) })\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "page.js"
        js.write_text(PRELUDE + "\n" + page_script() + "\n" + tail)
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

    def test_cards_and_columns_never_pass_360px(self):
        css = page_css()
        self.assertRegex(css, r"\.card\s*\{[^}]*max-width:\s*360px")
        self.assertRegex(css, r"\.col\s*\{[^}]*max-width:\s*360px")
        self.assertRegex(css, r"\.grid-archived\s*\{[^}]*minmax\(260px,\s*360px\)")

    def test_there_is_no_page_wide_cap(self):
        css = page_css()
        self.assertNotIn("1600px", css)
        self.assertNotRegex(css, r"\.hall\b")
        self.assertNotIn('class="hall"', PAGE.read_text())

    def test_the_programme_scrolls_instead_of_squeezing_the_cards(self):
        css = page_css()
        self.assertRegex(css, r"\.prog\s*\{[^}]*overflow-x:\s*auto")
        self.assertRegex(css, r"\.col\s*\{[^}]*min-width:\s*260px")
        self.assertNotRegex(css, r"\.prog\s*\{[^}]*flex-wrap:\s*wrap")
        small = css[css.index("@media (max-width: 1100px)"):css.index("@media (max-width: 620px)")]
        self.assertRegex(small, r"\.col\s*\{[^}]*flex:\s*0 0 clamp\(260px")

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

    def test_the_header_is_shorter(self):
        css = page_css()
        self.assertRegex(css, r"\.meter svg\s*\{[^}]*width:\s*100px[^}]*height:\s*58px")
        self.assertRegex(css, r"\.headline\s*\{[^}]*clamp\(26px,\s*3\.4vw,\s*40px\)")


if __name__ == "__main__":
    unittest.main()
