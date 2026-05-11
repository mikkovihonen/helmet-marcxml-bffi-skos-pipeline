# P-03 — M6 stall watchdog

**Status**: planning (graduated). See
[`docs/plans/in-progress/p-03-m6-stall-watchdog.md`](../plans/in-progress/p-03-m6-stall-watchdog.md)
for the full plan.
**Proposal-base commit**: `0263f9f`. To gauge drift before acting,
run `git diff 0263f9f..HEAD -- src/bffi_pipeline/stages/judge.py
src/bffi_pipeline/cli.py scripts/run-full-pipeline.sh`.

Material updates since drafting:

- `f50b99d` — added the "Empirical evidence: stall recovery" section
  below after the preview-373 pipeline's stall recovered cleanly on
  kill+resume, which shifts the design preference from "mark
  uncertain" to "kill and retry the same pair".
- This commit — graduated into the plan; Motivation + Empirical
  evidence preserved here, the Approach / Prerequisites / Risks /
  Open-questions content moved into the plan with execution detail
  (configurable timeout, structured event logging, retry
  semantics, overnight dry-run procedure).

## Motivation

Concrete incident — preview-373 pipeline, 2026-05-11 18:31 EEST: M6
reached 185/211 LLM-judged pairs and then **stopped writing decisions
for 35 minutes** before the operator noticed. The Ollama process was
alive and burning ~6 % CPU, the python judge had an established TCP
socket on `:11434`, and the in-flight rationale generation was
presumably looping or running away on a single pair. Nothing in the
pipeline was going to recover on its own — `set -e` only fires on a
non-zero exit, and the judge process never exited.

This failure mode is a hard problem for unattended overnight or
multi-day production runs. The 800 k-corpus pass needs M6 to chew
through tens of thousands of pairs; even a single ~hour stall buried
in the middle is invisible noise the operator only catches the next
morning. We need the pipeline to detect "no progress for a while" and
either abandon the stuck pair or interrupt the hung LLM call so the
cascade can fall through to the fallback model / mark `uncertain` /
move on.

## Empirical evidence: stall recovery

The same preview-373 incident gave a second useful data point. After
the operator killed the python judge tree at ~2 h of stall and
resumed the pipeline, the SQLite judge cache picked up at decision
186 — the same pair that had been stuck — and the LLM successfully
judged it within the normal ~28 s per-decision window. M6 then
walked through the remaining 25 escalate pairs at the steady-state
rate and finished cleanly.

That changes the design preference for what the watchdog should do
when it fires:

- **Before this evidence**, both options assumed the stuck pair was
  inherently pathological — the abort would mark it
  `uncertain` and move on. The cataloguer would manually review
  every `watchdog-aborted` pair afterwards.
- **After this evidence**, the stalls look transient (an Ollama
  state machine wedge that the kill-and-restart resolves, not an
  inherently bad pair). The right first move is **kill the in-
  flight call and retry the *same* pair on the *same* model**,
  treating watchdog abort like any other transient connection
  error in the existing 5/30/120-second exponential-backoff retry
  stack. Only if retry-on-same-model fails repeatedly (say, 3
  retries) does the pair escalate to the fallback model, and only
  if that also fails does it land in `uncertain`.

One-time stalls become invisible to the operator (the kill + retry
both happen in-process; the only operational signal is a slightly
longer per-pair latency on the affected pair, recorded in
provenance). Persistently pathological pairs still surface as
`uncertain` for manual review.

Both Option A and Option B accommodate this; the only change is
the abort handler reaching for the retry path instead of going
straight to `uncertain`. The same `stage="watchdog-aborted"`
provenance value still records every watchdog intervention so the
audit trail of "how often did this fire on the corpus?" stays
visible.

## Graduated to plan

Approach detail, prerequisites, risks, open questions, and the
sequenced execution steps live in
[`docs/plans/in-progress/p-03-m6-stall-watchdog.md`](../plans/in-progress/p-03-m6-stall-watchdog.md).
Refer to [`docs/plans/README.md`](../plans/README.md) for the
state-folder convention.

