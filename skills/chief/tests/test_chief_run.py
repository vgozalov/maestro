import contextlib
import http.server
import io
import importlib.util
import json
import os
import subprocess
import shlex
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
from datetime import datetime, timezone
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

BARE = {"input_tokens": 5, "output_tokens": 5}
WRITE_1H = {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 2_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 2_000, "ephemeral_5m_input_tokens": 0}}
WRITE_5M = {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 2_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 2_000}}
READ_ONLY = {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 42_000, "cache_creation_input_tokens": 0}


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

    def transcript(self, name, prompt, messages, done=True, model_alias="sonnet", base=None):
        base = chief.parse_time("2026-11-12T10:00:00.000Z") if base is None else base
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines = [{"type": "user", "timestamp": stamp(base), "message": {"role": "user", "content": prompt}}]
        for index, (model, usage) in enumerate(messages):
            message = {"id": f"msg_{name}_{index}", "model": model, "role": "assistant", "usage": usage,
                       "stop_reason": "end_turn" if done and index == len(messages) - 1 else "tool_use",
                       "content": [{"type": "text", "text": f"DONE from {name}"}]}
            # Streaming writes the same message id more than once; only the last copy counts.
            lines.append({"type": "assistant", "timestamp": stamp(base + 60 * (index + 1)), "message": dict(message, usage={**usage, "output_tokens": 1})})
            lines.append({"type": "assistant", "timestamp": stamp(base + 60 * (index + 1) + 30), "message": message})
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

    def test_an_invalid_session_id_is_recorded_as_null(self):
        for bad in ("../*", "a/b", "x" * 81, "abc\n"):
            with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": bad}):
                self.assertIsNone(json.loads((self.start() / "run.json").read_text())["session_id"])
            self.assertIsNone(json.loads((self.start(session_id=bad) / "run.json").read_text())["session_id"])

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

    def test_a_tampered_session_id_never_reaches_the_card(self):
        run = self.start(title="X")
        self.record_session(run, "../../etc/x")
        self.assertIsNone(self.card(run)["session_id"])

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

    def turns(self, path, run, session_id, turns):
        """Append assistant turns, given as (seconds after the run started, usage), to a main session file. Returns the start as epoch seconds."""
        began = chief.parse_time(json.loads((run / "run.json").read_text())["started"])
        lines = []
        for index, (offset, usage) in enumerate(turns):
            stamp = datetime.fromtimestamp(began + offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append({"sessionId": session_id, "type": "assistant", "timestamp": stamp, "isSidechain": False,
                          "message": {"id": f"msg_{session_id}_t{index}", "model": "claude-opus-5-5", "role": "assistant",
                                      "stop_reason": "tool_use", "usage": usage, "content": [{"type": "text", "text": "ok"}]}})
        with open(path, "a") as handle:
            handle.write("\n".join(json.dumps(line) for line in lines) + "\n")
        return int(began)

    def cache_of(self, run, turns, session_id="cache0001"):
        began = self.turns(self.main_session(session_id, run), run, session_id, turns)
        return began, self.card(run)["cache"]

    def test_a_one_hour_write_means_a_one_hour_ttl(self):
        run = self.start(title="X")
        began, cache = self.cache_of(run, [(60, WRITE_1H)])
        self.assertEqual(cache, {"last_at": began + 60, "ttl_s": 3600})
        self.assertEqual(self.chief_row(run)["cache"], cache)

    def test_a_five_minute_write_means_a_five_minute_ttl(self):
        run = self.start(title="X")
        began, cache = self.cache_of(run, [(60, WRITE_5M)])
        self.assertEqual(cache, {"last_at": began + 60, "ttl_s": 300})

    def test_no_usage_means_no_cache_and_no_cache_tokens_means_no_ttl(self):
        no_usage = json.dumps({"type": "assistant", "timestamp": "2026-11-17T00:00:00Z", "message": {"id": "m", "role": "assistant", "content": []}})
        self.assertIsNone(chief.parse_messages([no_usage], PRICES)["cache"])
        self.assertIsNone(chief.parse_messages([], PRICES)["cache"])
        run = self.start(title="X")
        began, cache = self.cache_of(run, [(60, BARE)])
        self.assertEqual(cache, {"last_at": began + 60, "ttl_s": None})

    def test_the_last_message_wins(self):
        run = self.start(title="X")
        began, cache = self.cache_of(run, [(60, WRITE_1H), (600, WRITE_5M)])
        self.assertEqual(cache, {"last_at": began + 600, "ttl_s": 300})
        other = self.start(title="Y")
        began, cache = self.cache_of(other, [(60, WRITE_5M), (900, WRITE_1H)], session_id="cache0002")
        self.assertEqual(cache, {"last_at": began + 900, "ttl_s": 3600})

    def test_a_pure_cache_read_keeps_the_ttl_of_the_last_write(self):
        run = self.start(title="X")
        began, cache = self.cache_of(run, [(60, WRITE_1H), (900, READ_ONLY)])
        self.assertEqual(cache, {"last_at": began + 900, "ttl_s": 3600})
        other = self.start(title="Y")
        began, cache = self.cache_of(other, [(60, READ_ONLY)], session_id="cache0003")
        self.assertEqual(cache, {"last_at": began + 60, "ttl_s": 300})

    def test_a_message_that_is_not_the_last_does_not_decide_the_ttl(self):
        run = self.start(title="X")
        began, cache = self.cache_of(run, [(60, WRITE_1H), (120, BARE)])
        self.assertEqual(cache, {"last_at": began + 120, "ttl_s": None})

    def test_a_finished_run_has_no_cache_and_workers_never_get_one(self):
        run = self.start(title="X")
        self.cache_of(run, [(60, WRITE_1H)])
        self.transcript("w1", f"Run: {run}   Task: 1   Role: implement", [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})])
        row = next(a for a in self.card(run)["agents"] if a["id"] == "claude-w1")
        self.assertNotIn("cache", row)
        chief.set_phase(str(run), "done")
        self.assertIsNone(self.card(run)["cache"])
        self.assertIsNone(self.chief_row(run)["cache"])

    def test_a_run_without_a_chief_session_has_no_cache(self):
        run = self.start(title="X")
        self.assertIsNone(self.card(run)["cache"])


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
        self.transcript("rev", f"Run: {run}   Task: 2   Role: reviewer", [("claude-opus-5-5", {"input_tokens": 10, "output_tokens": 10})], done=False, base=time.time() - 120)
        tasks = {t["id"]: t["status"] for t in self.board()["runs"][0]["tasks"]}
        self.assertEqual(tasks, {"1": "done", "2": "review", "3": "queued"})


class LastMessageTest(Workspace):
    def test_trailing_metadata_is_not_activity(self):
        lines = [json.dumps(r) for r in [
            {"type": "user", "timestamp": "2026-11-17T15:49:17.097Z", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-11-17T15:49:25.594Z", "message": {"id": "m", "role": "assistant", "content": []}},
            {"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-11-17T15:49:25.632Z"},
            {"type": "api-request", "timestamp": "2026-11-17T15:50:00Z"},
            {"type": "queue-operation", "timestamp": "2026-11-17T15:51:00Z"},
            {"type": "last-prompt"}, {"type": "cost-state"}, {"type": "tools-used"},
        ]]
        self.assertEqual(chief.parse_messages(lines, PRICES)["last_at"], "2026-11-17T15:49:25.594Z")
        self.assertIsNone(chief.parse_messages([json.dumps({"type": "last-prompt"})], PRICES)["last_at"])

    def test_no_timestamped_message_falls_back_to_the_mtime(self):
        run = self.start(title="X")
        path = self.projects / "agent-bare.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": f"Run: {run}   Task: 1   Role: implement"}}) + "\n"
                        + json.dumps({"type": "tools-used", "tools": []}) + "\n")
        os.utime(path, (time.time() - 60, time.time() - 60))
        row = self.board()["runs"][0]["agents"][0]
        self.assertAlmostEqual(row["active"], os.path.getmtime(path), places=3)
        self.assertEqual(row["status"], "running")

    def test_a_subagent_is_stale_from_its_last_message_not_its_mtime(self):
        run = self.start(title="X")
        usage = [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})]
        self.transcript("old", f"Run: {run}   Task: 1   Role: implement", usage, done=False, base=time.time() - 3600)
        with open(self.projects / "agent-old.jsonl", "a") as handle:
            handle.write(json.dumps({"type": "tools-used", "tools": []}) + "\n")  # bumps the mtime to now
        self.transcript("new", f"Run: {run}   Task: 2   Role: implement", usage, done=False, base=time.time() - 120)
        status = {a["id"]: a["status"] for a in self.board()["runs"][0]["agents"]}
        self.assertEqual((status["claude-old"], status["claude-new"]), ("stopped", "running"))


class SpendAndQuietTest(Workspace):
    def test_a_stopped_chief_session_goes_quiet_despite_trailing_records(self):
        run = self.start(title="Abandoned")
        state = json.loads((run / "run.json").read_text())
        state["started"] = state["phases"][0]["at"] = "2026-01-01T00:00:00Z"
        state["session_id"] = "stop0001"
        (run / "run.json").write_text(json.dumps(state))
        base = {"sessionId": "stop0001", "isSidechain": False}
        records = [
            {**base, "type": "user", "timestamp": "2026-01-01T00:00:10Z", "message": {"role": "user", "content": f"/chief {run}"}},
            {**base, "type": "assistant", "timestamp": "2026-01-01T00:00:20Z", "message": {"id": "m1", "model": "claude-opus-5-5", "role": "assistant",
                "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "chief-run.py start"}]}},
            {**base, "type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-01-01T00:00:21Z"},
            {"type": "last-prompt", "sessionId": "stop0001"}, {"type": "cost-state", "sessionId": "stop0001"}, {"type": "tools-used", "sessionId": "stop0001"},
        ]
        (self.config / "projects" / "-repo" / "stop0001.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")  # mtime is now
        card = self.board()["runs"][0]
        row = next(a for a in card["agents"] if a["id"] == "chief")
        self.assertEqual(row["status"], "stopped")
        self.assertTrue(card["quiet"])
        self.assertEqual(card["last_activity"], "2026-01-01T00:00:20Z")

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

    def test_an_archived_run_still_working_counts_its_current_spend(self):
        run = self.start(title="Busy")
        self.codex_record(run, "first")
        chief.archive_runs(str(self.repo), [run.name], config_dir=str(self.config), prices=PRICES)
        self.assertAlmostEqual(self.board()["spend"]["all_runs"], 0.078, places=3)
        self.codex_record(run, "second")  # spend that arrives after the archive
        board = self.board()
        self.assertAlmostEqual(board["spend"]["all_runs"], 0.156, places=3)
        self.assertEqual(board["runs"], [])

    def test_an_archived_run_that_finishes_counts_the_spend_added_after_the_archive(self):
        run = self.start(title="Busy")
        self.codex_record(run, "first")
        chief.archive_runs(str(self.repo), [run.name], config_dir=str(self.config), prices=PRICES)
        self.codex_record(run, "second")  # spend that arrives after the archive
        chief.set_phase(str(run), "done", config_dir=str(self.config), prices=PRICES)
        self.assertAlmostEqual(self.board()["spend"]["all_runs"], 0.156, places=3)

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

    def test_start_prints_the_run_but_not_the_session_id(self):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "CHIEF_DASHBOARD": "off", "CLAUDE_CODE_SESSION_ID": "abc-123"}
        out = subprocess.run([sys.executable, "-B", str(SCRIPT), "start", "--title", "X"], cwd=self.repo, env=env,
                             input="do the thing", capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        printed = json.loads(out.stdout)
        self.assertNotIn("session_id", printed)
        self.assertEqual(printed["id"], Path(printed["run"]).name)
        self.assertEqual(json.loads((Path(printed["run"]) / "run.json").read_text())["session_id"], "abc-123")

    def test_archive_from_the_command_line(self):
        run = self.start(title="X")
        chief.set_phase(str(run), "done")
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "CHIEF_DASHBOARD": "off"}
        out = subprocess.run([sys.executable, "-B", str(SCRIPT), "archive", "--done"], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["archived"], [chief.list_runs(str(self.repo))[0]["key"]])


class ArchiveDoneFlagTest(Workspace):
    def archive_cli(self, *args):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "CHIEF_DASHBOARD": "off"}
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "archive", *args], cwd=self.repo, env=env, capture_output=True, text=True)

    def test_done_with_named_runs_is_refused_and_archives_nothing(self):
        finished, named = self.start(title="Finished"), self.start(title="Named")
        chief.set_phase(str(finished), "done")
        out = self.archive_cli(named.name, "--done")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")
        self.assertEqual(out.stderr.strip(), "chief-run: --done archives every finished run; run it on its own, without run names")
        self.assertEqual([r["archived"] for r in chief.list_runs(str(self.repo))], [None, None])

    def test_a_bare_done_still_sweeps_every_finished_run(self):
        finished, live = self.start(title="Finished"), self.start(title="Live")
        chief.set_phase(str(finished), "done")
        out = self.archive_cli("--done")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(json.loads(out.stdout)["archived"]), 1)
        self.assertEqual([r["title"] for r in self.board()["runs"]], ["Live"])


class ResumedRunTest(Workspace):
    """A run resumed in a new Claude Code session: back on the board, driven by the newest session, every session kept."""

    BEGAN = chief.parse_time("2026-01-01T00:00:00Z")  # long ago, so no chat in these tests counts as live
    FIRST, SECOND = "aaaa1111", "bbbb2222"
    FIRST_USAGE = {"input_tokens": 1_000_000, "output_tokens": 0}  # $4.00 on claude-opus-5-5
    SECOND_USAGE = {**WRITE_1H, "input_tokens": 2_000_000, "output_tokens": 0}  # $8.024, and a one-hour cache

    def setUp(self):
        super().setUp()
        chief.OPENED_CHATS.clear()
        self.addCleanup(chief.OPENED_CHATS.clear)

    def stamp(self, t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def old_run(self, **kwargs):
        run = self.start(title="X", **kwargs)
        state = json.loads((run / "run.json").read_text())
        state["started"] = state["phases"][0]["at"] = self.stamp(self.BEGAN)
        (run / "run.json").write_text(json.dumps(state))
        return run

    def session(self, run, session_id, verb, offset, usage=None, title=None, cwd=None, age=None):
        """A main session whose last message came `offset` seconds after the run began. Returns its transcript path."""
        base = {"sessionId": session_id, "isSidechain": False, "cwd": cwd or str(self.repo)}
        lines = [
            {**base, "type": "user", "uuid": f"{session_id}-user", "timestamp": self.stamp(self.BEGAN + offset - 30),
             "message": {"role": "user", "content": "/chief add CSV export"}},
            {**base, "type": "assistant", "uuid": f"{session_id}-assistant", "timestamp": self.stamp(self.BEGAN + offset), "message": {
                "id": f"msg_{session_id}", "model": "claude-opus-5-5", "role": "assistant", "stop_reason": "tool_use",
                "usage": usage or self.FIRST_USAGE,
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"python3 chief-run.py {verb} {run.name}  # {run}"}}]}},
        ]
        if title:
            lines.append({"type": "ai-title", "aiTitle": title})
        return self.write_session(session_id, lines, age)

    def write_session(self, session_id, lines, age=None):
        path = self.config / "projects" / "-repo" / f"{session_id}.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        stale = time.time() - 86400 if age is None else time.time() - age
        os.utime(path, (stale, stale))
        return path

    def replayed(self, run, source, session_id, offset=None, usage=None, title=None):
        """A resumed chat: a transcript that starts as a verbatim copy of `source` (the old session id, uuids, message ids,
        timestamps and usage, as Claude Code writes it), then adds its own messages when `offset` is given."""
        lines = [json.loads(line) for line in source.read_text().splitlines()]
        if offset is not None:
            base = {"sessionId": session_id, "isSidechain": False, "cwd": str(self.repo)}
            lines += [
                {**base, "type": "user", "uuid": f"{session_id}-user", "timestamp": self.stamp(self.BEGAN + offset - 30),
                 "message": {"role": "user", "content": "carry on"}},
                {**base, "type": "assistant", "uuid": f"{session_id}-assistant", "timestamp": self.stamp(self.BEGAN + offset), "message": {
                    "id": f"msg_{session_id}", "model": "claude-opus-5-5", "role": "assistant", "stop_reason": "tool_use",
                    "usage": usage or self.SECOND_USAGE,
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"python3 chief-run.py phase {run.name}"}}]}},
            ]
        if title:
            lines.append({"type": "ai-title", "aiTitle": title})
        return self.write_session(session_id, lines)

    def two_sessions(self, **second):
        run = self.old_run(session_id=self.FIRST)
        self.session(run, self.FIRST, "start", 600, title="First chat")
        self.session(run, self.SECOND, "phase", 90_000, usage=second.pop("usage", self.SECOND_USAGE), title="Resumed chat", **second)
        return run

    def card(self, run):
        return next(item for item in self.board()["runs"] if item["id"] == run.name)

    def pointer(self, run):
        return chief.read_json(chief.pointer_path(str(self.repo.resolve()), run.name))

    def archive(self, run):
        chief.archive_runs(str(self.repo), [run.name], config_dir=str(self.config), prices=PRICES)

    def sessions_of(self, run):
        return json.loads((run / "run.json").read_text()).get("sessions")

    def phase_cli(self, run, phase, session_id):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "CHIEF_DASHBOARD": "off", "CLAUDE_CODE_SESSION_ID": session_id}
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "phase", run.name, phase], cwd=self.repo, env=env, capture_output=True, text=True)

    # an archived run that resumes comes back

    def test_every_working_phase_brings_an_archived_run_back_to_the_board(self):
        for phase in sorted(set(chief.PHASES) - {"done", "paused"}):
            run = self.start(title=f"Resumed into {phase}")
            self.archive(run)
            self.assertNotIn(run.name, [r["id"] for r in self.board()["runs"]])
            chief.set_phase(str(run), phase)
            self.assertIn(run.name, [r["id"] for r in self.board()["runs"]], phase)
            self.assertFalse(self.card(run)["archived"], phase)
            self.assertNotIn("archived", self.pointer(run), phase)
            self.assertNotIn("totals", self.pointer(run), phase)

    def test_the_stale_archived_totals_are_dropped_and_the_spend_is_counted_once(self):
        run = self.start(title="Busy")
        self.codex_record(run, "first")
        self.archive(run)
        self.assertAlmostEqual(self.pointer(run)["totals"]["total_cost_usd"], 0.078, places=3)
        self.codex_record(run, "second")
        chief.set_phase(str(run), "implement")
        board = self.board()
        self.assertEqual([r["id"] for r in board["runs"]], [run.name])
        self.assertAlmostEqual(board["spend"]["all_runs"], 0.156, places=3)  # live, not the stored 0.078 on top
        self.assertEqual(board["archived_count"], 0)

    def test_done_and_paused_keep_the_archive_rule(self):
        for phase in ("done", "paused"):
            run = self.start(title=f"Archived then {phase}")
            self.codex_record(run, f"job-{phase}")
            self.archive(run)
            chief.set_phase(str(run), phase, config_dir=str(self.config), prices=PRICES)
            self.assertTrue(self.pointer(run)["archived"], phase)  # still archived: done and paused never bring it back
            self.assertAlmostEqual(self.pointer(run)["totals"]["total_cost_usd"], 0.078, places=3)
            self.assertNotIn(run.name, [r["id"] for r in self.board()["runs"]], phase)

    def test_pausing_a_resumed_run_leaves_it_on_the_board(self):
        run = self.start(title="X")
        self.archive(run)
        chief.set_phase(str(run), "implement")
        chief.set_phase(str(run), "paused", config_dir=str(self.config), prices=PRICES)
        self.assertEqual([r["id"] for r in self.board()["runs"]], [run.name])
        self.assertNotIn("archived", self.pointer(run))  # it was not archived any more, so paused has nothing to re-archive
        chief.set_phase(str(run), "implement")
        chief.set_phase(str(run), "done", config_dir=str(self.config), prices=PRICES)
        self.assertEqual([r["id"] for r in self.board()["runs"]], [run.name])

    def test_a_run_that_was_never_archived_is_not_touched_by_a_phase(self):
        run = self.start(title="X")
        chief.set_phase(str(run), "implement")
        self.assertEqual(self.pointer(run), {"id": run.name, "dir": str(run.resolve()), "repo": str(self.repo.resolve())})

    # the session that drives the run

    def test_start_records_the_session_in_the_list(self):
        self.assertEqual(self.sessions_of(self.start(session_id=self.FIRST)), [self.FIRST])
        self.assertEqual(self.sessions_of(self.start()), [])
        self.assertEqual(self.sessions_of(self.start(session_id="../x")), [])

    def test_a_phase_in_another_session_makes_it_the_current_one_and_keeps_the_first(self):
        run = self.start(session_id=self.FIRST)
        chief.set_phase(str(run), "research", session_id=self.FIRST)  # the same session again changes nothing
        self.assertEqual((chief.load_run(str(run))["session_id"], self.sessions_of(run)), (self.FIRST, [self.FIRST]))
        chief.set_phase(str(run), "plan", session_id=self.SECOND)
        state = chief.load_run(str(run))
        self.assertEqual((state["session_id"], state["sessions"]), (self.SECOND, [self.FIRST, self.SECOND]))
        chief.set_phase(str(run), "implement", session_id=self.FIRST)  # going back to an earlier session makes it the latest
        state = chief.load_run(str(run))
        self.assertEqual((state["session_id"], state["sessions"]), (self.FIRST, [self.SECOND, self.FIRST]))

    def test_the_session_id_comes_from_claude_code_when_not_given(self):
        run = self.start(session_id=self.FIRST)
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": self.SECOND}):
            chief.set_phase(str(run), "implement")
        self.assertEqual(self.sessions_of(run), [self.FIRST, self.SECOND])
        chief.set_phase(str(run), "review")  # no id in the environment: nothing to record
        self.assertEqual(self.sessions_of(run), [self.FIRST, self.SECOND])
        for bad in ("../../etc/x", "a/b", ""):
            chief.set_phase(str(run), "review", session_id=bad)
            self.assertEqual(chief.load_run(str(run))["session_id"], self.SECOND)

    def test_the_board_follows_the_newest_session(self):
        run = self.two_sessions()
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        card = self.card(run)
        row = next(a for a in card["agents"] if a["id"] == "chief")
        self.assertEqual((row["session_id"], row["chat_title"], row["has_transcript"]), (self.SECOND, "Resumed chat", True))
        self.assertEqual((card["session_id"], card["chat_title"]), (self.SECOND, "Resumed chat"))
        self.assertEqual(card["cache"], {"last_at": int(self.BEGAN) + 90_000, "ttl_s": 3600})  # the new session's, not yesterday's
        self.assertEqual(row["cache"], card["cache"])
        self.assertEqual(row["status"], "stopped")

    def test_the_first_session_stays_listed_as_an_earlier_chief_row(self):
        run = self.two_sessions()
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        chiefs = [a for a in self.card(run)["agents"] if a["role"] == "chief"]
        self.assertEqual([(a["session_id"], a["tokens"]["input"]) for a in chiefs], [(self.FIRST, 1_000_000), (self.SECOND, 2_000_000)])
        self.assertEqual([a["id"] for a in chiefs].count("chief"), 1)
        self.assertEqual(len({a["id"] for a in chiefs}), 2)
        self.assertTrue(all(a["has_transcript"] for a in chiefs))

    def test_the_cost_covers_every_session(self):
        run = self.two_sessions()
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        price = lambda usage: chief.price_claude("claude-opus-5-5", usage, PRICES)
        expected = price(self.FIRST_USAGE) + price(self.SECOND_USAGE)
        self.assertAlmostEqual(expected, 12.024, places=3)
        card = self.card(run)
        self.assertAlmostEqual(card["totals"]["claude"]["cost_usd"], expected, places=3)
        self.assertEqual(card["totals"]["claude"]["agents"], 2)

    # a resumed chat that starts as a copy of the old one

    def copied_pair(self):
        """The first session's records, replayed in the second session's file, then the second session's own messages."""
        run = self.old_run(session_id=self.FIRST)
        first = self.session(run, self.FIRST, "start", 600, title="First chat")
        second = self.replayed(run, first, self.SECOND, offset=90_000, title="Resumed chat")
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        return run, first, second

    def chiefs(self, run):
        return [a for a in self.card(run)["agents"] if a["role"] == "chief"]

    def test_a_replayed_message_is_counted_once_in_the_oldest_session(self):
        run, _, _ = self.copied_pair()
        first, second = self.chiefs(run)
        self.assertEqual((first["session_id"], first["tokens"]["input"], first["cost_usd"]), (self.FIRST, 1_000_000, 4.0))
        self.assertEqual((second["session_id"], second["tokens"]["input"]), (self.SECOND, 2_000_000))  # not 3,000,000
        price = lambda usage: chief.price_claude("claude-opus-5-5", usage, PRICES)
        self.assertAlmostEqual(second["cost_usd"], price(self.SECOND_USAGE), places=6)
        card = self.card(run)
        self.assertAlmostEqual(card["totals"]["claude"]["cost_usd"], 12.024, places=3)  # not 16.024
        self.assertEqual(card["totals"]["claude"]["agents"], 2)
        self.assertAlmostEqual(self.board()["spend"]["all_runs"], 12.024, places=3)
        self.assertIn("Claude subtotal: $12.02 over 2 agent(s)",
                      chief.summary_markdown(str(self.repo), str(run), config_dir=str(self.config), prices=PRICES))

    def test_a_copied_session_starts_at_its_own_first_message(self):
        run, _, _ = self.copied_pair()
        first, second = self.chiefs(run)
        self.assertEqual(first["started"], self.stamp(self.BEGAN + 600 - 30))
        self.assertEqual(second["started"], self.stamp(self.BEGAN + 90_000 - 30))  # not the copied first message, which is the first row's
        self.assertEqual([a["session_id"] for a in self.chiefs(run)], [self.FIRST, self.SECOND])  # so the rows sort oldest first
        self.assertEqual(second["cache"], {"last_at": int(self.BEGAN) + 90_000, "ttl_s": 3600})
        self.assertEqual((self.card(run)["session_id"], self.card(run)["chat_title"]), (self.SECOND, "Resumed chat"))

    def test_a_copy_is_counted_in_the_newer_row_when_the_old_transcript_is_gone(self):
        run, first, _ = self.copied_pair()
        first.unlink()
        rows = self.chiefs(run)
        self.assertEqual([(a["id"], a["session_id"], a["tokens"]["input"]) for a in rows], [("chief", self.SECOND, 3_000_000)])
        self.assertAlmostEqual(self.card(run)["totals"]["claude"]["cost_usd"], 12.024, places=3)  # still once, now all in one row
        self.assertEqual(rows[0]["started"], self.stamp(self.BEGAN + 600 - 30))  # the copy's first message is the only one there is

    def test_a_copied_session_with_no_message_of_its_own_costs_nothing(self):
        run = self.old_run(session_id=self.FIRST)
        first = self.session(run, self.FIRST, "start", 600)
        self.replayed(run, first, self.SECOND, title="Resumed chat")
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        rows = self.chiefs(run)
        self.assertEqual([(a["session_id"], a["tokens"]["input"]) for a in rows], [(self.FIRST, 1_000_000), (self.SECOND, 0)])
        self.assertEqual(self.card(run)["totals"]["claude"]["cost_usd"], 4.0)
        self.assertEqual((self.card(run)["session_id"], self.card(run)["chat_title"]), (self.SECOND, "Resumed chat"))
        self.assertGreater(chief.parse_time(rows[1]["started"]), chief.parse_time(rows[0]["started"]))  # no message of its own: it starts when its file was last written, after the first

    def test_parse_messages_skips_what_an_earlier_transcript_already_counted(self):
        record = lambda kind, uuid, message_id=None, tokens=10: json.dumps({
            "type": kind, "uuid": uuid, "timestamp": "2026-01-01T00:00:10Z",
            "message": {"role": kind, "content": "hi", **({"id": message_id, "model": "claude-opus-5-5", "usage": {"input_tokens": tokens, "output_tokens": 0}} if message_id else {})}})
        old = [record("user", "u1"), record("assistant", "a1", "m1", 5), record("assistant", "a2", "m1", 7)]  # a streamed message is written twice
        seen = set()
        first = chief.parse_messages(old, PRICES, seen=seen)
        self.assertEqual((first["tokens"]["input"], first["started"]), (7, "2026-01-01T00:00:10Z"))  # a streamed copy still replaces the one before it
        again = chief.parse_messages(old + [record("assistant", "a3", "m2", 3)], PRICES, seen=seen)
        self.assertEqual(again["tokens"]["input"], 3)
        self.assertEqual(chief.parse_messages(old, PRICES, seen=seen)["started"], None)  # nothing in it is its own
        self.assertEqual(chief.parse_messages(old, PRICES)["tokens"]["input"], 7)  # without a seen-set nothing is skipped

    def test_the_summary_lists_a_priced_chief_row_per_session(self):
        run = self.two_sessions(usage={"input_tokens": 2_000_000, "output_tokens": 0})  # no cache tokens: a round $8.00
        chief.set_phase(str(run), "report", session_id=self.SECOND)
        text = chief.summary_markdown(str(self.repo), str(run), config_dir=str(self.config), prices=PRICES)
        rows = [line for line in text.splitlines() if "| chief |" in line]
        self.assertEqual(len(rows), 2, text)
        self.assertIn(self.FIRST[:8], rows[0])
        self.assertIn(self.SECOND[:8], rows[1])
        self.assertTrue(rows[0].endswith("| $4.00 |") and rows[1].endswith("| $8.00 |"), rows)
        self.assertIn("Claude subtotal: $12.00 over 2 agent(s)", text)

    def test_a_run_with_one_session_prints_its_chief_row_as_before(self):
        run = self.old_run(session_id=self.FIRST)
        self.session(run, self.FIRST, "start", 600)
        text = chief.summary_markdown(str(self.repo), str(run), config_dir=str(self.config), prices=PRICES)
        self.assertEqual([line for line in text.splitlines() if "| chief |" in line],
                         ["| claude | claude-opus-5-5 | chief | - | stopped | in 1,000,000 / cache w 0 r 0 / out 0 | $4.00 |"])

    def test_a_resumed_session_that_only_ran_phase_is_still_found(self):
        run = self.old_run(session_id=self.FIRST)
        self.session(run, self.SECOND, "phase", 600)  # its transcript never mentions `chief-run.py start`
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        self.assertEqual(next(a for a in self.card(run)["agents"] if a["id"] == "chief")["session_id"], self.SECOND)

    def test_a_session_whose_transcript_is_gone_gets_no_row(self):
        run = self.old_run(session_id=self.FIRST)
        self.session(run, self.SECOND, "phase", 600)
        chief.set_phase(str(run), "implement", session_id=self.SECOND)  # the first one's file was never written
        self.assertEqual([a["session_id"] for a in self.card(run)["agents"] if a["role"] == "chief"], [self.SECOND])
        self.assertEqual(self.card(run)["session_id"], self.SECOND)

    def test_a_session_without_a_transcript_still_names_the_card(self):
        run = self.start(session_id=self.FIRST)
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        self.assertEqual(self.card(run)["session_id"], self.SECOND)

    # older run.json files

    def test_a_run_json_with_only_session_id_keeps_working(self):
        run = self.old_run()
        state = json.loads((run / "run.json").read_text())
        state.pop("sessions", None)
        state["session_id"] = self.FIRST
        (run / "run.json").write_text(json.dumps(state))
        self.session(run, self.FIRST, "start", 600, title="First chat")
        self.assertEqual(chief.run_sessions(chief.load_run(str(run))), [self.FIRST])
        row = next(a for a in self.card(run)["agents"] if a["id"] == "chief")
        self.assertEqual((row["session_id"], row["chat_title"], row["tokens"]["input"]), (self.FIRST, "First chat", 1_000_000))
        self.assertEqual(self.card(run)["session_id"], self.FIRST)
        chief.set_phase(str(run), "review", session_id=self.FIRST)
        self.assertEqual(self.sessions_of(run), [self.FIRST])

    def test_a_later_session_is_added_after_the_one_an_old_run_json_holds(self):
        run = self.old_run()
        state = json.loads((run / "run.json").read_text())
        state.pop("sessions", None)
        state["session_id"] = self.FIRST
        (run / "run.json").write_text(json.dumps(state))
        self.session(run, self.FIRST, "start", 600)
        self.session(run, self.SECOND, "phase", 90_000, title="Resumed chat")
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        state = chief.load_run(str(run))
        self.assertEqual((state["session_id"], state["sessions"]), (self.SECOND, [self.FIRST, self.SECOND]))
        self.assertEqual(self.card(run)["chat_title"], "Resumed chat")

    def test_unsafe_ids_in_the_list_are_ignored(self):
        run = self.old_run(session_id=self.FIRST)
        state = json.loads((run / "run.json").read_text())
        state["sessions"] = ["../../etc/x", self.FIRST, 7, None, "a/b", self.FIRST]
        (run / "run.json").write_text(json.dumps(state))
        self.assertEqual(chief.run_sessions(chief.load_run(str(run))), [self.FIRST])
        state["sessions"] = "not a list"
        self.assertEqual(chief.run_sessions(state), [self.FIRST])

    # the dashboard's command line

    def test_the_phase_command_resumes_an_archived_run_in_a_new_session(self):
        run = self.two_sessions()
        self.archive(run)
        self.assertEqual(self.board()["runs"], [])
        out = self.phase_cli(run, "implement", self.SECOND)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["phase"], "implement")
        card = self.card(run)
        self.assertEqual((card["session_id"], card["chat_title"]), (self.SECOND, "Resumed chat"))
        self.assertEqual(self.sessions_of(run), [self.FIRST, self.SECOND])

    # Open chat

    def open_chat(self, run, launch=None):
        calls = []
        with unittest.mock.patch.object(chief.sys, "platform", "darwin"):
            result = chief.open_chat(str(self.repo), chief.run_key(str(self.repo.resolve()), run.name), config_dir=str(self.config),
                                     prices=PRICES, launch=launch or (lambda cwd, session: calls.append((cwd, session))))
        return result, calls

    def test_open_chat_resumes_the_newest_session(self):
        run = self.two_sessions()
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        self.assertEqual(self.open_chat(run), ((200, {"opened": "Terminal"}), [(str(self.repo), self.SECOND)]))

    def test_open_chat_still_refuses_a_live_current_session(self):
        run = self.two_sessions(age=0)  # the newest transcript was written just now
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        self.assertEqual(self.open_chat(run), ((409, {"error": "live"}), []))

    def test_an_earlier_live_session_does_not_block_the_current_one(self):
        run = self.two_sessions()
        os.utime(self.config / "projects" / "-repo" / f"{self.FIRST}.jsonl", None)
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        self.assertEqual(self.open_chat(run), ((200, {"opened": "Terminal"}), [(str(self.repo), self.SECOND)]))

    def test_a_just_opened_current_session_is_not_opened_twice(self):
        run = self.two_sessions()
        chief.set_phase(str(run), "implement", session_id=self.SECOND)
        self.assertEqual(self.open_chat(run)[0], (200, {"opened": "Terminal"}))
        self.assertEqual(self.open_chat(run), ((409, {"error": "live"}), []))

    def test_open_chat_keeps_the_id_rule_for_the_current_session(self):
        run = self.old_run(session_id=self.FIRST)
        self.session(run, self.FIRST, "start", 600)
        self.session(run, "-opt1234", "phase", 90_000)
        chief.set_phase(str(run), "implement", session_id="-opt1234")  # a plain id, but one that would read as an option
        self.assertEqual(self.open_chat(run), ((404, {"error": "no chief chat is recorded for this run"}), []))


class RenderTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def test_render_transcript_keeps_the_last_entries(self):
        path = Path(self.folder.name) / "t.jsonl"
        lines = [{"type": "user", "timestamp": f"t{i}", "message": {"role": "user", "content": f"line {i}"}} for i in range(5)]
        path.write_text("\n".join(json.dumps(line) for line in lines))
        text = chief.render_transcript(str(path), limit=2)
        self.assertTrue(text.startswith("… 3 earlier entries omitted"))
        self.assertIn("line 4", text)
        self.assertNotIn("line 1", text)

    def test_render_transcript_shows_tools_results_and_codex_lines(self):
        path = Path(self.folder.name) / "t.jsonl"
        records = [
            {"type": "assistant", "timestamp": "t1", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "x" * 5000}, {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b/c.py"}}]}},
            {"type": "user", "timestamp": "t2", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": [{"type": "text", "text": "y" * 3000}]}]}},
            {"type": "turn.started", "item": "z" * 3000}]
        path.write_text("\n".join(json.dumps(line) for line in records) + "\nnot json\n")
        text = chief.render_transcript(str(path))
        self.assertIn("[t1] assistant", text)
        self.assertIn("→ Read b/c.py", text)
        self.assertIn("x" * 4000, text)
        self.assertNotIn("x" * 4001, text)
        self.assertIn("y" * 2000, text)
        self.assertNotIn("y" * 2001, text)
        self.assertIn("[turn.started] ", text)
        self.assertLess(len(text.split("[turn.started] ")[1]), 2100)

    def test_odd_records_are_skipped_and_the_rest_rendered(self):
        path = Path(self.folder.name) / "t.jsonl"
        def record(content):
            return {"type": "assistant", "timestamp": "t", "message": {"role": "assistant", "content": content}}
        records = [record([{"type": "text", "text": "good one"}]),
                   record([{"type": "tool_use", "input": {}}]),
                   record([{"type": "tool_use", "name": "Bash", "input": {"description": "  \n "}}]),
                   {"type": "user", "timestamp": "t", "message": {"role": "user", "content": [{"type": "tool_result", "content": 5}]}},
                   record([{"type": "text", "text": "good two"}])]
        path.write_text("\n".join(json.dumps(line) for line in records))
        text = chief.render_transcript(str(path))
        self.assertIn("good one", text)
        self.assertIn("good two", text)
        self.assertEqual(chief.describe_tool("Bash", {"description": "  \n "}), "Bash")


class DashboardServerTest(Workspace):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.server = subprocess.Popen([sys.executable, "-B", str(SCRIPT), "serve", "--port", str(self.port), "--refresh-usage", "0"],
                                       cwd=self.repo, env={k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
                                       | {"CLAUDE_CONFIG_DIR": str(self.config)},
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

    def get(self, path, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode()

    OK = {"X-Chief-Dashboard": "1"}

    def test_transcript_is_served_only_for_known_agents(self):
        run = self.start(title="X")
        self.transcript("a1", f"Run: {run}   Task: 1   Role: implement", [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})])
        key = chief.run_key(str(self.repo.resolve()), run.name)
        status, text = self.get(f"/transcript?run={key}&agent=claude-a1", self.OK)
        self.assertEqual(status, 200)
        self.assertIn("DONE from a1", text)
        self.assertEqual(self.get(f"/transcript?run={key}&agent=claude-a1")[0], 403)
        self.assertEqual(self.get(f"/transcript?run={key}&agent=../../etc/passwd", self.OK)[0], 404)
        self.assertEqual(self.get("/transcript?run=nope&agent=claude-a1", self.OK)[0], 404)

    def test_every_get_checks_the_host_and_data_needs_the_header(self):
        evil = {"Host": "evil.example:80"}
        for path in ("/", "/health", "/board.json", "/fonts/instrument-serif.woff2"):
            self.assertEqual(self.get(path, {**self.OK, **evil})[0], 403, path)
        self.assertEqual(self.get("/board.json"), (403, "This page is out of date — reload it."))
        status, body = self.get("/board.json", {**self.OK, **evil})
        self.assertEqual(status, 403)
        self.assertIn(f"http://127.0.0.1:{self.port}/", body)
        self.assertIn("host name is not allowed", body)
        self.assertNotIn("out of date", body)
        self.assertEqual(self.get("/board.json", self.OK)[0], 200)
        self.assertEqual(self.get("/file?run=x&name=task")[0], 403)
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/", {"Host": f"localhost:{self.port}"})[0], 200)

    def test_odd_transcript_records_still_answer_200(self):
        run = self.start(title="X")
        self.transcript("a1", f"Run: {run}   Task: 1   Role: implement", [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})])
        odd = [{"type": "assistant", "timestamp": "t", "message": {"role": "assistant", "content": [{"type": "tool_use", "input": {}}]}},
               {"type": "assistant", "timestamp": "t", "message": {"role": "assistant", "content": [
                   {"type": "tool_use", "name": "Bash", "input": {"description": " \n"}}]}},
               {"type": "user", "timestamp": "t", "message": {"role": "user", "content": [{"type": "tool_result", "content": 5}]}}]
        with open(self.projects / "agent-a1.jsonl", "a") as handle:
            handle.write("\n".join(json.dumps(line) for line in odd) + "\n")
        key = chief.run_key(str(self.repo.resolve()), run.name)
        status, text = self.get(f"/transcript?run={key}&agent=claude-a1", self.OK)
        self.assertEqual(status, 200)
        self.assertIn("DONE from a1", text)

    def test_the_path_is_checked_again_when_it_is_served(self):
        run = self.start(title="X")
        self.transcript("a1", f"Run: {run}   Task: 1   Role: implement", [("claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1})])
        record = chief.list_runs(str(self.repo))[0]
        self.assertTrue(chief.resolve_transcript(record, "claude-a1", str(self.config), PRICES))
        outside = Path(self.folder.name) / "outside.jsonl"
        outside.write_text("secret")
        real = chief.build_cards

        def planted(runs, config_dir=None, prices=None):
            cards = real(runs, config_dir, prices)
            cards[0]["agents"][0]["_transcript"] = str(outside)
            return cards
        with unittest.mock.patch.object(chief, "build_cards", planted):
            self.assertIsNone(chief.resolve_transcript(record, "claude-a1", str(self.config), PRICES))

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

    def test_open_chat_checks_host_origin_header_and_json(self):
        # The server subprocess has no injected launcher, so every request here must be refused before any launch.
        run = self.start(title="X")  # no chief chat is recorded for it in self.config
        key = chief.run_key(str(self.repo.resolve()), run.name)

        def post(path, body, **headers):
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                             method="POST", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                error.close()
                return error.code

        js = {"Content-Type": "application/json", "X-Chief-Dashboard": "1"}
        for path in ("/open-chat", "/archive"):
            self.assertEqual(post(path, {"key": key}, **js, Host="evil.example:80"), 403)
            self.assertEqual(post(path, {"key": key}, **js, Origin="http://evil.example"), 403)
        self.assertEqual(post("/open-chat", {"key": key}, **{"Content-Type": "application/json"}), 403)
        self.assertEqual(post("/open-chat", {"key": key}, **{**js, "Content-Type": "text/plain"}), 415)
        self.assertEqual(post("/open-chat", {"key": "no-such-run"}, **js, Origin=f"http://127.0.0.1:{self.port}"), 404)
        self.assertEqual(post("/open-chat", {"key": key}, **js), 404)

    def test_open_chat_answers_500_when_the_run_record_is_corrupt(self):
        run = self.start(title="X")
        key = chief.run_key(str(self.repo.resolve()), run.name)
        state = json.loads((run / "run.json").read_text())
        (run / "run.json").write_text(json.dumps({**state, "started": 5}))  # valid JSON the card code cannot read
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/open-chat", data=json.dumps({"key": key}).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Chief-Dashboard": "1"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        with caught.exception as error:
            self.assertEqual(error.code, 500)
            self.assertIn("replace", json.loads(error.read())["error"])


class OpenChatTest(Workspace):
    """POST /open-chat: resume the chief chat in Terminal, but never fork one that is still being written."""

    def setUp(self):
        super().setUp()
        chief.OPENED_CHATS.clear()  # the record of launches is per process; every test starts without one
        self.addCleanup(chief.OPENED_CHATS.clear)

    def iso(self, t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def chat(self, run, session_id="abcd1234", cwd=None, age=3600):
        """Record a chief chat whose last message, run start and file mtime are all `age` seconds old."""
        t = time.time() - age
        state = json.loads((run / "run.json").read_text())
        state.update(started=self.iso(t - 60)[:19] + "Z", session_id=session_id)
        (run / "run.json").write_text(json.dumps(state))
        base = {"sessionId": session_id, "timestamp": self.iso(t), "isSidechain": False, "cwd": cwd or str(self.repo)}
        lines = [
            {**base, "type": "user", "message": {"role": "user", "content": "/chief add CSV export"}},
            {**base, "type": "assistant", "message": {
                "id": f"msg_{session_id}", "model": "claude-opus-5-5", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"python3 chief-run.py start --title X  # {run}"}}]}},
        ]
        path = self.config / "projects" / "-repo" / f"{session_id}.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        os.utime(path, (t, t))
        return path

    def key(self, run):
        return chief.run_key(str(self.repo.resolve()), run.name)

    def call(self, key, launch, platform="darwin"):
        with unittest.mock.patch.object(chief.sys, "platform", platform):
            return chief.open_chat(str(self.repo), key, config_dir=str(self.config), prices=PRICES, launch=launch)

    def refused(self, key, status, platform="darwin"):
        calls = []
        result = self.call(key, lambda cwd, session: calls.append((cwd, session)), platform)
        self.assertEqual(result[0], status, result)
        self.assertEqual(calls, [])
        return result[1]

    def test_a_quiet_chat_opens_with_the_recorded_values(self):
        run = self.start(title="X")
        self.chat(run)
        calls = []
        self.assertEqual(self.call(self.key(run), lambda cwd, session: calls.append((cwd, session))), (200, {"opened": "Terminal"}))
        self.assertEqual(calls, [(str(self.repo), "abcd1234")])

    def test_a_running_chat_is_not_forked(self):
        run = self.start(title="X")
        self.chat(run, age=0)
        self.assertEqual(self.refused(self.key(run), 409), {"error": "live"})

    def test_a_done_run_with_a_fresh_chat_is_not_forked(self):
        run = self.start(title="X")
        chief.set_phase(str(run), "done")
        self.chat(run, age=0)
        self.assertEqual(self.refused(self.key(run), 409), {"error": "live"})

    def test_a_freshly_written_transcript_is_live(self):
        # The records are old but the file is new: after fix/stale-chat-cards the card's `active` comes from the
        # last message, and the file's own mtime still catches a chat that is being written right now.
        run = self.start(title="X")
        path = self.chat(run, age=3600)
        os.utime(path, None)
        self.assertEqual(self.refused(self.key(run), 409), {"error": "live"})

    def test_unknown_runs_and_runs_without_a_chat_are_404(self):
        run = self.start(title="X")
        self.refused("nope", 404)
        self.refused(self.key(run), 404)  # no chat recorded yet

    def test_a_vanished_or_relative_folder_is_404(self):
        run = self.start(title="X")
        self.chat(run, cwd="/no/such/dir")
        self.refused(self.key(run), 404)
        self.chat(run, cwd="repo")
        self.refused(self.key(run), 404)

    def test_an_id_that_reads_as_an_option_is_refused(self):
        run = self.start(title="X")
        self.chat(run, session_id="-abcd1234")
        self.assertIsNotNone(chief.plain_session_id("-abcd1234"))  # the card accepts it; open-chat must not
        self.refused(self.key(run), 404)

    def test_other_systems_get_501(self):
        run = self.start(title="X")
        self.chat(run)
        self.refused(self.key(run), 501, platform="linux")

    def test_a_failed_launch_is_500(self):
        run = self.start(title="X")
        self.chat(run)

        def boom(cwd, session):
            raise OSError("boom")
        status, body = self.call(self.key(run), boom)
        self.assertEqual(status, 500)
        self.assertIn("boom", body["error"])

    def test_a_second_open_of_the_same_chat_is_refused(self):
        run = self.start(title="X")
        self.chat(run)  # quiet: only the first launch makes it live
        calls = []
        launch = lambda cwd, session: calls.append((cwd, session))
        self.assertEqual(self.call(self.key(run), launch), (200, {"opened": "Terminal"}))
        self.assertEqual(self.call(self.key(run), launch), (409, {"error": "live"}))
        self.assertEqual(calls, [(str(self.repo), "abcd1234")])

    def test_two_requests_at_once_launch_once(self):
        run = self.start(title="X")
        self.chat(run)
        calls, results = [], []

        def slow(cwd, session):
            calls.append(session)
            time.sleep(0.3)  # the first launch is still running when the second request arrives
        threads = [threading.Thread(target=lambda: results.append(self.call(self.key(run), slow))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(calls, ["abcd1234"])

    def test_a_launch_that_failed_or_is_old_does_not_block_the_next_open(self):
        run = self.start(title="X")
        self.chat(run)

        def boom(cwd, session):
            raise OSError("boom")
        self.assertEqual(self.call(self.key(run), boom)[0], 500)
        calls = []
        launch = lambda cwd, session: calls.append(session)
        self.assertEqual(self.call(self.key(run), launch)[0], 200)  # the failed launch left no record
        self.assertEqual(self.call(self.key(run), launch)[0], 409)
        chief.OPENED_CHATS["abcd1234"] = time.time() - chief.STALE_SECONDS - 1  # the launch is now old
        self.assertEqual(self.call(self.key(run), launch)[0], 200)
        self.assertEqual(calls, ["abcd1234", "abcd1234"])

    def test_launch_terminal_writes_a_self_deleting_command(self):
        seen = []
        cwd = "/w/it's here"
        argv = chief.launch_terminal(cwd, "abcd1234", run=lambda a, **kw: seen.append((a, kw)), folder=self.folder.name)
        path = argv[3]
        self.assertEqual(argv, ["open", "-a", "Terminal", path])
        self.assertEqual(Path(path).parent, Path(self.folder.name))
        self.assertTrue(path.endswith(".command") and os.access(path, os.X_OK))
        self.assertEqual(Path(path).read_text(), "#!/bin/sh\n"
                         f"rm -f -- {shlex.quote(path)}\ncd -- {shlex.quote(cwd)} && exec claude --resume {shlex.quote('abcd1234')}\n")
        self.assertEqual(seen, [(argv, {"check": True, "timeout": 10})])

    def test_a_failed_launch_leaves_no_command_file(self):
        def boom(argv, **kw):
            raise OSError("no open")
        with self.assertRaises(OSError):
            chief.launch_terminal("/w", "abcd1234", run=boom, folder=self.folder.name)
        self.assertEqual(list(Path(self.folder.name).glob("chief-chat-*.command")), [])


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

    def test_a_tag_from_a_brief_with_windows_line_endings_has_no_carriage_return(self):
        match = chief.RUN_TAG.search("Run: /a/r1\r\nRepo: /a")
        self.assertEqual(match.group(1), "/a/r1")
        match = chief.RUN_TAG.search("Run: /a/My Project/r1   Task: 2   Role: fix\r\nRepo: /a")
        self.assertEqual(match.groups(), ("/a/My Project/r1", "2", "fix"))
        match = chief.RUN_TAG.search("Run: /a/r1  \r\n")
        self.assertEqual(match.group(1), "/a/r1")

    def test_records_are_written_into_the_run_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = codex_job.record_path(folder, "20261112-100000", "build")
            codex_job.write_record(path, {"status": "running"})
            codex_job.write_record(path, {"status": "completed"})
            self.assertEqual(json.loads(Path(path).read_text())["status"], "completed")
            self.assertTrue(path.startswith(os.path.join(folder, "agents")))


if __name__ == "__main__":
    unittest.main()
