# shellcheck shell=bash
# Pins the maestro plugin packaging: both manifests and both marketplaces agree on name and
# version, the version matches the newest CHANGELOG entry (users only receive an update when
# it changes), every path a manifest names exists, and Codex never picks up Claude-only hooks.

json_get() {
  # json_get FILE EXPR: print a value from a JSON file; EXPR is Python applied to `d`.
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}

test_manifests_are_valid_json() {
  local f
  command -v python3 >/dev/null || return 0
  for f in .claude-plugin/plugin.json .claude-plugin/marketplace.json .codex-plugin/plugin.json \
    .agents/plugins/marketplace.json hooks/claude-code.json; do
    python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$REPO_ROOT/$f" || fail "$f is not valid JSON"
  done
}

test_everything_is_named_maestro() {
  command -v python3 >/dev/null || return 0
  assert_eq maestro "$(json_get "$REPO_ROOT/.claude-plugin/plugin.json" 'd["name"]')" "claude plugin name"
  assert_eq maestro "$(json_get "$REPO_ROOT/.codex-plugin/plugin.json" 'd["name"]')" "codex plugin name"
  assert_eq maestro "$(json_get "$REPO_ROOT/.claude-plugin/marketplace.json" 'd["name"]')" "claude marketplace name"
  assert_eq maestro "$(json_get "$REPO_ROOT/.agents/plugins/marketplace.json" 'd["name"]')" "codex marketplace name"
  assert_eq maestro "$(json_get "$REPO_ROOT/.claude-plugin/marketplace.json" '",".join(p["name"] for p in d["plugins"])')" "claude marketplace entry"
  assert_eq maestro "$(json_get "$REPO_ROOT/.agents/plugins/marketplace.json" '",".join(p["name"] for p in d["plugins"])')" "codex marketplace entry"
}

test_versions_match_the_changelog() {
  local claude codex changelog
  command -v python3 >/dev/null || return 0
  claude=$(json_get "$REPO_ROOT/.claude-plugin/plugin.json" 'd["version"]')
  codex=$(json_get "$REPO_ROOT/.codex-plugin/plugin.json" 'd["version"]')
  changelog=$(awk '/^## [0-9]/ { print $2; exit }' "$REPO_ROOT/CHANGELOG.md")
  assert_eq "$claude" "$codex" "codex plugin version"
  assert_eq "$changelog" "$claude" "newest CHANGELOG version vs plugin version"
}

# A top-level doc states the current version where it pins one: `maestro#v1.2.2`, a raw URL
# `maestro/v1.2.2/...`, a plugin cache path `maestro/1.2.2/...`, `maestro@1.2.2`, or the
# "Release: `v1.2.2`" line. Each of those must equal the plugin version. Bare mentions such as
# "since 1.1.0" and the CHANGELOG are history and are not read; `skills@1.7.2` is the CLI's version.
test_docs_state_the_current_version() {
  local want file hit found=0 stale=""
  command -v python3 >/dev/null || return 0
  want=$(json_get "$REPO_ROOT/.claude-plugin/plugin.json" 'd["version"]')
  for file in "$REPO_ROOT"/*.md; do
    case $file in */CHANGELOG.md) continue ;; esac
    while IFS= read -r hit; do
      [ -n "$hit" ] || continue
      found=$((found + 1))
      [ "${hit##*[!0-9.]}" = "$want" ] || stale="$stale
  $(basename "$file"):$hit"
    done <<EOF
$(grep -noE '(maestro[/#@]|Release: `)v?[0-9]+\.[0-9]+\.[0-9]+' "$file" || true)
EOF
  done
  [ "$found" -gt 0 ] || fail "no versioned references found in the docs; this check reads nothing"
  [ -z "$stale" ] || fail "docs pin a version other than the plugin's $want:$stale"
}

test_manifest_paths_exist() {
  local path
  command -v python3 >/dev/null || return 0
  for path in \
    "$(json_get "$REPO_ROOT/.claude-plugin/plugin.json" 'd["hooks"]')" \
    "$(json_get "$REPO_ROOT/.claude-plugin/plugin.json" 'd["icon"]')" \
    "$(json_get "$REPO_ROOT/.codex-plugin/plugin.json" 'd["skills"]')" \
    "$(json_get "$REPO_ROOT/.codex-plugin/plugin.json" 'd["interface"]["logo"]')" \
    "$(json_get "$REPO_ROOT/.codex-plugin/plugin.json" 'd["interface"]["composerIcon"]')"; do
    case $path in ./*) ;; *) fail "manifest path must start with ./: $path" ;; esac
    [ -e "$REPO_ROOT/$path" ] || fail "manifest path does not exist: $path"
  done
}

test_hooks_reach_the_plugin_scripts() {
  local body script
  body=$(cat "$REPO_ROOT/hooks/claude-code.json")
  assert_contains "$body" '${CLAUDE_PLUGIN_ROOT}/skills/model-routing/scripts/usage-before-spawn.py' "hook script path"
  assert_contains "$body" '--during-chief-run' "hook only runs during a chief run"
  script="$REPO_ROOT/skills/model-routing/scripts/usage-before-spawn.py"
  [ -f "$script" ] || fail "$script missing"
}

# Codex loads hooks/hooks.json by default; the hook is Claude Code only, and a Codex plugin
# with hooks is not eligible for OpenAI's public directory.
test_codex_gets_no_hooks() {
  [ ! -e "$REPO_ROOT/hooks/hooks.json" ] || fail "hooks/hooks.json would be loaded by Codex; keep Claude hooks in hooks/claude-code.json"
  assert_not_contains "$(cat "$REPO_ROOT/.codex-plugin/plugin.json")" '"hooks"' "codex manifest"
}

test_every_skill_ships_in_the_plugin() {
  local dir
  for dir in "$REPO_ROOT"/skills/*/; do
    [ -f "$dir/SKILL.md" ] || fail "$dir has no SKILL.md (the plugins load every folder under skills/)"
  done
  [ -f "$REPO_ROOT/agents/chief.md" ] || fail "agents/chief.md missing"
}
