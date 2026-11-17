# shellcheck shell=bash
# Zero-token guard: commit attribution is handled by settings (the SessionStart hook sets Claude
# Code's `attribution`) and checked by git-check.py, so no skill may instruct an agent to add a
# Co-Authored-By trailer or a "Generated with Claude" footer.

test_no_skill_adds_model_attribution() {
  local hits
  # git-check.py and its tests name these trailers on purpose, to warn about them.
  hits=$(grep -rniI --exclude-dir=tests --exclude=git-check.py -e 'co-authored-by' -e 'generated with \[claude' -e 'generated with claude' -e '🤖 generated' "$REPO_ROOT/skills" || true)
  [ -z "$hits" ] || fail "a skill adds model attribution: $hits"
}
