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

    def test_the_chief_row_links_to_its_chat(self):
        html = self.score("chief", cwd="/x/My repo")
        self.assertIn("vscode://anthropic.claude-code/open?session=1a2b3c4d-aaaa", html)
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


if __name__ == "__main__":
    unittest.main()
