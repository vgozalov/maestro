#!/usr/bin/env python3
"""SessionStart hook: turn off Claude Code's own commit/PR attribution, once per plugin version.

Claude Code has no install or update hook, so this runs at session start and keeps a marker with
the plugin version it last ran for. It adds "attribution": {"commit": "", "pr": ""} to the user
settings file only when that key is absent; an existing "attribution" (any value) and every other
key are left alone. Set MAESTRO_KEEP_ATTRIBUTION=1 to opt out.

stdout of a SessionStart hook enters the model's context, so this prints nothing, ever, and always
exits 0.
"""
import json
import os
import shutil
import sys
import tempfile

OFF = {"commit": "", "pr": ""}


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def plugin_version():
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            return str(json.load(f)["version"])
    except Exception:
        return "unknown"


def marker_path():
    return os.path.join(os.environ.get("CLAUDE_PLUGIN_DATA") or config_dir(), "maestro-attribution-version")


def write_atomic(path, text, mode):
    path = os.path.realpath(path)  # a symlinked settings file stays a symlink
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def apply(settings):
    if not os.path.exists(settings):
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        write_atomic(settings, dump({"attribution": dict(OFF)}), 0o644)
        return
    with open(settings, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "attribution" in data:
        return
    data["attribution"] = dict(OFF)
    backup = settings + ".maestro-bak"
    if not os.path.exists(backup):
        shutil.copy2(settings, backup)
    write_atomic(settings, dump(data), os.stat(settings).st_mode & 0o7777)


def main():
    if os.environ.get("MAESTRO_KEEP_ATTRIBUTION") == "1":
        return
    version = plugin_version()
    marker = marker_path()
    try:
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == version:
                return
    except OSError:
        pass
    try:
        apply(os.path.join(config_dir(), "settings.json"))
    except (ValueError, UnicodeDecodeError):
        pass  # not a JSON object we can read: change nothing, still mark this version done
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(version + "\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
