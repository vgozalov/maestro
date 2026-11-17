"""Tests for scripts/disable-attribution.py, the SessionStart hook that turns off Claude Code's
own commit/PR attribution once per plugin version. Every case runs the script as a subprocess
against temp dirs (CLAUDE_CONFIG_DIR, CLAUDE_PLUGIN_DATA, HOME); the real ~/.claude is never read."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "disable-attribution.py"
OFF = {"commit": "", "pr": ""}


class DisableAttributionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.config = root / "config"
        self.data = root / "data"
        self.home = root / "home"
        self.plugin = root / "plugin"
        self.home.mkdir()
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        self.set_version("1.2.0")
        self.settings = self.config / "settings.json"
        self.backup = self.config / "settings.json.maestro-bak"
        self.marker = None

    def tearDown(self):
        self.folder.cleanup()

    def set_version(self, version):
        (self.plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "maestro", "version": version}))

    def run_hook(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "MAESTRO_"))}
        env.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config), CLAUDE_PLUGIN_DATA=str(self.data),
                   CLAUDE_PLUGIN_ROOT=str(self.plugin))
        env.update(extra)
        env = {k: v for k, v in env.items() if v is not None}
        result = subprocess.run([sys.executable, "-B", str(SCRIPT)], env=env, input="{}", capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "stdout enters the model's context")
        self.assertEqual(result.stderr, "")
        return result

    def write_settings(self, text):
        self.config.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(text)

    def read_settings(self):
        return json.loads(self.settings.read_text())

    def test_a_missing_settings_file_is_created(self):
        self.run_hook()
        self.assertEqual(self.read_settings(), {"attribution": OFF})
        self.assertFalse(self.backup.exists())

    def test_an_existing_file_gets_the_key_and_keeps_everything_else(self):
        original = {"model": "opus", "includeCoAuthoredBy": True, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}, "n": [1, 2], "ü": "ü"}
        self.write_settings(json.dumps(original, indent=4))
        self.settings.chmod(0o640)
        before = self.settings.read_text()
        self.run_hook()
        data = self.read_settings()
        self.assertEqual(data.pop("attribution"), OFF)
        self.assertEqual(data, original)
        self.assertEqual(list(self.read_settings())[:-1], list(original))
        self.assertEqual(self.backup.read_text(), before)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o640)
        self.assertTrue(self.settings.read_text().startswith('{\n  "model"'), "2-space indentation")
        self.assertEqual([p.name for p in self.config.iterdir() if p.name.startswith(".")], [], "no temp file left behind")

    def test_an_existing_backup_is_not_overwritten(self):
        self.write_settings('{"a": 1}')
        self.backup.write_text("older backup")
        self.run_hook()
        self.assertEqual(self.backup.read_text(), "older backup")
        self.assertEqual(self.read_settings(), {"a": 1, "attribution": OFF})

    def test_an_existing_attribution_key_is_never_touched(self):
        for value in (False, "", "@handle", {"commit": "mine"}, {}, None, True):
            with self.subTest(value=value):
                self.marker_reset()
                text = json.dumps({"attribution": value, "includeCoAuthoredBy": True}, indent=1)
                self.write_settings(text)
                if self.backup.exists():
                    self.backup.unlink()
                self.run_hook()
                self.assertEqual(self.settings.read_text(), text)
                self.assertFalse(self.backup.exists())

    def test_settings_that_are_not_a_json_object_are_left_alone(self):
        for text in ("{not json", '{"a": 1, // comment\n}', "[]", '"x"', "", "null", "﻿{}"):
            with self.subTest(text=text):
                self.marker_reset()
                self.write_settings(text)
                self.run_hook()
                self.assertEqual(self.settings.read_text(), text)
                self.assertFalse(self.backup.exists())
                self.assertTrue(self.marker_file().exists(), "marker is still written")

    def test_the_opt_out_variable_changes_nothing(self):
        self.run_hook(MAESTRO_KEEP_ATTRIBUTION="1")
        self.assertFalse(self.settings.exists())
        self.write_settings('{"a": 1}')
        self.run_hook(MAESTRO_KEEP_ATTRIBUTION="1")
        self.assertEqual(self.settings.read_text(), '{"a": 1}')
        self.assertFalse(self.marker_file().exists(), "opting out does not use up this version's run")
        self.run_hook()
        self.assertEqual(self.read_settings(), {"a": 1, "attribution": OFF})

    def test_it_runs_once_per_plugin_version(self):
        self.run_hook()
        self.assertEqual(self.marker_file().read_text().strip(), "1.2.0")
        self.settings.write_text('{"mine": true}')
        self.run_hook()
        self.assertEqual(self.settings.read_text(), '{"mine": true}', "same version: no write")
        self.set_version("1.3.0")
        self.run_hook()
        self.assertEqual(self.read_settings(), {"mine": True, "attribution": OFF}, "new version runs again")
        self.assertEqual(self.marker_file().read_text().strip(), "1.3.0")

    def test_a_new_version_still_respects_an_existing_key(self):
        self.run_hook()
        self.settings.write_text('{"attribution": "@me"}')
        self.set_version("1.3.0")
        self.run_hook()
        self.assertEqual(self.read_settings(), {"attribution": "@me"})

    def test_without_the_data_variable_the_marker_lives_under_the_claude_dir(self):
        self.run_hook(CLAUDE_PLUGIN_DATA=None, CLAUDE_CONFIG_DIR=None)
        claude = self.home / ".claude"
        self.assertEqual(json.loads((claude / "settings.json").read_text()), {"attribution": OFF})
        markers = [p for p in claude.iterdir() if p.name != "settings.json"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].read_text().strip(), "1.2.0")

    def test_a_symlinked_settings_file_stays_a_symlink(self):
        target = Path(self.folder.name) / "dotfiles-settings.json"
        target.write_text('{"a": 1}')
        self.config.mkdir()
        self.settings.symlink_to(target)
        self.run_hook()
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(json.loads(target.read_text()), {"a": 1, "attribution": OFF})

    def test_it_stays_silent_and_exits_zero_when_things_break(self):
        self.config.parent.joinpath("config").write_text("a file where the config dir should be")
        self.run_hook()
        self.run_hook(CLAUDE_PLUGIN_ROOT=str(Path(self.folder.name) / "nowhere"))

    def test_a_missing_plugin_manifest_does_not_stop_it(self):
        (self.plugin / ".claude-plugin" / "plugin.json").unlink()
        self.run_hook(CLAUDE_PLUGIN_ROOT=None)
        self.assertEqual(self.read_settings(), {"attribution": OFF})

    def marker_file(self):
        found = [p for p in self.data.glob("*") if p.is_file()]
        self.assertLessEqual(len(found), 1)
        return found[0] if found else self.data / "no-marker-yet"

    def marker_reset(self):
        for p in self.data.glob("*"):
            p.unlink()


if __name__ == "__main__":
    unittest.main()
