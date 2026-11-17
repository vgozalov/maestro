# shellcheck shell=bash
# Runs the Python tests of scripts/disable-attribution.py, the plugin's SessionStart hook that
# turns off Claude Code's own commit/PR attribution once per plugin version.

test_disable_attribution_unit_tests() {
  local out
  command -v python3 >/dev/null || return 0
  out=$(python3 -B -m unittest discover -s "$REPO_ROOT/tests" -p 'test_disable_attribution.py' 2>&1) || fail "disable-attribution unit tests failed:
$out"
}

test_the_session_start_hook_is_registered_for_claude_code_only() {
  local body
  body=$(cat "$REPO_ROOT/hooks/claude-code.json")
  assert_contains "$body" '"SessionStart"' "SessionStart hook"
  assert_contains "$body" '${CLAUDE_PLUGIN_ROOT}/scripts/disable-attribution.py' "hook script path"
  [ -f "$REPO_ROOT/scripts/disable-attribution.py" ] || fail "scripts/disable-attribution.py missing"
}
