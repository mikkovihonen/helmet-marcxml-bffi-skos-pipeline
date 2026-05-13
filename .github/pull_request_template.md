## Summary
<!-- 1-3 bullets: what changes, why -->

## Eval results
<!--
Required if this PR touches any of:
  - prompts/
  - gold/
  - src/bffi_pipeline/stages/judge.py
  - src/bffi_pipeline/eval/

Run `make eval LABEL=<short-id>` locally on the M5 Max and paste the
text output below. Compare against the previous main-branch run; flag
per-category accuracy regressions > 10 points as blockers.

If this PR doesn't touch the above paths, write "N/A — does not touch
the LLM-eval surface".
-->

```
N/A
```

## Checklist
- [ ] `make lint && make test` pass locally
- [ ] If this PR touches the judge, prompts, gold set, or eval code,
      `make eval` output is pasted above and compared against main
- [ ] Documentation updated (README / runbook / docstrings) if the
      user-facing surface changed
- [ ] If this PR ships a plan-phase commit, the plan's `Phase commits:` field is filled with the merge hash and the plan moved to the correct lifecycle folder if applicable
