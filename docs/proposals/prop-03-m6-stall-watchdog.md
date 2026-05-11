# P-03 — M6 stall watchdog

**Status**: proposed.
**Scope**: half a day for the in-process variant (Option A);
~1-2 days for the heartbeat watchdog (Option B). Pick one; they're
not additive.
**Proposal-base commit**: `0263f9f`. To gauge drift before acting,
run `git diff 0263f9f..HEAD -- src/bffi_pipeline/stages/judge.py
src/bffi_pipeline/cli.py scripts/run-full-pipeline.sh`.

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

## Approach

Two options of differing investment, both with the same observable
behavior at the cascade level: a stuck pair gets abandoned, recorded
in provenance with a `stage="watchdog-aborted"` value, and the next
pair starts being judged.

### Option A — In-process per-call timeout (recommended MVP)

LangChain's `ChatOpenAI` accepts a `timeout` parameter that propagates
to the underlying `httpx` client. Wire it up so:

1. `Settings` (in `src/bffi_pipeline/config.py`) gains
   `llm_call_timeout_seconds: int = 90` (configurable via
   `LLM_CALL_TIMEOUT_SECONDS`). 90 seconds is roughly 3× the median
   M6 call observed on this machine; calls below 270 seconds stay in
   the prompt-cache window if we move to vllm-mlx (P-02), so the
   ceiling matches our cache-window math.

2. `_build_chain` in `stages/judge.py` passes the timeout through to
   `ChatOpenAI(..., timeout=settings.llm_call_timeout_seconds, ...)`.

3. The existing `Exception` catch in `judge_pair` / `cascade_judge`
   already handles upstream errors gracefully — a `ReadTimeout` from
   httpx fits naturally as a cascade trigger:
   - Primary times out → escalate to fallback model.
   - Fallback times out → record `decision="uncertain"`,
     `stage="watchdog-aborted"`, `latency_seconds=<elapsed>`, with
     a synthetic rationale `"M6 watchdog aborted after N s — manual
     review required"`. Provenance carries the abort signal so the
     audit trail can find every watchdog-handled pair.

4. A new CLI flag `bffi-pipeline judge --abort-budget-seconds N`
   overrides the settings value per run, defaulting to the env value.
   Helpful when an operator wants to be aggressive on a particular
   batch.

5. Tests: monkeypatch the chain to raise `httpx.ReadTimeout`; assert
   the cascade transitions correctly and provenance records the
   `watchdog-aborted` stage.

This catches the obvious "single LLM call wall-time exceeded the
budget" case, which is what the preview-373 incident was. **It does
not** catch the more pathological "model is producing one token every
20 seconds, never hits the budget but is effectively useless" case —
for that, see Option B.

### Option B — Out-of-process heartbeat watchdog

A companion CLI: `bffi-pipeline judge-watchdog --decisions-path
<path> --max-stall-seconds N --target-pid <pid>` that tails the
`judge-decisions.jsonl` file and:

1. Records the mtime of the decisions file each tick.
2. If `now - last_mtime > max_stall_seconds`, sends a custom signal
   (e.g. `SIGUSR1`) to the target judge process.
3. The judge process (modified) installs a `SIGUSR1` handler that
   cancels the current httpx call, records the abandoned pair as
   `watchdog-aborted` in provenance, and moves on.
4. The watchdog itself exits when it sees `PIPELINE_DONE` in the
   pipeline log or the target PID disappears.

The shell driver (`scripts/run-full-pipeline.sh`) starts the
watchdog as a background sibling of the M6 invocation:

```bash
uv run bffi-pipeline judge-watchdog \
    --decisions-path "$BFFI_DATA_DIR/judge-decisions.jsonl" \
    --max-stall-seconds 300 \
    --target-pid $$ &
WATCHDOG_PID=$!
trap "kill $WATCHDOG_PID 2>/dev/null" EXIT
uv run bffi-pipeline judge ...
```

This catches both the "single hung call" *and* the "slow-but-not-
dead" cases that Option A misses, at the cost of:

- A second process to debug.
- Cross-process signaling correctness (signal arrives mid-write to
  `judge-decisions.jsonl` → may corrupt the half-written line).
  Mitigation: the judge writes via `_append_jsonl` atomically; the
  signal handler defers until the atomic write completes.

Practical recommendation: ship Option A as the MVP because it
addresses the actual incident, and only ship Option B if the
slow-but-not-dead pathology shows up after Option A is in place.

## Prerequisites

- `httpx.ReadTimeout` propagation through `langchain-openai` —
  confirm the current pinned version surfaces this as a recoverable
  exception rather than swallowing it. (A 5-line smoke test resolves
  this question.)
- A `bffi-prov:WatchdogAbortActivity` class (or just a new
  `bffi-prov:stage = "watchdog-aborted"` literal in the existing
  Activity ontology) decided in advance — provenance grammar shouldn't
  be invented at implementation time.

## Risks

- **Budget too tight → false aborts on legitimately long calls.**
  The 90 s default is conservative against the observed median; long-
  rationale generations on hard pairs can legitimately take 60-80 s.
  A false abort marks a pair `uncertain` that should have been
  decided, increasing manual-review load. Mitigation: track the
  budget-hit rate; if it exceeds (say) 5 % of decisions in a batch,
  loosen the budget for the next run.
- **Budget too loose → still catches almost nothing.** 5-minute
  budgets effectively never fire. Mitigation: the budget should be
  on the order of 3× the observed median, not the observed p99.
- **Provenance schema sprawl.** If we add `watchdog-aborted` as a
  new stage value, anyone reading the provenance graph needs to
  know it exists. Mitigation: update the spec § 8 stage enum table
  in the same commit as the watchdog code.
- **Cache poisoning** (Option B specifically). If a SIGUSR1 arrives
  *after* the LLM response was received but *before* the cache
  write, the decision is lost and the pair gets re-judged on
  resume — wasteful, not wrong. Mitigation: the signal handler
  flushes the cache before raising.

## Open questions

- Should the watchdog budget apply per-call (primary call, fallback
  call) or per-pair (whole cascade)? Per-call is simpler and is
  what Option A naturally gives us. Per-pair would need a wall-clock
  start at the top of `cascade_judge` and a budget check at each
  step, which is more invasive but matches operator intuition ("I
  said abort if this pair takes more than 5 minutes").
- Does the watchdog also kick in for M9 reconciliation (which also
  fires LangChain calls)? Yes if we extend the timeout setting to
  the `Settings` level (one knob applied everywhere); no if we
  scope this to M6 only. Recommendation: ship Option A for M6
  first, extend to M9 in a follow-up once the budget calibration is
  understood.
- Should aborted pairs be retried automatically on a subsequent
  pipeline run with a fresh `--abort-budget-seconds` value, or
  should the cataloguer review them manually before the next run?
  The provenance entry makes manual review trivial; auto-retry
  risks re-tripping the same pathological pair. Recommend manual.
- Interaction with P-02 (vllm-mlx prefix cache): a hung call wastes
  the prefix-cache slot for the duration of the hang. A watchdog
  abort frees the slot for the next call. P-02 makes the watchdog
  *more* valuable, not less.
