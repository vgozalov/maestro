# shellcheck shell=bash
# Pins the no-attribution rule: commits and PRs made through the plugin carry the repo's git
# user only. No skill tells an agent to add a Co-Authored-By trailer or a "Generated with
# Claude" line, and the rule sits wherever an agent is told to commit or open a PR (in
# write-briefs.md only in the Git steward block, since most briefs forbid committing).

RULE='no model author, Co-Authored-By or AI attribution'

test_no_skill_adds_model_attribution() {
  local hits
  # git-check.py and its tests name these trailers on purpose, to warn about them.
  hits=$(grep -rniI --exclude-dir=tests --exclude=git-check.py -e 'co-authored-by' -e 'generated with \[claude' -e 'generated with claude' -e '🤖 generated' "$REPO_ROOT/skills" | grep -vF "$RULE" || true)
  [ -z "$hits" ] || fail "a skill adds model attribution: $hits"
}

test_commit_steps_carry_the_rule() {
  local f
  for f in skills/parallel-build/implementer-prompt.md skills/finishing-a-development-branch/SKILL.md \
    skills/wrap-up/SKILL.md skills/model-routing/references/write-briefs.md; do
    grep -qF "$RULE" "$REPO_ROOT/$f" || fail "$f lacks the no-attribution rule"
  done
  assert_eq 1 "$(grep -cF "$RULE" "$REPO_ROOT/skills/model-routing/references/write-briefs.md")" "write-briefs rule count (Git steward only)"
}
