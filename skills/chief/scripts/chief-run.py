#!/usr/bin/env python3
"""One chief run: its task, its phases, and every agent that worked on it, Claude and Codex.

  chief-run.py start [--title T] [--file F | --issue REF]   task text on stdin otherwise
  chief-run.py phase <run> <phase> [--plan P] [--workspace W]
  chief-run.py summary <run>                                 cost table for the report
  chief-run.py register                                      list this checkout's existing runs
  chief-run.py archive <run>... [--done] [--restore]         hide runs from the dashboard, or bring them back
  chief-run.py board [--days 14]                             the dashboard's data, as JSON
  chief-run.py dashboard                                     start the shared dashboard or join it
  chief-run.py serve [--port 8765] [--days 14]               the dashboard in the foreground

Runs live in <repo>/.chief/runs/<id>/, which git ignores. Every run also leaves a pointer in
~/.chief/runs/ (CHIEF_HOME overrides ~/.chief), so one dashboard shows the runs of every repo
and worktree on this machine; `serve` started a second time finds the first server and exits.
`start` and `phase` start that
dashboard in the background when none answers, and replace one older than this script.
This script is the only writer of run.json. Agents are never registered by hand: Claude subagents are found in Claude Code's
transcripts and Codex jobs in the records run-codex-job.py writes, both by the brief's first
line, `Run: <run dir>   Task: <id>   Role: <role>`. Costs are API-equivalent estimates from
model-routing's prices.json, kept per provider.
"""
import argparse
import collections
import errno
import hashlib
import glob
import http.server
import importlib.util
import json
import os
import re
import shlex
import signal
import socketserver
import subprocess
import sys
import socket
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILLS_ROOT = SKILL_DIR.parent
PRICES_PATH = SKILLS_ROOT / "model-routing" / "prices.json"
GIT_CHECK_PATH = SKILLS_ROOT / "model-routing" / "scripts" / "git-check.py"
USAGE_SCRIPT = SKILLS_ROOT / "model-routing" / "scripts" / "check-account-usage.py"
USAGE_CACHE = os.path.join(tempfile.gettempdir(), "model-routing-usage.json")
DASHBOARD = SKILL_DIR / "dashboard.html"
ROBOTS = SKILL_DIR / "robots.svg"  # drawn by assets/generate.py, the same robots as the README banner
FONTS = {name: SKILL_DIR / "fonts" / name for name in ("instrument-serif.woff2", "instrument-sans.woff2", "ibm-plex-mono.woff2")}

PHASES = ["intake", "research", "plan", "plan-review", "approval", "implement", "review", "report", "paused", "done"]
RUN_TAG = re.compile(r"Run:[ \t]*(.+?)(?=[ \t]+(?:Task|Role):|[ \t\r]*$)(?:[ \t]+Task:[ \t]*(\S+))?(?:[ \t]+Role:[ \t]*(\S+))?", re.MULTILINE)
STALE_SECONDS = 15 * 60
QUIET_SECONDS = 2 * 3600  # an unfinished run with no agent working and no phase change for this long has gone quiet
BUCKET_SECONDS = 600  # spend is kept in 10-minute buckets for the sparkline and the burn rate
VERDICT = re.compile(r"^\W*(?:status\W*)?(DONE_WITH_CONCERNS|DONE|BLOCKED|FAILED|NEEDS_CONTEXT|APPROVED|CHANGES(?:_REQUESTED)?)\b", re.I)
DEFAULT_DAYS = 14
DASHBOARD_APP = "chief-dashboard"
DASHBOARD_VERSION = 5  # a running dashboard older than this is replaced by the next run
DEFAULT_PORT = int(os.environ.get("CHIEF_DASHBOARD_PORT") or 8765)
AUTO_IDLE_HOURS = 6  # a dashboard the chief started exits after this long with no page open
PROVIDERS = ("claude", "chatgpt")


# ---- small helpers -----------------------------------------------------------------------


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def runs_root(repo):
    return os.path.join(repo, ".chief", "runs")


def chief_home():
    return os.environ.get("CHIEF_HOME") or os.path.join(os.path.expanduser("~"), ".chief")


def registry_dir():
    """Pointers to every run on this machine, whichever repo or worktree holds it."""
    return os.path.join(chief_home(), "runs")


def repo_hash(repo):
    return hashlib.sha1(os.path.realpath(repo).encode("utf-8")).hexdigest()[:8]


def run_key(repo, run_id):
    """Unique across repos: two checkouts can start runs with the same id in the same minute."""
    return f"{run_id}@{repo_hash(repo)}"


def register_run(run_dir, repo):
    """Leave a pointer to the run where the shared dashboard finds it. Idempotent."""
    run_dir, repo = os.path.realpath(run_dir), os.path.realpath(repo)
    run_id = os.path.basename(run_dir)
    os.makedirs(registry_dir(), exist_ok=True)
    path = pointer_path(repo, run_id)
    current = read_json(path) or {}
    wanted = {**current, "id": run_id, "dir": run_dir, "repo": repo}  # keeps archive state
    if current != wanted:
        write_json(path, wanted)
    return path


def pointer_path(repo, run_id):
    return os.path.join(registry_dir(), f"{repo_hash(repo)}--{run_id}.json")


def set_archived(run, archived, totals=None):
    """Archive a run (hide it from the dashboard, keeping its totals for the all-runs spend) or restore it."""
    path = register_run(run["dir"], run["repo"])
    pointer = read_json(path) or {}
    if archived:
        pointer.update(archived=now_iso(), totals=totals)
    else:
        pointer.pop("archived", None)
        pointer.pop("totals", None)
    write_json(path, pointer)
    return pointer


def register_checkout(repo):
    """Register every run already in this checkout, for runs started before the registry."""
    count = 0
    for run_dir in glob.glob(os.path.join(runs_root(repo), "*")):
        if os.path.isfile(os.path.join(run_dir, "run.json")):
            register_run(run_dir, repo)
            count += 1
    return count


def write_json(path, data):
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(temporary, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def slugify(text, limit=40):
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "-".join(words)[:limit].strip("-") or "task"


def git_toplevel(cwd="."):
    completed = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return completed.stdout.strip() if completed.returncode == 0 else None


# ---- starting a run and moving its phase ---------------------------------------------------


def start_run(repo, text, title=None, source="chat", now=None, session_id=None):
    if not text or not text.strip():
        raise ValueError("the task is empty: pass the request on stdin, with --file or with --issue")
    title = (title or text.strip().splitlines()[0]).strip()[:120]
    root = runs_root(repo)
    os.makedirs(root, exist_ok=True)
    ignore = os.path.join(repo, ".chief", ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write("*\n")
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(now))
    base = f"{stamp}-{slugify(title)}"
    run_id, counter = base, 2
    while True:
        try:
            os.makedirs(os.path.join(root, run_id))
            break
        except FileExistsError:
            run_id, counter = f"{base}-{counter}", counter + 1
    run_dir = os.path.join(root, run_id)
    with open(os.path.join(run_dir, "task.md"), "w", encoding="utf-8") as handle:
        handle.write(f"# {title}\n\nSource: {source}\n\n{text.strip()}\n")
    started = now_iso()
    write_json(os.path.join(run_dir, "run.json"), {
        "id": run_id, "title": title, "source": source, "repo": repo, "started": started, "ended": None,
        "phase": "intake", "plan": None, "workspace": None,
        "session_id": plain_session_id(session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")),
        "phases": [{"phase": "intake", "at": started, "usage": usage_snapshot()}],
    })
    try:
        register_run(run_dir, repo)
    except OSError as error:  # the run still works; only the shared dashboard misses it
        print(f"chief-run: could not register the run in {registry_dir()}: {error}", file=sys.stderr)
    return run_dir


def issue_command(ref, remote_url=None):
    gitlab = "gitlab" in ref or (not ref.startswith("http") and "gitlab" in (remote_url or ""))
    if gitlab:
        return ["glab", "issue", "view", ref, "--output", "json"]
    return ["gh", "issue", "view", ref, "--json", "title,body,url"]


def import_issue(repo, ref):
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True).stdout
    command = issue_command(ref, remote)
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(command[:3])} failed: {completed.stderr.strip()}")
    issue = json.loads(completed.stdout)
    body = issue.get("body") or issue.get("description") or ""
    url = issue.get("url") or issue.get("web_url") or ref
    return issue.get("title") or f"Issue {ref}", body or "(the issue has no description)", f"issue {url}"


def resolve_run(repo, run):
    """A run's folder from its id or its path, or None."""
    candidate = run if os.path.isabs(run) or os.sep in run else os.path.join(runs_root(repo), run)
    return candidate if os.path.isfile(os.path.join(candidate, "run.json")) else None


def load_run(run_dir):
    state = read_json(os.path.join(run_dir, "run.json"))
    if state is None:
        raise ValueError(f"no run.json in {run_dir}")
    return state


def set_phase(run_dir, phase, plan=None, workspace=None):
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; one of: {', '.join(PHASES)}")
    state = load_run(run_dir)
    at = now_iso()
    state["phase"] = phase
    if not state.get("session_id"):  # a run from before the id was recorded learns it at its next phase
        state["session_id"] = plain_session_id(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    state["phases"].append({"phase": phase, "at": at, "usage": usage_snapshot()})
    if plan:
        state["plan"] = plan
    if workspace:
        state["workspace"] = workspace
    state["ended"] = at if phase == "done" else None
    write_json(os.path.join(run_dir, "run.json"), state)
    return state


# ---- account usage (read from model-routing's cache; never spends a model turn) ----------


def read_usage_cache(max_age=None):
    try:
        age = time.time() - os.path.getmtime(USAGE_CACHE)
    except OSError:
        return None
    if max_age is not None and age > max_age:
        return None
    readings = read_json(USAGE_CACHE)
    if not isinstance(readings, dict):
        return None
    as_of = datetime.fromtimestamp(os.path.getmtime(USAGE_CACHE), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"as_of": as_of, **{name: readings.get(name) for name in PROVIDERS}}


def usage_snapshot():
    cache = read_usage_cache(max_age=900)
    if not cache:
        return None
    return {name: {key: (cache.get(name) or {}).get(key) for key in ("5h", "week")} for name in PROVIDERS}


# ---- prices -----------------------------------------------------------------------------


def load_prices(path=PRICES_PATH):
    return read_json(path, {}) or {}


def price_claude(model, usage, prices):
    """API-equivalent USD for one Claude message, or None when the model has no price."""
    rates = (prices.get("claude") or {}).get(model)
    if not rates:
        return None
    split = usage.get("cache_creation") or {}
    write_1h = split.get("ephemeral_1h_input_tokens", 0) or 0
    write_5m = (usage.get("cache_creation_input_tokens", 0) or 0) - write_1h
    read = usage.get("cache_read_input_tokens", 0) or 0
    fresh = usage.get("input_tokens", 0) or 0
    long_rates = rates.get("long_context")
    if long_rates and fresh + read + write_5m + write_1h > long_rates.get("above_input_tokens", float("inf")):
        rates = {**rates, **long_rates}
    return (fresh * rates["input"] + (usage.get("output_tokens", 0) or 0) * rates["output"]
            + write_5m * rates["cache_write_5m"] + write_1h * rates["cache_write_1h"]
            + read * rates["cache_read"]) / 1e6


def price_codex(model, usage, prices):
    """API-equivalent USD for one Codex job. Cached input is part of input; reasoning is part of output."""
    rates = (prices.get("chatgpt") or {}).get(model)
    if not rates or not usage:
        return None
    cached = usage.get("cached", 0) or 0
    uncached = max((usage.get("input", 0) or 0) - cached, 0)
    return (uncached * rates["input"] + cached * rates.get("cached_input", rates["input"])
            + (usage.get("output", 0) or 0) * rates["output"]) / 1e6


# ---- Claude agents, from Claude Code's transcripts ----------------------------------------

_TRANSCRIPTS = {}
_SESSIONS = {}
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")


def plain_session_id(value):
    """The id when it is a plain session id, else None."""
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else None


def prompt_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


def short_path(path):
    parts = [part for part in str(path).split("/") if part]
    return "/".join(parts[-2:]) if parts else str(path)


def describe_tool(name, args):
    """One line for a tool call, as the dashboard's activity feed shows it."""
    args = args if isinstance(args, dict) else {}
    if name == "Bash":
        lines = (str(args.get("description") or args.get("command") or "")).strip().splitlines()
        return f"Bash {lines[0][:90]}" if lines else "Bash"
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"{name} {short_path(args.get('file_path') or args.get('notebook_path') or '')}".strip()
    if name == "Grep":
        return f'rg "{str(args.get("pattern") or "")[:60]}"'
    if name == "Glob":
        return f"Glob {args.get('pattern') or ''}".strip()
    if name in ("Agent", "Task"):
        return f"Agent {args.get('description') or ''}".strip()
    if name == "WebFetch":
        return f"WebFetch {urllib.parse.urlparse(str(args.get('url') or '')).netloc}".strip()
    if name == "WebSearch":
        return f"WebSearch {str(args.get('query') or '')[:60]}".strip()
    if name == "TodoWrite":
        return "Update the todo list"
    return name


def verdict_of(report):
    match = VERDICT.match(report or "")
    return match.group(1).upper() if match else None


def cache_ttl(usage, written_ttl):
    """(ttl of the latest cache write, ttl this message implies) for one message's usage, given the ttl of the writes before it.

    A write names its ttl: 1 hour, else 5 minutes. A pure cache read writes nothing, so it does not say which ttl it
    refreshed: it takes the latest write's, else 5 minutes. No cache tokens at all means caching is off: None."""
    split = usage.get("cache_creation") or {}
    if (split.get("ephemeral_1h_input_tokens") or 0) > 0:
        return 3600, 3600
    if (split.get("ephemeral_5m_input_tokens") or 0) > 0 or (usage.get("cache_creation_input_tokens") or 0) > 0:
        return 300, 300
    if (usage.get("cache_read_input_tokens") or 0) > 0:
        return written_ttl, written_ttl or 300
    return written_ttl, None


def parse_messages(lines, prices, start=None, end=None, sidechain=None):
    """Usage, cost, recent tool calls, spend per 10 minutes and the last reply over a transcript's assistant messages."""
    messages, last_text, last_entry, first_at, last_at = {}, "", None, None, None
    acts, stamps = [], {}
    last_cache, written_ttl = None, None  # the newest message's (time, ttl), and the ttl of the latest cache write
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        at = parse_time(entry.get("timestamp"))
        if start is not None and (at is None or at < start or (end is not None and at > end)):
            continue
        if sidechain is not None and bool(entry.get("isSidechain")) != sidechain:
            continue
        if entry.get("type") in ("user", "assistant"):
            last_entry = entry
            first_at = first_at or entry.get("timestamp")
            last_at = entry.get("timestamp") or last_at
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if message.get("usage") and message.get("id"):
            messages[message["id"]] = message
            stamps[message["id"]] = at
            written_ttl, ttl = cache_ttl(message["usage"], written_ttl)
            last_cache = (at, ttl)
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                acts.append([entry.get("timestamp"), describe_tool(block.get("name"), block.get("input"))])
        text = prompt_text(entry).strip()
        if text:
            last_text = text
    tokens = {"input": 0, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0}
    cost, unpriced, model, spend, sizes = 0.0, False, None, {}, []
    for message_id, message in messages.items():
        usage = message["usage"]
        split = usage.get("cache_creation") or {}
        write_1h = split.get("ephemeral_1h_input_tokens", 0) or 0
        tokens["input"] += usage.get("input_tokens", 0) or 0
        tokens["output"] += usage.get("output_tokens", 0) or 0
        tokens["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
        tokens["cache_write_1h"] += write_1h
        tokens["cache_write_5m"] += (usage.get("cache_creation_input_tokens", 0) or 0) - write_1h
        sizes.append((usage.get("input_tokens", 0) or 0) + (usage.get("cache_read_input_tokens", 0) or 0)
                     + (usage.get("cache_creation_input_tokens", 0) or 0))
        model = message.get("model") or model
        price = price_claude(message.get("model"), usage, prices)
        if price is None:
            unpriced = True
        else:
            cost += price
            if stamps.get(message_id):
                bucket = int(stamps[message_id] // BUCKET_SECONDS)
                spend[bucket] = spend.get(bucket, 0.0) + price
    done = bool(last_entry and last_entry.get("type") == "assistant"
                and (last_entry.get("message") or {}).get("stop_reason") == "end_turn")
    context = {"last": sizes[-1], "peak": max(sizes)} if sizes else None
    cache = {"last_at": int(last_cache[0]), "ttl_s": last_cache[1]} if last_cache and last_cache[0] is not None else None
    return {"tokens": tokens, "cost_usd": None if unpriced else round(cost, 6), "model": model,
            "report": last_text[-6000:], "started": first_at, "ended": last_at if done else None, "done": done,
            "acts": [act for act in reversed(acts[-6:])], "spend": spend, "context": context, "cache": cache}


def parse_subagent(path, prices):
    with open(path, encoding="utf-8", errors="replace") as handle:
        first = handle.readline()
        try:
            prompt = prompt_text(json.loads(first))
        except json.JSONDecodeError:
            return None
        tag = RUN_TAG.search(prompt[:600])
        if not tag:
            return None
        parsed = parse_messages(handle, prices)
    meta = read_json(path[: -len(".jsonl")] + ".meta.json", {}) or {}
    return {"tag_run": tag.group(1), "task": tag.group(2) or "-", "role": tag.group(3) or meta.get("agentType") or "agent",
            "description": meta.get("description") or "", **parsed}


def cached_parse(path, parser, prices):
    stat = os.stat(path)
    key = (path, stat.st_mtime, stat.st_size)
    if key not in _TRANSCRIPTS:
        _TRANSCRIPTS[key] = parser(path, prices)
    return _TRANSCRIPTS[key], stat.st_mtime


def same_run(tag_run, run_dir, run_id):
    if os.path.isabs(tag_run):
        return os.path.realpath(tag_run) == os.path.realpath(run_dir)
    return tag_run == run_id


def claude_agents(config_dir, runs, prices):
    """{run id: [agent]} for every tagged subagent transcript, plus each run's chief session."""
    found = {run["id"]: [] for run in runs}
    if not runs:
        return found
    since = min(parse_time(run["started"]) or 0 for run in runs) - 300
    for path in glob.glob(os.path.join(config_dir, "projects", "*", "*", "subagents", "agent-*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            parsed, mtime = cached_parse(path, parse_subagent, prices)
        except OSError:
            continue
        if not parsed:
            continue
        for run in runs:
            if same_run(parsed["tag_run"], run["dir"], run["id"]):
                status = "done" if parsed["done"] else ("stopped" if time.time() - mtime > STALE_SECONDS else "running")
                verdict = verdict_of(parsed["report"]) if parsed["done"] else None
                if verdict in ("BLOCKED", "NEEDS_CONTEXT"):
                    status = "blocked"
                elif verdict == "FAILED":
                    status = "failed"
                found[run["id"]].append({
                    "id": "claude-" + os.path.basename(path)[len("agent-"): -len(".jsonl")], "provider": "claude",
                    "model": parsed["model"], "task": parsed["task"], "role": parsed["role"],
                    "description": parsed["description"], "status": status, "started": parsed["started"],
                    "ended": parsed["ended"], "tokens": parsed["tokens"], "cost_usd": parsed["cost_usd"],
                    "report": parsed["report"], "verdict": verdict, "acts": parsed["acts"], "active": mtime,
                    "context": parsed["context"], "_spend": parsed["spend"],
                    "session_id": os.path.basename(os.path.dirname(os.path.dirname(path))),
                    "agent_id": os.path.basename(path)[len("agent-"): -len(".jsonl")], "_transcript": path})
                break
    for run in runs:
        chief = chief_session(config_dir, run, prices, since)
        if chief:
            found[run["id"]].append(chief)
    return found


def session_text(path):
    """A main session's transcript, or None when it never started a run; remembered until the file changes."""
    stat = os.stat(path)
    cached = _SESSIONS.get(path)
    if cached and cached[:2] == (stat.st_mtime, stat.st_size):
        return cached[2]
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    text = text if "chief-run.py start" in text else None  # never keep a transcript that is not ours
    _SESSIONS[path] = (stat.st_mtime, stat.st_size, text)
    return text


def session_transcript(config_dir, session_id):
    """The main-session transcript for a session id, or None; an id that is not plain is never globbed."""
    if not plain_session_id(session_id):
        return None
    matches = sorted(glob.glob(os.path.join(glob.escape(config_dir), "projects", "*", session_id + ".jsonl")))
    return matches[0] if matches else None


def jsonl_records(text, *markers):
    """The JSON records of the lines containing any marker; lines that do not parse are skipped."""
    for line in text.splitlines():
        if any(marker in line for marker in markers):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def chat_title(text):
    """The chat's title: the last non-empty custom title, else the last AI-written one."""
    custom = ai = None
    for record in jsonl_records(text, "custom-title", "ai-title"):
        kind = record.get("type")
        if kind not in ("custom-title", "ai-title"):
            continue
        value = record.get("customTitle" if kind == "custom-title" else "aiTitle")
        if isinstance(value, str) and value.strip():
            if kind == "custom-title":
                custom = value.strip()
            else:
                ai = value.strip()
    return custom or ai


def session_cwd(text):
    """The folder the session ran in: the cwd of its first record that has one."""
    for record in jsonl_records(text, '"cwd"'):
        if isinstance(record.get("cwd"), str) and record["cwd"]:
            return record["cwd"]
    return None


def chief_session(config_dir, run, prices, since):
    """The chief session.s own tokens: the main session that started this run, inside the run's time."""
    start = parse_time(run["started"])
    end = parse_time(run.get("ended"))
    found = None
    recorded = session_transcript(config_dir, run.get("session_id"))
    if recorded:
        try:
            text = session_text(recorded)
        except OSError:
            text = None
        if text and run["id"] in text:
            found = (recorded, text, True)
    if not found:
        for path in glob.glob(os.path.join(config_dir, "projects", "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < since:
                    continue
                text = session_text(path)
            except OSError:
                continue
            if text and run["id"] in text:
                found = (path, text, run["dir"] in text)  # an id alone is a loose match
                break
    if not found:
        return None
    path, text, sure = found
    parsed = parse_messages(text.splitlines(), prices, start=start - 120, end=end, sidechain=False)
    mtime = os.path.getmtime(path)
    status = "done" if run.get("ended") else ("running" if time.time() - mtime < STALE_SECONDS else "stopped")
    return {"id": "chief", "provider": "claude", "model": parsed["model"], "task": "-", "role": "chief",
            "description": "the chief session", "status": status,
            "started": run["started"], "ended": run.get("ended"), "tokens": parsed["tokens"],
            "cost_usd": parsed["cost_usd"], "report": "", "verdict": None, "acts": parsed["acts"][:3],
            "active": mtime, "context": parsed["context"], "_spend": parsed["spend"],
            "cache": None if run.get("ended") else parsed["cache"],
            "session_id": os.path.basename(path)[: -len(".jsonl")], "chat_title": chat_title(text),
            "cwd": session_cwd(text), "_transcript": path if sure else None}


# ---- Codex agents, from run-codex-job.py's records ----------------------------------------


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def unwrap_shell(command):
    """`/bin/zsh -lc "npm test"` -> `npm test`."""
    match = re.match(r'^\S*sh\s+-l?c\s+(["\'])(.*)\1\s*$', command or "", re.S)
    return match.group(2) if match else (command or "")


def codex_acts(log_path, limit=6):
    """The latest tool calls of a Codex job, newest first, from its events.jsonl. Only the newest carries a time."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-400:]
        mtime = os.path.getmtime(log_path)
    except (OSError, TypeError):
        return [], None
    acts, seen = [], set()
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") not in ("item.started", "item.completed") or item.get("id") in seen:
            continue
        kind, text = item.get("type"), None
        if kind == "command_execution":
            text = "Bash " + unwrap_shell(item.get("command")).strip().splitlines()[0][:90] if item.get("command") else "Bash"
        elif kind == "file_change":
            text = "Edit " + ", ".join(short_path(change.get("path", "")) for change in (item.get("changes") or [])[:2])
        elif kind == "mcp_tool_call":
            text = f"{item.get('server', 'mcp')}.{item.get('tool', '')}"
        elif kind == "web_search":
            text = f"WebSearch {str(item.get('query') or '')[:60]}"
        if text:
            seen.add(item.get("id"))
            acts.append([None, text])
    acts = list(reversed(acts[-limit:]))
    if acts:
        acts[0][0] = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return acts, mtime


def inside(folder, path):
    """Whether path is a real location within folder."""
    if not isinstance(path, str) or not path:
        return False
    root = os.path.realpath(folder)
    return os.path.commonpath([root, os.path.realpath(path)]) == root


def codex_agents(run_dir, prices):
    agents = []
    for path in sorted(glob.glob(os.path.join(run_dir, "agents", "codex-*.json"))):
        record = read_json(path)
        if not isinstance(record, dict):
            continue
        status = {"completed": "done"}.get(record.get("status"), record.get("status") or "running")
        if status == "running" and not process_alive(record.get("pid")):
            status = "stopped"
        usage = record.get("usage") or {}
        report = record.get("last_message") or ""
        verdict = verdict_of(report) if status == "done" else None
        if verdict in ("BLOCKED", "NEEDS_CONTEXT"):
            status = "blocked"
        elif verdict == "FAILED" or status == "failed":
            status = "failed"
        acts, active = codex_acts(record.get("log"))
        log = record.get("log")
        cost = price_codex(record.get("model"), record.get("usage"), prices)
        when = parse_time(record.get("ended") or record.get("started"))
        spend = {int(when // BUCKET_SECONDS): cost} if cost and when else {}
        agents.append({
            "id": os.path.basename(path)[: -len(".json")], "provider": "chatgpt", "model": record.get("model"),
            "effort": record.get("effort"), "task": record.get("task") or "-", "role": record.get("role") or "agent",
            "description": record.get("label") or "", "status": status, "started": record.get("started"),
            "ended": record.get("ended"), "thread_id": record.get("thread_id"), "resumed": bool(record.get("resumed")),
            "tokens": {key: usage.get(key, 0) or 0 for key in ("input", "cached", "output", "reasoning")},
            "cost_usd": cost, "report": report[-6000:], "verdict": verdict, "acts": acts,
            "active": active or parse_time(record.get("ended") or record.get("started")), "context": None, "_spend": spend,
            "_transcript": log if inside(run_dir, log) else None})
    latest = {}
    for agent in agents:
        if agent["thread_id"]:
            mark = agent["ended"] or agent["started"] or ""
            if agent["thread_id"] not in latest or mark >= latest[agent["thread_id"]][0]:
                latest[agent["thread_id"]] = (mark, agent["id"])
    for agent in agents:
        if agent["thread_id"] and latest[agent["thread_id"]][1] != agent["id"]:
            agent["superseded"] = True  # a later resume of the same thread reports its whole usage
    return agents


# ---- plan tasks, from the plan's Owns lines, the build ledger and the live agents -------------


TASK_HEAD = re.compile(r"^###\s+Task\s+(\d+[a-z]?)\s*:\s*(.+?)\s*$")
WAVE_LINE = re.compile(r"^\s*(Wave\s+\d+|Integration|Final)\s*(?:\([^)]*\))?\s*:\s*Tasks?\s+([0-9a-z,\s]+)", re.I)
LEDGER_LINE = re.compile(r"^Task\s+(\d+[a-z]?)\s*:\s*(.+)$")
ROOTS = {"src", "lib", "app"}
GROUPS = {"apps", "packages", "libs", "services", "modules"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "cypress", "playwright"}


def area_of(path):
    """The part of the codebase a path belongs to: tests, or the first meaningful folder."""
    parts = [part for part in path.strip("`").split("/") if part and "*" not in part]
    if any(part in TEST_DIRS for part in parts) or re.search(r"\.(test|spec)\.", path):
        return "tests"
    while parts and parts[0] in ROOTS and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[0] in GROUPS and len(parts) > 1:
        parts = parts[1:]
    if len(parts) <= 1:
        return "root"
    return parts[0]


def parse_plan(text):
    """[{id, name, owns, area, wave}] from a parallel-plans plan; empty when the plan has no task headings."""
    tasks, current, waves = [], None, {}
    for line in text.splitlines():
        head = TASK_HEAD.match(line)
        if head:
            current = {"id": head.group(1), "name": head.group(2)[:90], "owns": [], "area": None, "wave": None}
            tasks.append(current)
            continue
        wave = WAVE_LINE.match(line)
        if wave:
            for task_id in re.findall(r"\d+[a-z]?", wave.group(2)):
                waves.setdefault(task_id, wave.group(1).strip().capitalize() if not wave.group(1).lower().startswith("wave") else wave.group(1).strip())
        if current and line.strip().startswith("**Owns:**"):
            current["owns"] = re.findall(r"`([^`]+)`", line)
    for task in tasks:
        areas = [area_of(path) for path in task["owns"]]
        code = [area for area in areas if area != "tests"] or areas  # a task's code names it; tests alone make it "tests"
        task["area"] = max(sorted(set(code)), key=code.count) if code else None
        task["wave"] = waves.get(task["id"])
    return tasks


def ledger_states(text):
    """{task id: (state, fix round)} from a build ledger's `Task N: ...` lines; the last line about a task wins."""
    states = {}
    for line in text.splitlines():
        match = LEDGER_LINE.match(line.strip())
        if not match:
            continue
        task_id, rest = match.group(1), match.group(2).lower()
        previous_round = states.get(task_id, (None, 0))[1]
        if rest.startswith("complete"):
            states[task_id] = ("done", previous_round)
        elif "blocked" in rest or "parked" in rest:
            states[task_id] = ("blocked", previous_round)
        elif rest.startswith("fix round"):
            number = re.match(r"fix round\s+(\d+)", rest)
            states[task_id] = ("fix", int(number.group(1)) if number else previous_round + 1)
        elif rest.startswith("review") or "review dispatched" in rest:
            states[task_id] = ("review", previous_round)
        elif rest.startswith(("dispatched", "implemented")):
            states[task_id] = ("build", previous_round)
    return states


ROLE_KINDS = ("chief", "research", "plan", "implement", "review", "fix", "git")


def role_kind(role):
    """The section a brief's role belongs to: `implementer` -> implement, `plan-review` -> review, `fixer` -> fix."""
    role = (role or "").lower()
    if "review" in role:
        return "review"
    for kind in ROLE_KINDS:
        if role.startswith(kind[:4]):
            return kind
    if role in ("steward", "merge", "commit", "push"):
        return "git"
    return "other"


def run_tasks(run, agents):
    """The run's plan tasks with a status each: a live agent first, then the ledger, else waiting."""
    plan = run.get("plan")
    if not plan:
        return []
    try:
        with open(os.path.join(run["repo"], plan), encoding="utf-8", errors="replace") as handle:
            tasks = parse_plan(handle.read())
    except OSError:
        return []
    ledger = {}
    if run.get("workspace"):
        try:
            with open(os.path.join(run["repo"], run["workspace"], "progress.md"), encoding="utf-8", errors="replace") as handle:
                ledger = ledger_states(handle.read())
        except OSError:
            pass
    live = {"implement": "build", "review": "review", "fix": "fix"}
    for agent in agents:
        agent.setdefault("kind", role_kind(agent["role"]))
    for task in tasks:
        state, fix_round = ledger.get(task["id"], ("queued", 0))
        mine = [agent for agent in agents if agent["task"] == task["id"]]
        for agent in mine:
            if agent["status"] == "running" and agent["kind"] in live:
                state = live[agent["kind"]]
            elif agent["status"] in ("blocked", "failed") and state != "done":
                state = "blocked"
        fixes = [agent for agent in mine if agent["kind"] == "fix"]
        task.update(status=state, round=max(fix_round, len(fixes)),
                    who=next((agent["model"] for agent in reversed(mine) if agent["kind"] in ("implement", "fix")), None))
        task.pop("owns")
    return tasks


# ---- the board ----------------------------------------------------------------------------


def token_count(agent):
    tokens = agent["tokens"]
    if agent["provider"] == "chatgpt":
        return tokens.get("input", 0) + tokens.get("output", 0)
    return sum(tokens.values())


def totals_for(agents):
    totals = {name: {"agents": 0, "tokens": 0, "cost_usd": 0.0, "unpriced": 0} for name in PROVIDERS}
    for agent in agents:
        bucket = totals[agent["provider"]]
        bucket["agents"] += 1
        if agent.get("superseded"):
            continue
        bucket["tokens"] += token_count(agent)
        if agent["cost_usd"] is None:
            bucket["unpriced"] += 1
        else:
            bucket["cost_usd"] += agent["cost_usd"]
    for bucket in totals.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    totals["total_cost_usd"] = round(sum(totals[name]["cost_usd"] for name in PROVIDERS), 6)
    totals["estimate"] = True
    return totals


def read_run(run_dir, repo):
    state = read_json(os.path.join(run_dir, "run.json"))
    if not isinstance(state, dict) or not state.get("id"):
        return None
    repo = os.path.realpath(repo or state.get("repo") or os.path.dirname(os.path.dirname(os.path.dirname(run_dir))))
    return {**state, "dir": run_dir, "repo": repo, "key": run_key(repo, state["id"])}


def list_runs(repo=None, days=None):
    """Every registered run on this machine plus the given checkout's own, newest first.

    Pointers whose run folder is gone (a removed worktree) are dropped. With days, runs that
    ended longer ago are left out; running and paused runs always stay. Archived runs are
    included and marked; build_board decides what to show."""
    found = {}
    for path in glob.glob(os.path.join(registry_dir(), "*.json")):
        pointer = read_json(path)
        run_dir = isinstance(pointer, dict) and pointer.get("dir")
        run = read_run(run_dir, pointer.get("repo")) if run_dir else None
        if run is None:
            if not run_dir or not os.path.exists(run_dir):
                try:
                    os.remove(path)
                except OSError:
                    pass
            continue
        run["archived"] = pointer.get("archived")
        run["archived_totals"] = pointer.get("totals")
        found[os.path.realpath(run_dir)] = run
    if repo:
        for run_dir in glob.glob(os.path.join(runs_root(repo), "*")):
            if os.path.realpath(run_dir) not in found:
                run = read_run(run_dir, repo)
                if run:
                    found[os.path.realpath(run_dir)] = run
    cutoff = time.time() - days * 86400 if days else None
    runs = [run for run in found.values()
            if not cutoff or not run.get("ended") or (parse_time(run["ended"]) or 0) >= cutoff]
    runs.sort(key=lambda run: run.get("started") or "", reverse=True)
    return runs


def git_state(repo):
    try:
        spec = importlib.util.spec_from_file_location("git_check", GIT_CHECK_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.report(repo)
        return {"branch": result.get("branch"), "upstream": result.get("upstream"), "warnings": result.get("warnings", [])}
    except Exception as error:  # the board still renders without git
        return {"error": str(error)}


def default_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def build_cards(runs, config_dir=None, prices=None):
    prices = load_prices() if prices is None else prices
    claude = claude_agents(config_dir or default_config_dir(), runs, prices)
    cards = []
    for run in runs:
        agents = claude[run["id"]] + codex_agents(run["dir"], prices)
        agents.sort(key=lambda agent: agent.get("started") or "")
        for agent in agents:
            agent["kind"] = role_kind(agent["role"])
        card = {key: run.get(key) for key in ("key", "id", "title", "source", "phase", "phases", "started", "ended",
                                              "plan", "workspace", "dir", "repo", "archived")}
        chief_row = next((agent for agent in agents if agent["id"] == "chief"), None) or {}
        card.update(session_id=chief_row.get("session_id") or plain_session_id(run.get("session_id")),
                    chat_title=chief_row.get("chat_title"), cwd=chief_row.get("cwd") or run.get("repo"),
                    cache=chief_row.get("cache"))
        last = max([parse_time(entry.get("at")) or 0 for entry in run.get("phases") or []]
                   + [agent.get("active") or 0 for agent in agents] + [parse_time(run.get("started")) or 0])
        working = any(agent["status"] == "running" and agent["kind"] != "chief" for agent in agents)
        open_run = run.get("phase") not in ("done", "paused")
        card.update(agents=agents, totals=totals_for(agents), tasks=run_tasks(run, agents),
                    last_activity=datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None,
                    quiet=bool(open_run and not working and last and time.time() - last > QUIET_SECONDS))
        cards.append(card)
    return cards


def spend_summary(agents, archived_total, now=None):
    """All-runs spend, the last 24 hours in 10-minute buckets, and the burn rate over the last hour."""
    now = now or time.time()
    current = int(now // BUCKET_SECONDS)
    buckets = [0.0] * 144
    for agent in agents:
        if agent.get("superseded"):
            continue
        for bucket, cost in (agent.get("_spend") or {}).items():
            age = current - int(bucket)
            if 0 <= age < 144:
                buckets[143 - age] += cost
    every = totals_for(agents)["total_cost_usd"]
    return {"all_runs": round(every + archived_total, 4), "last_24h": round(sum(buckets), 4),
            "burn_per_hour": round(sum(buckets[-6:]), 4), "buckets": [round(value, 4) for value in buckets]}


def is_archived(run, days):
    if run.get("archived"):
        return True
    ended = parse_time(run.get("ended"))
    return bool(days and ended and time.time() - ended > days * 86400)


def build_board(repo=None, config_dir=None, prices=None, with_git=True, days=None, git=None, archived=False):
    """Every run on this machine; archived=True lists the archived ones instead. git(repo) supplies each
    checkout's git state; with_git=False skips it. Runs that ended more than `days` ago count as archived."""
    every_run = list_runs(repo)
    shown = [run for run in every_run if is_archived(run, days) == archived]
    stored = [run for run in every_run if run.get("archived") and run.get("archived_totals")]
    cards = build_cards(shown, config_dir, prices)
    agents = [agent for card in cards for agent in card["agents"]]
    archived_total = 0.0 if archived else sum((run["archived_totals"] or {}).get("total_cost_usd") or 0 for run in stored)
    spend = spend_summary(agents, archived_total)
    for agent in agents:
        agent.pop("_spend", None)
        agent["has_transcript"] = bool(agent.pop("_transcript", None))
    repos = []
    for path in dict.fromkeys(card["repo"] for card in cards):
        state = (git or git_state)(path) if with_git else None
        repos.append({"repo": path, "name": os.path.basename(path), "git": state})
    return {"generated": now_iso(), "repo": repo, "repos": repos, "runs": cards, "totals": totals_for(agents),
            "spend": spend, "accounts": read_usage_cache(), "days": days, "view": "archived" if archived else "active",
            "archived_count": sum(1 for run in every_run if is_archived(run, days)),
            "prices_note": "API-equivalent estimates from model-routing/prices.json; on a subscription the window use is what limits you"}


def archive_runs(repo, keys, archived=True, config_dir=None, prices=None):
    """Archive or restore runs by key or id. Returns the keys changed."""
    runs = list_runs(repo)
    changed = []
    for key in keys:
        run = find_run(runs, key)
        if not run:
            raise ValueError(f"no run {key}")
        totals = build_cards([run], config_dir, prices)[0]["totals"] if archived else None
        set_archived(run, archived, totals)
        changed.append(run["key"])
    return changed


def money(value):
    if value is None:
        return "unpriced"
    return "<$0.01" if 0 < value < 0.005 else f"${value:,.2f}"


def summary_markdown(repo, run_dir, config_dir=None, prices=None):
    run = read_run(os.path.normpath(run_dir), repo)
    if run is None:
        raise ValueError(f"no run {os.path.basename(os.path.normpath(run_dir))}")
    card = build_cards([run], config_dir, prices)[0]
    for agent in card["agents"]:
        agent.pop("_spend", None)
    lines = [f"Run {card['id']}: {card['title']}", "",
             "| provider | model | role | task | status | tokens | API-eq $ |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for agent in card["agents"]:
        tokens = agent["tokens"]
        if agent["provider"] == "chatgpt":
            detail = f"in {tokens['input']:,} (cached {tokens['cached']:,}) / out {tokens['output']:,} (reasoning {tokens['reasoning']:,})"
        else:
            detail = (f"in {tokens['input']:,} / cache w {tokens['cache_write_5m'] + tokens['cache_write_1h']:,}"
                      f" r {tokens['cache_read']:,} / out {tokens['output']:,}")
        cost = "in the resume below" if agent.get("superseded") else money(agent["cost_usd"])
        lines.append(f"| {agent['provider']} | {agent['model'] or '?'} | {agent['role']} | {agent['task']} | {agent['status']} | {detail} | {cost} |")
    totals = card["totals"]
    for name, label in (("claude", "Claude"), ("chatgpt", "ChatGPT")):
        bucket = totals[name]
        extra = f", {bucket['unpriced']} unpriced" if bucket["unpriced"] else ""
        lines.append(f"{label} subtotal: {money(bucket['cost_usd'])} over {bucket['agents']} agent(s), {bucket['tokens']:,} tokens{extra}")
    lines.append(f"Total: {money(totals['total_cost_usd'])}, an API-equivalent estimate; on subscriptions the window use below is what limits you.")
    snapshots = [entry for entry in card.get("phases") or [] if entry.get("usage")]
    if snapshots:
        first, last = snapshots[0], snapshots[-1]
        for name in PROVIDERS:
            before, after = (first["usage"].get(name) or {}), (last["usage"].get(name) or {})
            lines.append(f"{name} 5-hour window: {before.get('5h')}% at {first['phase']} -> {after.get('5h')}% at {last['phase']}; week {before.get('week')}% -> {after.get('week')}%")
    return "\n".join(lines)


# ---- files the dashboard may open ----------------------------------------------------------


def find_run(runs, key):
    """The run a dashboard link names: its key, or a bare run id when only one run has it."""
    if not key:
        return None
    exact = [run for run in runs if run["key"] == key]
    if exact:
        return exact[0]
    by_id = [run for run in runs if run["id"] == key]
    return by_id[0] if len(by_id) == 1 else None


# ---- resuming the chief chat from the dashboard ---------------------------------------------

OPEN_CHAT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$")  # stricter than SESSION_ID: never starts with "-", so never reads as an option
OPENED_CHATS = {}  # session id -> when this process last opened it; a chat opened within STALE_SECONDS counts as live
OPEN_CHAT_LOCK = threading.Lock()  # the check and the record happen under it, so two requests cannot both open one chat


def launch_terminal(cwd, session_id, run=subprocess.run, folder=None):
    """Open Terminal in `cwd` on `claude --resume <session_id>` through a one-use .command file that deletes
    itself. `open -a Terminal` on a file needs no Automation permission; osascript would ask for one.
    Returns the argv it ran; a failed launch removes the file and raises."""
    fd, path = tempfile.mkstemp(prefix="chief-chat-", suffix=".command", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\nrm -f -- {shlex.quote(path)}\n"
                         f"cd -- {shlex.quote(cwd)} && exec claude --resume {shlex.quote(session_id)}\n")
        os.chmod(path, 0o700)
        argv = ["open", "-a", "Terminal", path]
        run(argv, check=True, timeout=10)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return argv


def open_chat(repo, key, config_dir=None, prices=None, launch=None):
    """Resume a run's chief chat in Terminal. Returns (http status, json body). The client names only the run; the
    folder and session id come from the run's own record, and a chat that is still being written is never forked."""
    run = find_run(list_runs(repo), key)
    if not run:
        return 404, {"error": "run not found"}
    card = build_cards([run], config_dir, prices)[0]
    chief_row = next((agent for agent in card["agents"] if agent["id"] == "chief"), None)
    session = card.get("session_id")
    if not chief_row or not isinstance(session, str) or not OPEN_CHAT_ID.fullmatch(session):
        return 404, {"error": "no chief chat is recorded for this run"}
    cwd = card.get("cwd")
    if not isinstance(cwd, str) or not (os.path.isabs(cwd) and os.path.isdir(cwd)):
        return 404, {"error": "the chat's folder no longer exists"}
    path = session_transcript(config_dir or default_config_dir(), session)
    try:
        mtime = os.path.getmtime(path) if path else 0
    except OSError:
        mtime = 0
    # The file's mtime counts as well as the card's `active`: `active` can stop at the run's end or its last message.
    if chief_row.get("status") == "running" or time.time() - max(chief_row.get("active") or 0, mtime) < STALE_SECONDS:
        return 409, {"error": "live"}
    if sys.platform != "darwin":
        return 501, {"error": "Open chat needs macOS Terminal"}
    # A chat this process opened is live from the moment it is opened, though its transcript has not changed yet
    # (a double click, or a second click before the user types). Reserve it before the launch, under the lock.
    with OPEN_CHAT_LOCK:
        now = time.time()
        for old in [sid for sid, at in OPENED_CHATS.items() if now - at >= STALE_SECONDS]:
            del OPENED_CHATS[old]
        if session in OPENED_CHATS:
            return 409, {"error": "live"}
        OPENED_CHATS[session] = now
    launched = False
    try:
        (launch or launch_terminal)(cwd, session)
        launched = True
    except (OSError, subprocess.SubprocessError) as error:
        return 500, {"error": f"Terminal did not open: {error}"}
    finally:
        if not launched:  # nothing opened, so nothing to protect: the next click may try again
            with OPEN_CHAT_LOCK:
                OPENED_CHATS.pop(session, None)
    return 200, {"opened": "Terminal"}


def resolve_file(run, name):
    """A file the dashboard may show for this run, inside the run's own checkout, or None."""
    if not run:
        return None
    repo, run_dir, state = run["repo"], run["dir"], run
    fixed = {"task": os.path.join(run_dir, "task.md"), "run": os.path.join(run_dir, "run.json")}
    if name in fixed:
        return fixed[name]
    targets = {"plan": state.get("plan"),
               "progress": state.get("workspace") and os.path.join(state["workspace"], "progress.md"),
               "handoff": state.get("workspace") and os.path.join(state["workspace"], "handoff.md")}
    target = targets.get(name)
    if not target:
        return None
    path = os.path.realpath(os.path.join(repo, target))
    return path if path.startswith(os.path.realpath(repo) + os.sep) and os.path.isfile(path) else None


def resolve_transcript(run, agent_id, config_dir=None, prices=None):
    """The transcript of an agent this run knows by id, or None. The id is looked up in the board's own agents,
    never turned into a path, and the path is checked against the allowed folders here, at serve time."""
    if not run or not agent_id:
        return None
    agent = next((row for row in build_cards([run], config_dir, prices)[0]["agents"] if row["id"] == agent_id), None)
    path = agent.get("_transcript") if agent else None
    if not isinstance(path, str) or not path:
        return None
    real = os.path.realpath(path)
    projects = os.path.join(os.path.realpath(config_dir or default_config_dir()), "projects")
    if os.path.isfile(real) and (inside(projects, real) or inside(run["dir"], real)):
        return real
    return None


def tool_result_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))


def render_entry(entry, line):
    """One transcript record as text, or None when it has nothing to show."""
    message = entry.get("message")
    if message is None:
        return f"[{entry.get('type') or '?'}] {line.strip()[:2000]}"
    if entry.get("type") not in ("user", "assistant") or not isinstance(message, dict):
        return None
    parts = [f"[{entry.get('timestamp') or ''}] {entry['type']}"]
    content = message.get("content")
    for item in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or "")[:4000])
        elif item.get("type") == "tool_use":
            parts.append("→ " + describe_tool(item.get("name"), item.get("input")))
        elif item.get("type") == "tool_result":
            parts.append(tool_result_text(item.get("content"))[:2000])
    return "\n".join(parts)


def render_transcript(path, limit=300):
    """A transcript as plain text, the last `limit` entries, each tool call on one line. A record that
    cannot be rendered is skipped, never fatal."""
    blocks, total = collections.deque(maxlen=limit), 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
                block = render_entry(entry, line) if isinstance(entry, dict) else None
            except Exception:
                continue
            if block is not None:
                blocks.append(block)
                total += 1
    omitted = total - len(blocks)
    return "\n\n".join(([f"… {omitted} earlier entries omitted"] if omitted else []) + list(blocks))


# ---- the dashboard server ------------------------------------------------------------------


def refresh_usage_forever(minutes):
    """Keep model-routing's usage cache fresh for the gauges; reading usage spends no model turn."""
    try:
        spec = importlib.util.spec_from_file_location("check_account_usage", USAGE_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return
    while True:
        try:
            module.read_usage(minutes * 60)
        except Exception:
            pass
        time.sleep(60)


def host_allowed(handler):
    port = handler.server.server_port
    return handler.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")


def request_allowed(handler, data):
    """Whether a GET may be answered: the Host names this server (a rebinding page carries its own host), and
    for data the page's own header, which another site cannot add without a CORS preflight this server never answers."""
    if not host_allowed(handler):
        return False
    return not data or handler.headers.get("X-Chief-Dashboard") == "1"


class DashboardServer(http.server.ThreadingHTTPServer):
    """HTTPServer without the reverse DNS lookup of its own address (socket.getfqdn) on bind, which can stall
    for many seconds on macOS; the dashboard only ever listens on 127.0.0.1."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def dashboard_page():
    page = DASHBOARD.read_text(encoding="utf-8")
    try:
        robots = ROBOTS.read_text(encoding="utf-8")
    except OSError:
        robots = ""  # the board still works; the stage draws no figures
    return page.replace("<!--ROBOTS-->", robots, 1)


def dashboard_health(port, timeout=2):
    """The /health reply of a chief dashboard on this port, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return health if isinstance(health, dict) and health.get("app") == DASHBOARD_APP else None


def dashboard_running(port):
    """True when a chief dashboard at least as new as this script answers on this port."""
    health = dashboard_health(port)
    return bool(health) and health.get("version", 0) >= DASHBOARD_VERSION


def port_free(port):
    """True when nothing listens on the port. A connect probe: on macOS a test bind can succeed beside a listener."""
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def listener_pids(port):
    """Processes listening on the port, from lsof; empty when lsof is missing."""
    try:
        completed = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(pid) for pid in completed.stdout.split() if pid.isdigit()]


def command_line(pid):
    completed = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return completed.stdout.strip()


def stop_old_dashboard(port, health):
    """Stop an older chief dashboard holding the port. Anything that is not chief-run.py serve is left alone."""
    pids = [health["pid"]] if health and health.get("pid") else listener_pids(port)
    targets = [pid for pid in pids if health or ("chief-run.py" in command_line(pid) and " serve" in command_line(pid))]
    if not targets:
        return False
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if port_free(port):
            return True
        time.sleep(0.1)
    return False


def dashboard_log():
    return os.path.join(chief_home(), "dashboard.log")


def ensure_dashboard(port=None, wait=8):
    """Make sure the shared dashboard answers on the port: join it, replace an older one, or start one.

    Runs register through their pointer files, so a running dashboard needs no call to pick them
    up. Returns {"url", "server": running|started|replaced|failed, "detail"?}; never raises."""
    port = port or DEFAULT_PORT
    url = f"http://127.0.0.1:{port}/"
    if os.environ.get("CHIEF_DASHBOARD", "").lower() in ("off", "0", "no"):
        return {"url": url, "server": "off"}
    try:
        health = dashboard_health(port)
        if health and health.get("version", 0) >= DASHBOARD_VERSION:
            return {"url": url, "server": "running"}
        replaced = False
        if not port_free(port):
            if not stop_old_dashboard(port, health):
                return {"url": url, "server": "failed",
                        "detail": f"port {port} is held by something other than a chief dashboard; set CHIEF_DASHBOARD_PORT to another port"}
            replaced = True
        os.makedirs(chief_home(), exist_ok=True)
        with open(dashboard_log(), "a", encoding="utf-8") as log:
            subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "serve", "--port", str(port),
                              "--idle-hours", str(AUTO_IDLE_HOURS)],
                             cwd=chief_home(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)  # its own session: it outlives the chief's shell
        deadline = time.time() + wait
        while time.time() < deadline:
            if dashboard_running(port):
                return {"url": url, "server": "replaced" if replaced else "started"}
            time.sleep(0.2)
        return {"url": url, "server": "failed", "detail": f"the dashboard did not answer; see {dashboard_log()}"}
    except Exception as error:  # the run goes on without a dashboard
        return {"url": url, "server": "failed", "detail": str(error)}


def serve(repo, port, refresh_minutes=5, days=DEFAULT_DAYS, idle_hours=0):
    url = f"http://127.0.0.1:{port}/"
    if repo:
        register_checkout(repo)
    git_cache = {}
    last_request = {"at": time.time()}

    def cached_git(path):
        at, value = git_cache.get(path, (0, None))
        if time.time() - at > 15:
            value = git_state(path)
            git_cache[path] = (time.time(), value)
        return value

    class Handler(http.server.BaseHTTPRequestHandler):
        def send(self, status, body, content_type):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            data = url.path in ("/board.json", "/file", "/transcript")
            if not request_allowed(self, data):
                if not host_allowed(self):
                    body = f"Open the dashboard at http://127.0.0.1:{self.server.server_port}/ — this host name is not allowed."
                else:
                    body = "This page is out of date — reload it."
                return self.send(403, body, "text/plain; charset=utf-8")
            if url.path != "/health":
                last_request["at"] = time.time()
            if url.path in ("/", "/index.html"):
                return self.send(200, dashboard_page(), "text/html; charset=utf-8")
            if url.path.startswith("/fonts/") and url.path[len("/fonts/"):] in FONTS:
                try:
                    data = FONTS[url.path[len("/fonts/"):]].read_bytes()
                except OSError:
                    return self.send(404, "not found", "text/plain; charset=utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            if url.path == "/health":
                return self.send(200, json.dumps({"app": DASHBOARD_APP, "version": DASHBOARD_VERSION, "pid": os.getpid()}), "application/json")
            if url.path == "/board.json":
                archived = urllib.parse.parse_qs(url.query).get("view") == ["archived"]
                board = build_board(repo, days=days, git=cached_git, archived=archived)
                return self.send(200, json.dumps(board), "application/json")
            if url.path == "/file":
                query = urllib.parse.parse_qs(url.query)
                run = find_run(list_runs(repo), (query.get("run") or [""])[0])
                path = resolve_file(run, (query.get("name") or [""])[0])
                if not path:
                    return self.send(404, "not found", "text/plain; charset=utf-8")
                with open(path, encoding="utf-8", errors="replace") as handle:
                    return self.send(200, handle.read(), "text/plain; charset=utf-8")
            if url.path == "/transcript":
                query = urllib.parse.parse_qs(url.query)
                run = find_run(list_runs(repo), (query.get("run") or [""])[0])
                path = resolve_transcript(run, (query.get("agent") or [""])[0])
                if not path:
                    return self.send(404, "not found", "text/plain; charset=utf-8")
                try:
                    text = render_transcript(path)
                except OSError:
                    return self.send(404, "not found", "text/plain; charset=utf-8")
                return self.send(200, text, "text/plain; charset=utf-8")
            return self.send(404, "not found", "text/plain; charset=utf-8")

        def do_POST(self):
            # Only the dashboard page sends this header; a page on another site cannot add it without a
            # CORS preflight, which this server never answers.
            url = urllib.parse.urlparse(self.path)
            port = self.server.server_port
            origin = self.headers.get("Origin")
            if (not host_allowed(self) or (origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"))
                    or url.path not in ("/archive", "/open-chat") or self.headers.get("X-Chief-Dashboard") != "1"):
                return self.send(403, "forbidden", "text/plain; charset=utf-8")
            last_request["at"] = time.time()
            if url.path == "/open-chat":
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    return self.send(415, json.dumps({"error": "send application/json"}), "application/json")
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 65536)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    key = str(body.get("key") or "")  # the only field read: never a command, folder or session id
                except (ValueError, AttributeError):
                    return self.send(415, json.dumps({"error": "the body is not a JSON object"}), "application/json")
                try:
                    status, payload = open_chat(repo, key)
                except Exception as error:  # a run record the card code cannot read must not drop the connection
                    status, payload = 500, {"error": f"{type(error).__name__}: {error}"}
                return self.send(status, json.dumps(payload), "application/json")
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                body = json.loads(self.rfile.read(length) or b"{}")
                keys = [str(key) for key in body.get("keys") or []][:200]
                changed = archive_runs(repo, keys, archived=bool(body.get("archived", True)))
            except (ValueError, TypeError, AttributeError) as error:
                return self.send(400, json.dumps({"error": str(error)}), "application/json")
            return self.send(200, json.dumps({"changed": changed}), "application/json")

        def log_message(self, *args):
            pass

    try:
        server = DashboardServer(("127.0.0.1", port), Handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        if dashboard_running(port):
            print(f"Agent runs dashboard already running for every repo: {url}")
            return
        raise RuntimeError(f"port {port} is taken by something that is not a current chief dashboard (an older one"
                           " shows fewer runs): `chief-run.py dashboard` replaces an older one; otherwise pass --port")
    if refresh_minutes > 0:
        threading.Thread(target=refresh_usage_forever, args=(refresh_minutes,), daemon=True).start()
    if idle_hours > 0:
        def stop_when_idle():
            while time.time() - last_request["at"] < idle_hours * 3600:
                time.sleep(min(60, idle_hours * 3600 / 4))
            server.shutdown()  # the next chief run starts it again
        threading.Thread(target=stop_when_idle, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Agent runs dashboard for every repo on this machine: {url}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---- command line -------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="create a run from the task text on stdin, a file or an issue")
    start.add_argument("--title")
    source = start.add_mutually_exclusive_group()
    source.add_argument("--file", help="read the task from this file")
    source.add_argument("--issue", help="import a GitHub or GitLab issue: number or URL")
    phase = commands.add_parser("phase", help="record the phase the run enters")
    phase.add_argument("run")
    phase.add_argument("phase", choices=PHASES)
    phase.add_argument("--plan", help="the plan file, relative to the repo")
    phase.add_argument("--workspace", help="the plan workspace holding progress.md and handoff.md")
    summary = commands.add_parser("summary", help="the cost table for the report")
    summary.add_argument("run")
    commands.add_parser("register", help="add this checkout's existing runs to the shared dashboard")
    archive_command = commands.add_parser("archive", help="hide runs from the dashboard, or bring them back")
    archive_command.add_argument("runs", nargs="*", help="run ids or dashboard keys")
    archive_command.add_argument("--done", action="store_true", help="every finished run on this machine")
    archive_command.add_argument("--restore", action="store_true", help="bring the runs back instead")
    board_command = commands.add_parser("board", help="print the dashboard data")
    serve_command = commands.add_parser("serve", help="serve the dashboard for every repo on 127.0.0.1")
    serve_command.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_command.add_argument("--idle-hours", type=float, default=0,
                               help="exit after this many hours with no page open; 0 never")
    commands.add_parser("dashboard", help="start the shared dashboard in the background, or join the running one")
    for command in (board_command, serve_command):
        command.add_argument("--days", type=int, default=DEFAULT_DAYS,
                             help="leave out runs that ended more than this many days ago; 0 keeps all")
    serve_command.add_argument("--refresh-usage", type=int, default=5, metavar="MINUTES",
                               help="refresh both accounts' usage this often for the gauges; 0 turns it off")
    args = parser.parse_args()

    repo = git_toplevel()
    if not repo and args.command not in ("board", "serve", "dashboard", "archive"):
        print("chief-run: run it inside the project's git repository", file=sys.stderr)
        return 2
    try:
        if args.command == "start":
            if args.issue:
                title, text, origin = import_issue(repo, args.issue)
            elif args.file:
                with open(args.file, encoding="utf-8") as handle:
                    title, text, origin = None, handle.read(), f"file {args.file}"
            else:
                title, text, origin = None, sys.stdin.read(), "chat"
            run_dir = start_run(repo, text, title=args.title or title, source=origin)
            key = run_key(repo, os.path.basename(run_dir))
            server = ensure_dashboard()
            print(json.dumps({"run": run_dir, "id": os.path.basename(run_dir), "key": key,
                              "dashboard": f"{server['url']}?run={urllib.parse.quote(key)}",
                              "dashboard_server": server["server"], **({"dashboard_detail": server["detail"]} if "detail" in server else {}),
                              "tag": f"Run: {run_dir}   Task: <id>   Role: <role>"}))
        elif args.command in ("phase", "summary"):
            run_dir = resolve_run(repo, args.run)
            if not run_dir:
                raise ValueError(f"no run {args.run} under {runs_root(repo)}")
            if args.command == "phase":
                register_run(run_dir, repo)  # a run from before the registry joins the shared dashboard
                state = set_phase(run_dir, args.phase, plan=args.plan, workspace=args.workspace)
                result = {"id": state["id"], "phase": state["phase"]}
                if args.phase != "done":  # a resumed session, or a dashboard that went idle, comes back
                    server = ensure_dashboard()
                    result.update(dashboard=f"{server['url']}?run={urllib.parse.quote(run_key(repo, state['id']))}",
                                  dashboard_server=server["server"], **({"dashboard_detail": server["detail"]} if "detail" in server else {}))
                print(json.dumps(result))
            else:
                print(summary_markdown(repo, run_dir))
        elif args.command == "archive":
            keys = list(args.runs)
            if args.done:
                keys += [run["key"] for run in list_runs(repo) if run.get("phase") == "done" and not run.get("archived")]
            if not keys:
                raise ValueError("name the runs to archive, or pass --done")
            changed = archive_runs(repo, keys, archived=not args.restore)
            print(json.dumps({"restored" if args.restore else "archived": changed}))
        elif args.command == "register":
            print(json.dumps({"registered": register_checkout(repo), "registry": registry_dir()}))
        elif args.command == "board":
            board = build_board(repo, days=args.days or None)
            print(json.dumps(board))
        elif args.command == "dashboard":
            if repo:
                register_checkout(repo)
            print(json.dumps(ensure_dashboard()))
        else:
            serve(repo, args.port, args.refresh_usage, days=args.days or None, idle_hours=args.idle_hours)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"chief-run: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
