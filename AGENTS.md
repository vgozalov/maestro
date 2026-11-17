# maestro plugin source

- Behaviour with constant input and output belongs in a script, hook, config or settings, not in prompt, brief or skill text. Add prose to skills only for judgement a script cannot make.
- Before adding lines to a skill, brief template or prompt, check whether a script, hook, setting or test can enforce it instead. Skill text is loaded into agent context and costs tokens on every run.
- Example: commit attribution is set by Claude Code's `attribution` setting (maestro's SessionStart hook) and checked by git-check.py, not by prompt lines.
