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
        self.assertNotRegex(css, r"\.hall\s*\{[^}]*max-width")  # the stage's hall is a backdrop, not a cap

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

    def test_a_session_id_that_is_not_a_plain_token_gets_no_command_or_link(self):
        self.assertIsNone(run_page("return resumeCommand({cwd: '/r'}, 'a;rm -rf ~')"))
        chief = {**self.CHIEF, "session_id": "a;rm -rf ~"}
        board = sample_board([sample_run(agents=[chief], cwd="/x/repo")])
        html = run_page(f"active = {json.dumps(board)}; openKey = 'r1@abc'; openAgent = 'chief'; renderScore({{ force: true }}); return $('score').innerHTML")
        for banned in ("claude --resume", "vscode://", "data-copy", "rm -rf ~"):
            self.assertNotIn(banned, html)

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
    def test_column_sizing_by_card_count(self):
        self.assertEqual(run_page("return [1, 2, 3, 4, 5, 9].map((n) => { const v = colVars(n); return [v.b, v.a] })"),
                         [[1, 1], [1, 2], [1, 3], [2, 2], [3, 3], [3, 3]])

    def test_a_busy_column_carries_its_vars_and_a_card_grid(self):
        runs = [sample_run(key=f"k{n}@x", id=f"k{n}", phase="implement") for n in range(4)]
        html = run_page(f"active = {json.dumps(sample_board(runs))}; renderProgramme(); return $('prog').innerHTML")
        self.assertIn('style="--n:4;--b:2;--a:2"', html)
        self.assertEqual(html.count('<div class="cards">'), 1)
        self.assertIn('class="col rail"', html)  # empty columns are still rails

    def test_the_wide_board_rules_live_in_a_container_query(self):
        css = page_css()
        self.assertRegex(css, r"\.prog-wrap\s*\{[^}]*container:\s*board\s*/\s*inline-size")
        wide = css[css.index("@container board (min-width: 900px)"):]
        self.assertIn("repeat(auto-fill, minmax(260px, 1fr))", wide)
        self.assertIn("calc(var(--a) * 360px + (var(--a) - 1) * 10px)", wide)
        self.assertIn('class="prog-wrap"', PAGE.read_text())


def agent(id, role, status="running", provider="claude", model="claude-sonnet-5-5", kind=None):
    return {"id": id, "role": role, "kind": kind or role, "provider": provider, "model": model, "status": status}


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
