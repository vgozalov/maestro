# shellcheck shell=bash
# Pins the no-attribution rule: commits and PRs made through the plugin carry the repo's git
# user only. No skill tells an agent to add Co-Authored-By or "Generated with", and the rule
# line sits in every place an agent is told to commit or open a PR.

RULE='never set a model as author or committer'

test_no_skill_adds_model_attribution() {
  local hits
  hits=$(grep -rniI -e 'co-authored-by' -e 'generated with' -e 'commit trailer' "$REPO_ROOT/skills" | grep -vF "$RULE" || true)
  [ -z "$hits" ] || fail "a skill adds model attribution: $hits"
}

test_commit_steps_carry_the_rule() {
  local f
  for f in skills/parallel-build/implementer-prompt.md skills/finishing-a-development-branch/SKILL.md \
    skills/wrap-up/SKILL.md skills/model-routing/references/write-briefs.md; do
    grep -qF "$RULE" "$REPO_ROOT/$f" || fail "$f lacks the no-attribution rule"
  done
  assert_eq 2 "$(grep -cF "$RULE" "$REPO_ROOT/skills/model-routing/references/write-briefs.md")" "write-briefs rule count (Common and Git steward)"
}
