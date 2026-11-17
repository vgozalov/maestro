import contextlib
import http.server
import io
import importlib.util
import json
import os
import subprocess
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "chief-run.py"
PRICES = json.loads((HERE.parent.parent / "model-routing" / "prices.json").read_text())


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chief = load(SCRIPT, "chief_run")
codex_job = load(HERE.parent.parent / "model-routing" / "scripts" / "run-codex-job.py", "run_codex_job")


class Workspace(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.repo = root / "repo"
        self.config = root / "claude-config"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        self.projects = self.config / "projects" / "-repo" / "session-1" / "subagents"
        self.projects.mkdir(parents=True)
        self.saved_home = os.environ.get("CHIEF_HOME")
        os.environ["CHIEF_HOME"] = str(root / "chief-home")  # never touch the real ~/.chief
        self.saved_session = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # never inherit the builder's own session

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("CHIEF_HOME", None)
        else:
            os.environ["CHIEF_HOME"] = self.saved_home
        if self.saved_session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = self.saved_session
        self.folder.cleanup()

    def start(self, text="Add CSV export to the report page.\nClient wants it by Friday.", repo=None, **kwargs):
        return Path(chief.start_run(str(repo or self.repo), text, **kwargs))

    def other_repo(self, name="other"):
        path = Path(self.folder.name) / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        return path

    def transcript(self, name, prompt, messages, done=True, model_alias="sonnet"):
        lines = [{"type": "user", "timestamp": "2026-11-12T10:00:00.000Z", "message": {"role": "user", "content": prompt}}]
        for index, (model, usage) in enumerate(messages):
            message = {"id": f"msg_{name}_{index}", "model": model, "role": "assistant", "usage": usage,
                       "stop_reason": "end_turn" if done and index == len(messages) - 1 else "tool_use",
                       "content": [{"type": "text", "text": f"DONE from {name}"}]}
            # Streaming writes the same message id more than once; only the last copy counts.
            lines.append({"type": "assistant", "timestamp": f"2026-11-12T10:0{index + 1}:00.000Z", "message": dict(message, usage={**usage, "output_tokens": 1})})
            lines.append({"type": "assistant", "timestamp": f"2026-11-12T10:0{index + 1}:30.000Z", "message": message})
        (self.projects / f"agent-{name}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        (self.projects / f"agent-{name}.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": f"{name} job", "model": model_alias}))

    def codex_record(self, run, name, **fields):
        agents = run / "agents"
        agents.mkdir(exist_ok=True)
        record = {"provider": "chatgpt", "model": "gpt-6-luna", "effort": "medium", "task": "2", "role": "implement",
                  "label": name, "status": "completed", "started": "2026-11-12T10:00:00Z", "ended": "2026-11-12T10:03:00Z",
                  "usage": {"input": 1_000_000, "cached": 800_000, "output": 100_000, "reasoning": 40_000}}
        record.update(fields)
        (agents / f"codex-{name}.json").write_text(json.dumps(record))

    def board(self):
        return chief.build_board(str(self.repo), config_dir=str(self.config), prices=PRICES, with_git=False)


class StartRunTest(Workspace):
    def test_start_records_the_session(self):
        run = self.start(session_id="1a2b3c4d-0000-1111")
        self.assertEqual(json.loads((run / "run.json").read_text())["session_id"], "1a2b3c4d-0000-1111")

    def test_the_session_id_comes_from_claude_code_when_not_given(self):
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abc-123"}):
            self.assertEqual(json.loads((self.start() / "run.json").read_text())["session_id"], "abc-123")
        self.assertIsNone(json.loads((self.start() / "run.json").read_text())["session_id"])  # setUp removed it

    def test_start_saves_the_task_verbatim_and_hides_runs_from_git(self):
        run = self.start(title="CSV export")
        task = (run / "task.md").read_text()
        self.assertIn("# CSV export", task)
        self.assertIn("Source: chat", task)
        self.assertIn("Add CSV export to the report page.\nClient wants it by Friday.", task)
        state = json.loads((run / "run.json").read_text())
        self.assertEqual(state["phase"], "intake")
        self.assertEqual(state["title"], "CSV export")
        self.assertTrue(run.name.endswith("-csv-export"))
        status = subprocess.run(["git", "status", "--short"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(status, "")

    def test_the_title_defaults_to_the_first_line(self):
        run = self.start()
        self.assertEqual(json.loads((run / "run.json").read_text())["title"], "Add CSV export to the report page.")

    def test_two_runs_in_the_same_minute_get_distinct_folders(self):
        self.assertNotEqual(self.start(title="Same"), self.start(title="Same"))

    def test_an_empty_task_is_refused(self):
        with self.assertRaises(ValueError):
            self.start(text="   ")

    def test_issue_references_map_to_gh_or_glab(self):
        self.assertEqual(chief.issue_command("123")[:3], ["gh", "issue", "view"])
        self.assertEqual(chief.issue_command("https://gitlab.com/acme/app/-/issues/7")[:3], ["glab", "issue", "view"])
        self.assertEqual(chief.issue_command("https://github.com/acme/app/issues/9")[:3], ["gh", "issue", "view"])


class SessionLinkTest(Workspace):
    """The chief's chat session: which one, its title, its folder, and whether its transcript may be served."""

    def main_session(self, session_id, run, mention="dir", cwd="/w/My Project", titles=(), input_tokens=100):
        at = json.loads((run / "run.json").read_text())["started"]
        named = str(run) if mention == "dir" else run.name
        base = {"sessionId": session_id, "timestamp": at, "isSidechain": False, **({"cwd": cwd} if cwd else {})}
        lines = [
            {**base, "type": "user", "message": {"role": "user", "content": "/chief add CSV export"}},
            {**base, "type": "assistant", "message": {
                "id": f"msg_{session_id}", "model": "claude-opus-5-5", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": input_tokens, "output_tokens": 10},
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"python3 chief-run.py start --title X  # {named}"}}]}},
            *titles,
        ]
        path = self.config / "projects" / "-repo" / f"{session_id}.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def record_session(self, run, session_id):
        state = json.loads((run / "run.json").read_text())
        (run / "run.json").write_text(json.dumps({**state, "session_id": session_id}))

    def card(self, run):
        return next(item for item in self.board()["runs"] if item["id"] == run.name)

    def chief_row(self, run):
        return next(a for a in self.card(run)["agents"] if a["id"] == "chief")

    def test_the_chief_row_comes_from_the_recorded_session(self):
        run = self.start(title="X")
        self.main_session("aaaa1111", run, input_tokens=999)
        self.main_session("bbbb2222", run, input_tokens=100)
        self.record_session(run, "bbbb2222")
        row = self.chief_row(run)
        self.assertEqual((row["session_id"], row["tokens"]["input"], row["has_transcript"]), ("bbbb2222", 100, True))
        self.assertEqual(self.card(run)["session_id"], "bbbb2222")

    def test_without_a_session_id_the_scan_still_finds_the_chief(self):
        run = self.start(title="X")
        self.main_session("cccc3333", run)
        self.assertEqual(self.chief_row(run)["session_id"], "cccc3333")

    def test_a_loosely_matched_chief_has_no_transcript(self):
        run = self.start(title="X")
        self.main_session("dddd4444", run, mention="id")
        self.assertFalse(self.chief_row(run)["has_transcript"])

    def test_an_unsafe_session_id_is_never_globbed(self):
        for bad in ("../*", "*", "a/b", ""):
            self.assertIsNone(chief.session_transcript(str(self.config), bad))

    def test_chat_title_prefers_custom_then_ai_title(self):
        ai = lambda t: {"type": "ai-title", "aiTitle": t}
        custom = lambda t: {"type": "custom-title", "customTitle": t}
        lines = lambda *records: "\n".join(json.dumps(r) for r in records)
        self.assertEqual(chief.chat_title(lines(ai("A"), ai("B"), custom("C"))), "C")
        self.assertEqual(chief.chat_title(lines(ai("A"), ai("B"))), "B")
        self.assertEqual(chief.chat_title(lines(custom({"text": "odd"}), ai("B"))), "B")
        self.assertEqual(chief.chat_title(lines(ai("A")) + "\n{not json ai-title"), "A")
        self.assertIsNone(chief.chat_title(lines({"type": "user"})))
        run = self.start(title="X")
        self.main_session("eeee5555", run, titles=[ai("Fix filed issues"), custom("Dashboard work")])
        self.assertEqual(self.card(run)["chat_title"], "Dashboard work")

    def test_cwd_comes_from_the_session_else_the_repo(self):
        run = self.start(title="X")
        self.main_session("ffff6666", run, cwd="/w/My Project")
        self.assertEqual(self.card(run)["cwd"], "/w/My Project")
        other = self.start(title="Y")
        self.assertEqual(self.card(other)["cwd"], str(self.repo.resolve()))

    def test_worker_rows_carry_parent_session_and_agent_id(self):
        run = self.start(title="X")
        self.transcript("w1", f"Run: {run}   Task: 1   Role: implement", [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})])
        row = next(a for a in self.card(run)["agents"] if a["id"] == "claude-w1")
        self.assertEqual((row["session_id"], row["agent_id"], row["has_transcript"]), ("session-1", "w1", True))

    def test_the_board_holds_no_transcript_paths(self):
        run = self.start(title="X")
        self.main_session("aaaa1111", run)
        self.transcript("w1", f"Run: {run}   Task: 1   Role: implement", [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})])
        text = json.dumps(self.board())
        self.assertNotIn(str(self.config), text)
        self.assertNotIn('"_transcript"', text)  # the key itself; has_transcript is meant to be there


class PhaseTest(Workspace):
    def test_phases_are_recorded_in_order_with_the_plan_path(self):
        run = self.start(title="X")
        chief.set_phase(str(run), "plan")
        chief.set_phase(str(run), "implement", plan="docs/plan.md", workspace=".parallel-build/plan")
        state = chief.load_run(str(run))
        self.assertEqual([entry["phase"] for entry in state["phases"]], ["intake", "plan", "implement"])
        self.assertEqual(state["plan"], "docs/plan.md")
        self.assertEqual(state["workspace"], ".parallel-build/plan")

    def test_an_unknown_phase_is_refused(self):
        run = self.start(title="X")
        with self.assertRaises(ValueError):
            chief.set_phase(str(run), "deploy")

    def test_done_records_the_end_time(self):
        run = self.start(title="X")
        chief.set_phase(str(run), "done")
        self.assertIsNotNone(chief.load_run(str(run))["ended"])


class BoardTest(Workspace):
    def test_claude_and_codex_agents_of_a_run_are_listed_with_separate_costs(self):
        run = self.start(title="CSV export")
        other = self.start(title="Other task")
        self.transcript("a1", f"Run: {run}   Task: 1   Role: implement\nBuild it.", [
            ("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}),
        ])
        self.transcript("a2", f"Run: {other}   Task: 1   Role: research\nLook.", [
            ("claude-haiku-5-5", {"input_tokens": 10, "output_tokens": 10}),
        ])
        self.transcript("untagged", "Plain prompt with no run tag", [("claude-haiku-5-5", {"input_tokens": 10, "output_tokens": 10})])
        self.codex_record(run, "build-2")

        board = self.board()
        card = next(item for item in board["runs"] if item["id"] == run.name)
        providers = sorted((agent["provider"], agent["role"], agent["task"]) for agent in card["agents"])
        self.assertEqual(providers, [("chatgpt", "implement", "2"), ("claude", "implement", "1")])
        self.assertEqual({agent["status"] for agent in card["agents"]}, {"done"})
        # Sonnet: 1M input * $2 + 0.1M output * $10 = $3.00
        self.assertAlmostEqual(card["totals"]["claude"]["cost_usd"], 3.0)
        # Luna: 0.2M uncached * $0.10 + 0.8M cached * $0.01 + 0.1M output (reasoning inside) * $0.50 = $0.078
        self.assertAlmostEqual(card["totals"]["chatgpt"]["cost_usd"], 0.078)
        self.assertAlmostEqual(card["totals"]["total_cost_usd"], 3.078)
        self.assertTrue(card["totals"]["estimate"])

    def test_streamed_copies_of_a_message_are_counted_once(self):
        run = self.start(title="X")
        self.transcript("a1", f"Run: {run}   Task: 1   Role: review", [
            ("claude-opus-5-5", {"input_tokens": 0, "output_tokens": 1_000_000}),
        ])
        agent = self.board()["runs"][0]["agents"][0]
        self.assertEqual(agent["tokens"]["output"], 1_000_000)
        self.assertAlmostEqual(agent["cost_usd"], 20.0)
        self.assertEqual(agent["status"], "done")
        self.assertEqual(agent["model"], "claude-opus-5-5")
        self.assertIn("DONE from a1", agent["report"])

    def test_each_claude_agent_reports_its_last_and_peak_context(self):
        run = self.start(title="X")
        self.transcript("c1", f"Run: {run}   Task: 1   Role: implement", [
            ("claude-sonnet-5-5", {"input_tokens": 100, "output_tokens": 5, "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 2_000}),
            ("claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 30_000, "cache_creation_input_tokens": 0}),
        ])
        self.codex_record(run, "build-2")
        card = next(item for item in self.board()["runs"] if item["id"] == run.name)
        worker = next(a for a in card["agents"] if a["id"] == "claude-c1")
        self.assertEqual(worker["context"], {"last": 30_010, "peak": 52_100})
        self.assertIsNone(next(a for a in card["agents"] if a["provider"] == "chatgpt")["context"])
        self.assertNotIn("context", worker["tokens"])

    def test_cache_writes_and_reads_use_their_own_prices(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1_000_000,
                 "cache_creation_input_tokens": 2_000_000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}
        # Sonnet: read 0.10 + 5m write 2.5 + 1h write 4.0
        self.assertAlmostEqual(chief.price_claude("claude-sonnet-5-5", usage, PRICES), 6.6)

    def test_haiku_long_prompts_use_the_long_context_rate(self):
        usage = {"input_tokens": 200_000, "output_tokens": 0}
        self.assertAlmostEqual(chief.price_claude("claude-haiku-5-5", usage, PRICES), 0.1)

    def test_a_resumed_codex_thread_is_counted_once(self):
        run = self.start(title="X")
        self.codex_record(run, "fix-1", thread_id="t1", ended="2026-11-12T10:03:00Z")
        self.codex_record(run, "fix-1-resume", thread_id="t1", ended="2026-11-12T10:09:00Z", resumed=True,
                          usage={"input": 2_000_000, "cached": 1_600_000, "output": 200_000, "reasoning": 0})
        card = self.board()["runs"][0]
        superseded = [agent for agent in card["agents"] if agent.get("superseded")]
        self.assertEqual(len(superseded), 1)
        self.assertAlmostEqual(card["totals"]["chatgpt"]["cost_usd"], 0.156)

    def test_unknown_models_are_unpriced_not_guessed(self):
        run = self.start(title="X")
        self.codex_record(run, "unknown", model="gpt-5-legacy-unknown")
        totals = self.board()["runs"][0]["totals"]
        self.assertEqual(totals["chatgpt"]["unpriced"], 1)
        self.assertEqual(totals["chatgpt"]["cost_usd"], 0)

    def test_subagents_of_a_repo_with_a_space_in_its_path_are_found(self):
        repo = self.other_repo("My Project")
        run = self.start(repo=repo, title="Spaced")
        self.transcript("s1", f"Run: {run}   Task: 2   Role: implement\nBuild it.", [
            ("claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 10})])
        card = next(item for item in self.board()["runs"] if item["id"] == run.name)
        self.assertEqual([(a["role"], a["task"]) for a in card["agents"] if a["id"] != "chief"], [("implement", "2")])

    def test_a_running_codex_job_whose_process_is_gone_shows_stopped(self):
        run = self.start(title="X")
        self.codex_record(run, "ghost", status="running", pid=99999999, ended=None, usage=None)
        self.assertEqual(self.board()["runs"][0]["agents"][0]["status"], "stopped")

    def test_board_totals_add_every_run(self):
        first, second = self.start(title="A"), self.start(title="B")
        self.codex_record(first, "one")
        self.codex_record(second, "two")
        self.assertAlmostEqual(self.board()["totals"]["chatgpt"]["cost_usd"], 0.156)


class SummaryTest(Workspace):
    def test_the_summary_keeps_providers_apart_and_labels_the_total(self):
        run = self.start(title="CSV export")
        self.transcript("a1", f"Run: {run}   Task: 1   Role: implement", [
            ("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 100_000}),
        ])
        self.codex_record(run, "build-2")
        text = chief.summary_markdown(str(self.repo), str(run), config_dir=str(self.config), prices=PRICES)
        self.assertIn("| claude | claude-sonnet-5-5 |", text)
        self.assertIn("| chatgpt | gpt-6-luna |", text)
        self.assertIn("Claude subtotal: $3.00", text)
        self.assertIn("ChatGPT subtotal: $0.08", text)
        self.assertIn("API-equivalent estimate", text)


class MoneyTest(unittest.TestCase):
    def test_fractions_of_a_cent_are_not_shown_as_zero(self):
        self.assertEqual(chief.money(0.0004), "<$0.01")
        self.assertEqual(chief.money(0), "$0.00")
        self.assertEqual(chief.money(None), "unpriced")


class FileAccessTest(Workspace):
    def test_only_named_run_files_can_be_read(self):
        run_dir = self.start(title="X")
        runs = chief.list_runs()
        run = chief.find_run(runs, runs[0]["key"])
        self.assertEqual(chief.resolve_file(run, "task"), os.path.realpath(run_dir / "task.md"))
        self.assertIsNone(chief.resolve_file(run, "../../etc/passwd"))
        self.assertIsNone(chief.find_run(runs, "../x"))
        self.assertIsNone(chief.resolve_file(None, "task"))

    def test_plan_files_resolve_inside_the_runs_own_checkout(self):
        other = self.other_repo()
        (other / "docs").mkdir()
        (other / "docs" / "plan.md").write_text("# plan\n")
        run_dir = self.start(title="Elsewhere", repo=other)
        chief.set_phase(str(run_dir), "plan", plan="docs/plan.md")
        run = chief.find_run(chief.list_runs(str(self.repo)), run_dir.name)
        self.assertEqual(chief.resolve_file(run, "plan"), os.path.realpath(other / "docs" / "plan.md"))
        chief.set_phase(str(run_dir), "plan", plan="../repo/secret.md")
        (self.repo / "secret.md").write_text("no")
        run = chief.find_run(chief.list_runs(), run_dir.name)
        self.assertIsNone(chief.resolve_file(run, "plan"))


class SharedDashboardTest(Workspace):
    """One dashboard for every repo and worktree: runs register in CHIEF_HOME."""

    def test_runs_from_every_repo_show_on_one_board(self):
        other = self.other_repo()
        self.start(title="Here")
        self.start(title="There", repo=other)
        board = chief.build_board(str(self.repo), config_dir=str(self.config), prices=PRICES, with_git=False)
        self.assertEqual(sorted(card["title"] for card in board["runs"]), ["Here", "There"])
        self.assertEqual(sorted(r["name"] for r in board["repos"]), ["other", "repo"])
        anywhere = chief.build_board(None, config_dir=str(self.config), prices=PRICES, with_git=False)
        self.assertEqual(len(anywhere["runs"]), 2)

    def test_git_state_is_read_per_checkout(self):
        other = self.other_repo()
        self.start(title="Here")
        self.start(title="There", repo=other)
        seen = []
        board = chief.build_board(str(self.repo), config_dir=str(self.config), prices=PRICES,
                                  git=lambda path: seen.append(path) or {"branch": os.path.basename(path), "warnings": []})
        self.assertEqual(sorted(seen), sorted(os.path.realpath(p) for p in (self.repo, other)))
        self.assertEqual({r["name"]: r["git"]["branch"] for r in board["repos"]}, {"repo": "repo", "other": "other"})

    def test_the_same_run_id_in_two_repos_gets_two_keys(self):
        other = self.other_repo()
        now = time.time()
        first = self.start(title="Same", now=now)
        second = self.start(title="Same", repo=other, now=now)
        self.assertEqual(first.name, second.name)
        runs = chief.list_runs()
        self.assertEqual(len({run["key"] for run in runs}), 2)
        self.assertIsNone(chief.find_run(runs, first.name), "a bare id that two repos share is ambiguous")
        self.assertEqual(chief.find_run(runs, runs[0]["key"])["key"], runs[0]["key"])

    def test_a_removed_worktree_drops_out_of_the_registry(self):
        other = self.other_repo("worktree")
        run_dir = self.start(title="Gone", repo=other)
        self.assertEqual(len(list(Path(chief.registry_dir()).glob("*.json"))), 1)
        subprocess.run(["rm", "-rf", str(other)], check=True)
        self.assertEqual(chief.list_runs(), [])
        self.assertEqual(list(Path(chief.registry_dir()).glob("*.json")), [], run_dir)

    def test_runs_from_before_the_registry_are_registered_on_request(self):
        run_dir = self.start(title="Old")
        for pointer in Path(chief.registry_dir()).glob("*.json"):
            pointer.unlink()
        self.assertEqual(len(chief.list_runs(str(self.repo))), 1, "the current checkout's runs show without a pointer")
        self.assertEqual(chief.list_runs(), [])
        self.assertEqual(chief.register_checkout(str(self.repo)), 1)
        self.assertEqual([run["id"] for run in chief.list_runs()], [run_dir.name])

    def test_long_finished_runs_are_left_out_but_open_ones_stay(self):
        old, live = self.start(title="Old"), self.start(title="Live")
        state = json.loads((old / "run.json").read_text())
        state.update(phase="done", ended="2026-01-01T00:00:00Z")
        (old / "run.json").write_text(json.dumps(state))
        self.assertEqual([run["title"] for run in chief.list_runs(days=14)], ["Live"])
        self.assertEqual(len(chief.list_runs()), 2)


class ServeTest(unittest.TestCase):
    def fake_server(self, body):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = chief.DashboardServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_a_second_serve_finds_the_running_dashboard_and_returns(self):
        port = self.fake_server(json.dumps({"app": chief.DASHBOARD_APP, "version": chief.DASHBOARD_VERSION}))
        self.assertTrue(chief.dashboard_running(port))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chief.serve(None, port, refresh_minutes=0)  # returns instead of failing or blocking
        self.assertIn("already running", out.getvalue())

    def test_an_older_single_repo_dashboard_on_the_port_is_named(self):
        port = self.fake_server("<html>old dashboard</html>")
        self.assertFalse(chief.dashboard_running(port))
        with self.assertRaisesRegex(RuntimeError, "older one"):
            chief.serve(None, port, refresh_minutes=0)

    def test_a_free_port_is_not_mistaken_for_a_dashboard(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(chief.dashboard_running(port))


FAKE_DASHBOARD = """
import http.server, json, os, sys
port, health = int(sys.argv[2]), sys.argv[3]  # argv[1] is "serve", as in a real dashboard's command line
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" and health != "none":
            body = json.dumps({"app": "chief-dashboard", "version": int(health), "pid": os.getpid()}).encode()
            self.send_response(200)
        else:
            body = b"<html>Agent runs</html>"
            self.send_response(200 if self.path == "/" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):  # no reverse DNS lookup on bind; it stalls on macOS runners
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
Server(("127.0.0.1", port), Handler).serve_forever()
"""


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class EnsureDashboardTest(unittest.TestCase):
    """The chief starts the shared dashboard itself, joins a running one, and replaces an older one."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.saved = {name: os.environ.get(name) for name in ("CHIEF_HOME", "CHIEF_DASHBOARD")}
        os.environ["CHIEF_HOME"] = self.folder.name
        os.environ.pop("CHIEF_DASHBOARD", None)
        self.addCleanup(self.restore)
        self.port = free_port()
        self.addCleanup(self.stop_server)

    def restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def stop_server(self):
        health = chief.dashboard_health(self.port, timeout=1)
        if health and health.get("pid"):
            os.kill(health["pid"], signal.SIGTERM)

    def fake(self, health, script_name="fake.py"):
        script = Path(self.folder.name) / script_name
        script.write_text(FAKE_DASHBOARD)
        process = subprocess.Popen([sys.executable, str(script), "serve", str(self.port), health])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.time() + 5
        while chief.port_free(self.port) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(chief.port_free(self.port), "the fake dashboard did not start")
        return process

    def test_it_starts_one_then_joins_it(self):
        first = chief.ensure_dashboard(self.port)
        self.assertEqual(first["server"], "started", first)
        pid = chief.dashboard_health(self.port)["pid"]
        second = chief.ensure_dashboard(self.port)
        self.assertEqual(second["server"], "running")
        self.assertEqual(chief.dashboard_health(self.port)["pid"], pid, "the running dashboard is kept")

    def test_an_older_chief_dashboard_is_replaced(self):
        old = self.fake("1")
        result = chief.ensure_dashboard(self.port)
        self.assertEqual(result["server"], "replaced", result)
        self.assertIsNotNone(old.wait(timeout=5))
        self.assertGreaterEqual(chief.dashboard_health(self.port)["version"], chief.DASHBOARD_VERSION)

    @unittest.skipUnless(shutil.which("lsof"), "finding a server without /health needs lsof")
    def test_a_dashboard_from_before_health_checks_is_replaced(self):
        old = self.fake("none", script_name="chief-run.py")
        result = chief.ensure_dashboard(self.port)
        self.assertEqual(result["server"], "replaced", result)
        self.assertIsNotNone(old.wait(timeout=5))

    def test_another_program_on_the_port_is_left_alone(self):
        other = self.fake("none", script_name="other-app.py")
        result = chief.ensure_dashboard(self.port)
        self.assertEqual(result["server"], "failed")
        self.assertIn("CHIEF_DASHBOARD_PORT", result["detail"])
        self.assertIsNone(other.poll(), "a program that is not chief-run.py serve keeps running")

    def test_it_can_be_turned_off(self):
        os.environ["CHIEF_DASHBOARD"] = "off"
        self.assertEqual(chief.ensure_dashboard(self.port)["server"], "off")
        self.assertTrue(chief.port_free(self.port))

    def test_an_unwatched_dashboard_exits_when_idle(self):
        process = subprocess.Popen([sys.executable, "-B", str(SCRIPT), "serve", "--port", str(self.port),
                                    "--refresh-usage", "0", "--idle-hours", "0.0005"],
                                   stdout=subprocess.DEVNULL, cwd=self.folder.name)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.wait(timeout=15), 0)


PLAN = """# Plan
Frozen during waves: `package.json`
Wave 1 (parallel): Tasks 1, 2
Integration (serial): Task 3

### Task 1: Export endpoint
**Owns:** `src/api/export.ts`, `src/api/export.test.ts`
### Task 2: Download button
**Owns:** `src/components/Report/Download.jsx`
### Task 3: Wire the route
**Owns:** `apps/web/routes.ts`
"""


class ActivityTest(Workspace):
    """What the stage shows for each agent: its latest tool calls, its verdict and its section."""

    def tool_transcript(self, name, tag, tools, reply=None):
        lines = [{"type": "user", "timestamp": "2026-11-12T10:00:00.000Z", "message": {"role": "user", "content": tag}}]
        for index, (tool, args) in enumerate(tools):
            lines.append({"type": "assistant", "timestamp": f"2026-11-12T10:0{index + 1}:00.000Z",
                          "message": {"id": f"m{name}{index}", "model": "claude-sonnet-5-5", "role": "assistant", "stop_reason": "tool_use",
                                      "usage": {"input_tokens": 1000, "output_tokens": 100}, "content": [{"type": "tool_use", "name": tool, "input": args}]}})
        if reply:
            lines.append({"type": "assistant", "timestamp": "2026-11-12T10:09:00.000Z",
                          "message": {"id": f"m{name}end", "model": "claude-sonnet-5-5", "role": "assistant", "stop_reason": "end_turn",
                                      "usage": {"input_tokens": 10, "output_tokens": 10}, "content": [{"type": "text", "text": reply}]}})
        (self.projects / f"agent-{name}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    def test_tool_calls_are_listed_newest_first(self):
        run = self.start(title="X")
        self.tool_transcript("t1", f"Run: {run}   Task: 1   Role: implementer", [
            ("Read", {"file_path": "/repo/src/api/export.ts"}),
            ("Bash", {"command": "npm test", "description": "Run the export tests"}),
            ("Grep", {"pattern": "bitDepth"}),
        ])
        agent = self.board()["runs"][0]["agents"][0]
        self.assertEqual([text for _, text in agent["acts"]], ['rg "bitDepth"', "Bash Run the export tests", "Read api/export.ts"])
        self.assertEqual(agent["kind"], "implement")
        self.assertNotIn("_spend", agent, "internal spend buckets stay out of the board")

    def test_a_blocked_reply_marks_the_agent_blocked(self):
        run = self.start(title="X")
        self.tool_transcript("t2", f"Run: {run}   Task: 3   Role: implement", [("Read", {"file_path": "/a/b.ts"})],
                             reply="**Status:** BLOCKED. The query loads every row.")
        agent = self.board()["runs"][0]["agents"][0]
        self.assertEqual((agent["status"], agent["verdict"]), ("blocked", "BLOCKED"))

    def test_roles_map_to_sections(self):
        kinds = {role: chief.role_kind(role) for role in ("implementer", "reviewer", "plan-review", "final-review", "fixer", "plan", "research", "git", "docs")}
        self.assertEqual(kinds, {"implementer": "implement", "reviewer": "review", "plan-review": "review", "final-review": "review",
                                 "fixer": "fix", "plan": "plan", "research": "research", "git": "git", "docs": "other"})

    def test_codex_events_become_tool_calls(self):
        log = Path(self.folder.name) / "events.jsonl"
        log.write_text("\n".join(json.dumps(event) for event in [
            {"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": '/bin/zsh -lc "rg bitDepth src/"'}},
            {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": '/bin/zsh -lc "rg bitDepth src/"'}},
            {"type": "item.completed", "item": {"id": "2", "type": "file_change", "changes": [{"path": "/r/src/ui/Panel.jsx"}]}},
            {"type": "item.completed", "item": {"id": "3", "type": "agent_message", "text": "ok"}},
        ]) + "\n")
        acts, _ = chief.codex_acts(str(log))
        self.assertEqual([text for _, text in acts], ["Edit ui/Panel.jsx", "Bash rg bitDepth src/"])
        self.assertIsNotNone(acts[0][0], "the newest call carries the log's time")


class PlanTasksTest(Workspace):
    def test_tasks_waves_and_areas_come_from_the_plan(self):
        tasks = chief.parse_plan(PLAN)
        self.assertEqual([(t["id"], t["wave"], t["area"]) for t in tasks],
                         [("1", "Wave 1", "api"), ("2", "Wave 1", "components"), ("3", "Integration", "web")])

    def test_test_paths_count_as_tests(self):
        self.assertEqual(chief.area_of("tests/e2e/cart.spec.ts"), "tests")
        self.assertEqual(chief.area_of("src/utils/getData/getData.test.js"), "tests")
        self.assertEqual(chief.area_of("packages/ui/src/Button.tsx"), "ui")

    def test_the_last_ledger_line_about_a_task_wins(self):
        states = chief.ledger_states("Task 1: dispatched (opus)\nTask 1: complete (a..b, review clean)\n"
                                     "Task 2: implemented abc\nTask 2: fix round 2 dispatched\nTask 3: review dispatched\nRuling: x\n")
        self.assertEqual(states, {"1": ("done", 0), "2": ("fix", 2), "3": ("review", 0)})

    def test_a_live_agent_overrides_the_ledger(self):
        (self.repo / "plan.md").write_text(PLAN)
        (self.repo / "ws").mkdir()
        (self.repo / "ws" / "progress.md").write_text("Task 1: complete\nTask 2: dispatched\n")
        run = self.start(title="X")
        chief.set_phase(str(run), "implement", plan="plan.md", workspace="ws")
        self.transcript("rev", f"Run: {run}   Task: 2   Role: reviewer", [("claude-opus-5-5", {"input_tokens": 10, "output_tokens": 10})], done=False)
        tasks = {t["id"]: t["status"] for t in self.board()["runs"][0]["tasks"]}
        self.assertEqual(tasks, {"1": "done", "2": "review", "3": "queued"})


class SpendAndQuietTest(Workspace):
    def test_spend_has_24_hours_of_buckets_and_a_burn_rate(self):
        now = time.time()
        bucket = int(now // chief.BUCKET_SECONDS)
        agents = [{"_spend": {bucket: 1.0, bucket - 3: 0.5, bucket - 200: 9.0}, "cost_usd": 10.5, "provider": "claude", "tokens": {}}]
        spend = chief.spend_summary(agents, archived_total=2.0, now=now)
        self.assertEqual(len(spend["buckets"]), 144)
        self.assertAlmostEqual(spend["last_24h"], 1.5)
        self.assertAlmostEqual(spend["burn_per_hour"], 1.5)
        self.assertAlmostEqual(spend["all_runs"], 12.5, msg="all runs adds the archived runs' stored totals")

    def test_an_unfinished_run_with_nothing_happening_goes_quiet(self):
        run = self.start(title="Stuck")
        state = json.loads((run / "run.json").read_text())
        state["started"] = state["phases"][0]["at"] = "2026-01-01T00:00:00Z"
        (run / "run.json").write_text(json.dumps(state))
        self.assertTrue(self.board()["runs"][0]["quiet"])
        chief.set_phase(str(run), "research")
        self.assertFalse(self.board()["runs"][0]["quiet"], "a phase change is activity")


class ArchiveTest(Workspace):
    def test_archived_runs_leave_the_board_and_keep_their_spend(self):
        done, live = self.start(title="Done one"), self.start(title="Live one")
        self.codex_record(done, "job")
        chief.set_phase(str(done), "done")
        chief.archive_runs(str(self.repo), [done.name], config_dir=str(self.config), prices=PRICES)
        board = self.board()
        self.assertEqual([r["title"] for r in board["runs"]], ["Live one"])
        self.assertEqual(board["archived_count"], 1)
        self.assertAlmostEqual(board["spend"]["all_runs"], 0.078, places=3)
        shelf = chief.build_board(str(self.repo), config_dir=str(self.config), prices=PRICES, with_git=False, archived=True)
        self.assertEqual([r["title"] for r in shelf["runs"]], ["Done one"])
        chief.archive_runs(str(self.repo), [done.name], archived=False)
        self.assertEqual(len(self.board()["runs"]), 2)

    def test_archive_state_survives_re_registering(self):
        run = self.start(title="X")
        chief.archive_runs(str(self.repo), [run.name], config_dir=str(self.config), prices=PRICES)
        chief.set_phase(str(run), "done")
        chief.register_run(str(run), str(self.repo))
        self.assertEqual(self.board()["runs"], [])

    def test_runs_that_ended_long_ago_count_as_archived(self):
        run = self.start(title="Old")
        state = json.loads((run / "run.json").read_text())
        state.update(phase="done", ended="2026-01-01T00:00:00Z")
        (run / "run.json").write_text(json.dumps(state))
        board = chief.build_board(str(self.repo), config_dir=str(self.config), prices=PRICES, with_git=False, days=14)
        self.assertEqual((board["runs"], board["archived_count"]), ([], 1))

    def test_archive_from_the_command_line(self):
        run = self.start(title="X")
        chief.set_phase(str(run), "done")
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "CHIEF_DASHBOARD": "off"}
        out = subprocess.run([sys.executable, "-B", str(SCRIPT), "archive", "--done"], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["archived"], [chief.list_runs(str(self.repo))[0]["key"]])


class DashboardServerTest(Workspace):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.server = subprocess.Popen([sys.executable, "-B", str(SCRIPT), "serve", "--port", str(self.port), "--refresh-usage", "0"],
                                       cwd=self.repo, env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.config)},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.server.wait)
        self.addCleanup(self.server.terminate)
        deadline = time.time() + 8
        while chief.port_free(self.port) and time.time() < deadline:
            time.sleep(0.05)

    def post(self, body, header=True):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/archive", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **({"X-Chief-Dashboard": "1"} if header else {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None

    def test_the_page_carries_the_robots(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            page = response.read().decode()
        self.assertIn('id="r-cello"', page)
        self.assertNotIn("<!--ROBOTS-->", page)

    def test_fonts_are_served_locally_and_nothing_else_is(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/fonts/instrument-serif.woff2", timeout=10) as response:
            self.assertEqual(response.headers["Content-Type"], "font/woff2")
            self.assertEqual(response.read(4), b"wOF2")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/fonts/../scripts/chief-run.py", timeout=10)
        caught.exception.close()

    def test_archiving_needs_the_dashboard_header(self):
        run = self.start(title="X")
        self.assertEqual(self.post({"keys": [run.name]}, header=False)[0], 403)
        status, body = self.post({"keys": [run.name]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["changed"]), 1)
        self.assertEqual(self.post({"keys": ["no-such-run"]})[0], 400)


class CodexRecordTest(unittest.TestCase):
    def test_the_run_tag_is_read_from_the_brief(self):
        tag = codex_job.parse_run_tag("Run: /repo/.chief/runs/20261112-1000-x   Task: 3   Role: fix\nRepo: /repo")
        self.assertEqual(tag, {"run": "/repo/.chief/runs/20261112-1000-x", "task": "3", "role": "fix"})

    def test_a_brief_without_a_tag_has_none(self):
        self.assertIsNone(codex_job.parse_run_tag("Repo: /repo\nFix it."))

    def test_a_run_path_with_spaces_is_read_whole(self):
        tag = codex_job.parse_run_tag("Run: /a/My Project/.chief/runs/r1   Task: 2   Role: implement\nRepo: /a")
        self.assertEqual(tag, {"run": "/a/My Project/.chief/runs/r1", "task": "2", "role": "implement"})

    def test_a_tag_without_task_and_role_keeps_the_whole_path(self):
        tag = codex_job.parse_run_tag("Run: /a/My Project/r1  \nRepo: /a")
        self.assertEqual(tag, {"run": "/a/My Project/r1", "task": None, "role": None})

    def test_single_spaces_between_fields_still_split(self):
        self.assertEqual(codex_job.parse_run_tag("Run: r1 Task: - Role: review"), {"run": "r1", "task": "-", "role": "review"})

    def test_the_tag_never_reads_the_next_line(self):
        self.assertEqual(codex_job.parse_run_tag("Run: /a/r1\nTask: 3"), {"run": "/a/r1", "task": None, "role": None})
        self.assertEqual(codex_job.parse_run_tag("Run: /a/r1\nRepo: /a"), {"run": "/a/r1", "task": None, "role": None})
        self.assertIsNone(codex_job.parse_run_tag("Run:\nRepo: /a"))

    def test_chief_run_reads_the_same_tag(self):
        match = chief.RUN_TAG.search("Run: /a/My Project/r1   Task: 2   Role: fix\nRepo: /a")
        self.assertEqual(match.groups(), ("/a/My Project/r1", "2", "fix"))

    def test_records_are_written_into_the_run_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = codex_job.record_path(folder, "20261112-100000", "build")
            codex_job.write_record(path, {"status": "running"})
            codex_job.write_record(path, {"status": "completed"})
            self.assertEqual(json.loads(Path(path).read_text())["status"], "completed")
            self.assertTrue(path.startswith(os.path.join(folder, "agents")))


if __name__ == "__main__":
    unittest.main()
