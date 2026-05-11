# P-03 — M6 stall watchdog

**Status**: in-progress.
**Source proposal**:
[`docs/proposals/prop-03-m6-stall-watchdog.md`](../../proposals/prop-03-m6-stall-watchdog.md)
(drafted in commit `da5e3c9`; recovery-evidence amendment in `538c99e`).
**Plan-base commit**: `538c99e`. To gauge drift before executing,
run `git diff 538c99e..HEAD -- src/bffi_pipeline/config.py
src/bffi_pipeline/stages/judge.py src/bffi_pipeline/cli.py
scripts/run-full-pipeline.sh`.
**Phase commits**:

- Phase A (in-process timeout + retry + structured logging): `f3367b1` (code, tests, docs; the overnight-grade dry-run measurement is the remaining operator step before Phase A is fully done).
- Phase B (out-of-process heartbeat watchdog, contingent): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: ~1-1.5 days for Phase A (the user-visible
unblocker for unattended overnight runs); Phase B is contingent and
costs another ~1-2 days only if Phase A turns out to leave the
slow-but-not-dead pathology uncovered.

## Goal

Make unattended overnight M6 runs safe to start: a single stuck pair
must not silently consume hours, and the operator must be able to see
that the watchdog fired without sifting through prose log spam.

Three concrete capabilities:

1. **A configurable maximum time** the LLM is allowed for any single
   call, exposed both as an env var and a CLI flag, defaulting to a
   safe value calibrated against the observed steady-state per-decision
   latency.
2. **Auto-recovery via kill-and-retry**, per the recovery-evidence
   finding in the source proposal: timed-out calls retry on the same
   model first (transient Ollama wedges resolve this way), then
   escalate to the fallback model, and only finally land as
   `uncertain`. Persistently pathological pairs still surface for
   manual review; one-time stalls become invisible to the operator.
3. **Structured log events** the operator can tail with the same
   pipeline-monitoring tooling they already use (Monitor / Bash
   `grep`), so the watchdog firing is observable in real time and
   auditable after the fact.

## Definition of done

- A new `LLM_CALL_TIMEOUT_SECONDS` env-var (and `--abort-budget-seconds`
  CLI flag) sets the per-call wall-time ceiling. Default chosen against
  the observed steady-state per-decision latency (~28 s on `qwen3:8b`
  on this dev machine) — so `LLM_CALL_TIMEOUT_SECONDS=90` (~3× median)
  is the committed default.
- A timed-out LLM call triggers the existing 5/30/120-second
  exponential-backoff retry path on the same model. After retries
  exhaust, the cascade escalates to the fallback model. If that also
  times out repeatedly, the pair is marked `decision="uncertain"` with
  rationale `"M6 watchdog aborted after N s — manual review required"`.
- Every watchdog intervention emits a structured JSON line to **two**
  destinations:
  - stderr (so the existing pipeline-log tail + grep catches it),
    prefixed with `WATCHDOG_EVENT ` so the existing Monitor filter
    matches it.
  - A sidecar `<BFFI_DATA_DIR>/watchdog-events.jsonl` for post-run
    audit, one JSON object per line.
- Provenance records every watchdog-handled pair with
  `bffi-prov:stage = "watchdog-aborted"` (and the same string in the
  cascade-step `model_name` for the retry chain). The spec § 8 stage
  enum gains this value in the same commit as the code change.
- An overnight-grade dry run succeeds: pipeline against a 5,000-pair
  slice with `LLM_CALL_TIMEOUT_SECONDS=20` (aggressive on purpose so
  the watchdog fires often) completes without operator intervention
  and produces a non-zero `watchdog-events.jsonl`.

## Current state

- All four LLM call sites in `src/bffi_pipeline/` use
  `langchain_openai.ChatOpenAI` against `LLM_BASE_URL` without an
  explicit timeout — httpx defaults to a connect timeout but no
  read timeout, which is exactly the situation that allowed the
  preview-373 stall.
- `cascade_judge` in `stages/judge.py` already has a
  5/30/120-second exponential-backoff retry for transient connection
  errors. The watchdog plan reuses this retry stack rather than
  inventing a parallel one.
- `Settings` in `src/bffi_pipeline/config.py` already loads env vars
  via Pydantic Settings; adding `llm_call_timeout_seconds: int = 90`
  is one entry.
- The pipeline log under `<BFFI_DATA_DIR>/pipeline.log` is the
  surface every existing operator-facing tool tails. The Monitor
  filter pattern is `^(STAGE_|PIPELINE_)` — the watchdog needs to
  emit on a parallel pattern.
- No `bffi-prov:stage = "watchdog-aborted"` value exists yet.

---

## Phase A — In-process timeout + retry + structured logging

Estimated wall-time: ~1-1.5 days, dominated by tests + the
dry-run validation. The code change is small.

### A1. Add `llm_call_timeout_seconds` to Settings

In `src/bffi_pipeline/config.py`:

```python
llm_call_timeout_seconds: int = Field(
    default=90,
    alias="LLM_CALL_TIMEOUT_SECONDS",
    description=(
        "Per-call wall-time ceiling for the LLM. Calibrated to ~3× "
        "the observed steady-state per-decision latency on the dev "
        "machine (qwen3:8b ~= 28 s/decision). Override per run via "
        "the same env var or `--abort-budget-seconds` on the CLI."
    ),
)
```

Update `.env.example` with the same entry + a short comment.

### A2. Thread through `_build_chain`

In `src/bffi_pipeline/stages/judge.py` (and the matching M9 picker
chain in `stages/reconcile.py`):

```python
llm = ChatOpenAI(
    base_url=settings.llm_base_url,
    api_key=settings.llm_api_key,
    model=model_name,
    timeout=settings.llm_call_timeout_seconds,  # NEW
    temperature=0.0,
    seed=42,
)
```

LangChain forwards the `timeout` to the underlying `httpx` client,
which raises `httpx.ReadTimeout` (a subclass of `httpx.TimeoutException`)
when the server doesn't produce a complete response within the
budget.

### A3. Catch the timeout in the cascade

The existing exception block in `cascade_judge` catches transient
connection errors and retries via 5/30/120-second exponential
backoff. `httpx.ReadTimeout` should join that path explicitly:

```python
TRANSIENT_LLM_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    # ...whatever the existing tuple already names
)
```

The retry budget per pair stays as today (3 attempts via the existing
5/30/120-second schedule). After exhausting retries on the primary,
the cascade escalates to the fallback model as it already would for
a low-confidence primary verdict. After exhausting retries on the
fallback too, the pair lands as
`decision="uncertain"`, `stage="watchdog-aborted"`, with the
synthetic rationale specified in the definition of done.

### A4. Structured event emission

Add `src/bffi_pipeline/stages/watchdog.py` (or fold into `judge.py` if
small enough — TBD at code review time) with one function:

```python
def emit_watchdog_event(
    *,
    pair_id: str,
    event: Literal["timeout", "retry", "escalate", "give_up"],
    model_name: str,
    elapsed_seconds: float,
    retry_n: int,
    sidecar_path: Path,
) -> None:
    """Write one structured event to stderr (prefixed) + the sidecar JSONL."""
```

Event shape (one JSON object per line, both destinations):

```json
{"ts": "2026-05-11T17:32:22Z", "pair_id": "abc...123",
 "event": "timeout", "model": "qwen3:8b-q4_K_M",
 "elapsed_s": 90.05, "retry_n": 1}
```

Stderr line carries an unambiguous prefix:

```
WATCHDOG_EVENT {"ts": "...", "event": "timeout", ...}
```

`run-full-pipeline.sh`'s Monitor pattern (`^(STAGE_|PIPELINE_)`)
broadens to `^(STAGE_|PIPELINE_|WATCHDOG_EVENT)` so existing
monitor invocations pick the events up without further work.

### A5. CLI flag

`bffi-pipeline judge --abort-budget-seconds N` overrides the
Settings default for the run. The flag is plumbed through to the
same `Settings` instance the rest of the stage uses, not parsed
separately. Doc the flag in the `judge` CLI's `--help`.

### A6. Provenance

Extend the `bffi-prov:stage` enum in
[`docs/marcxml-to-bffi-skosmos-pipeline.md`](../../marcxml-to-bffi-skosmos-pipeline.md)
§ 8 with the `"watchdog-aborted"` value. Add a constant to
`stages/judge.py` matching the existing `STAGE_AUTO_MERGE` /
`STAGE_PRIMARY` / `STAGE_FALLBACK` constants:

```python
STAGE_WATCHDOG: Final[str] = "watchdog-aborted"
```

`ProvenanceWriter` already handles arbitrary stage values; no schema
change beyond the documentation.

### A7. Tests

`tests/unit/test_judge.py` gains:

- A monkeypatch test where the chain raises `httpx.ReadTimeout` once
  → cascade retries → second call succeeds. Assert provenance carries
  one `watchdog-aborted` retry record but the final decision is the
  successful verdict.
- A monkeypatch test where every call raises `httpx.ReadTimeout` →
  cascade escalates to fallback, fallback also times out, final
  decision = `uncertain` with the synthetic rationale.
- A sidecar test: assert `watchdog-events.jsonl` exists after the
  cascade fires, one line per event, valid JSON.
- The new spec § 8 stage value is asserted by an existing-or-new
  enum-completeness test in `tests/unit/test_provenance.py`.

### A8. Overnight-grade dry run

Run the pipeline against a 5,000-pair slice with the **default**
`LLM_CALL_TIMEOUT_SECONDS=90`. The purpose of the dry run is **to
measure how often the watchdog fires at the committed default**, not
to stress-test the retry path. Two outcomes interpret meaningfully:

- **Watchdog fires zero or near-zero times.** Either the corpus
  has no pathological pairs (best case) or the default is too loose
  to catch the real ones. Compare against the preview-373 incident's
  observed wedge: that one would have hit the 90 s ceiling. If the
  dry run sees zero events, that's a positive signal *only if* the
  pipeline also visibly completed without long quiet stretches.
- **Watchdog fires N times.** The events file tells us exactly which
  pairs caused trouble, how often a retry was enough to recover,
  and whether any pairs landed as `uncertain` after exhausting
  retries. Counts inform whether to tighten or loosen the default
  for the production run.

Either way the artefacts we look at:

- `watchdog-events.jsonl` event count + distribution (`timeout` vs
  `retry` vs `escalate` vs `give_up`).
- M6 wall-time vs the no-watchdog baseline (should be within a few
  percent on the default unless the watchdog fires often).
- Final decision distribution — count of `uncertain` pairs that
  carry the `watchdog-aborted` provenance stage.

### A9. Phase A acceptance

- [ ] `LLM_CALL_TIMEOUT_SECONDS` Settings field exists and is wired
      through `_build_chain` in both M6 and M9.
- [ ] `--abort-budget-seconds` CLI flag overrides the Settings
      default.
- [ ] `cascade_judge` retries on `httpx.ReadTimeout` and emits the
      `watchdog-aborted` stage on every retry / escalate / give-up
      event.
- [ ] Stderr `WATCHDOG_EVENT ` lines are emitted; the existing
      Monitor / pipeline.log tail picks them up.
- [ ] `<BFFI_DATA_DIR>/watchdog-events.jsonl` sidecar gets one
      well-formed JSON line per event.
- [ ] `bffi-prov:stage = "watchdog-aborted"` documented in spec § 8.
- [ ] All new + existing tests in `tests/unit/test_judge.py` and
      `tests/unit/test_provenance.py` pass; `make lint` + `mypy
      --strict` green.
- [ ] Overnight-grade dry run on a 5,000-pair slice with aggressive
      timeout completes without operator intervention and produces a
      non-empty `watchdog-events.jsonl`.

### A10. Rollback

Revert the judge.py + config.py + CLI changes. The Settings field
defaults to the existing no-timeout behavior if removed from
`.env`. The sidecar file is additive — leaving it on disk is
harmless after rollback.

---

## Phase B — Out-of-process heartbeat watchdog (contingent)

**Only start Phase B if Phase A's dry-run reveals a "slow but not
dead" pathology** — i.e., calls that produce one token every 20
seconds, never hit the per-call budget but are still effectively
useless. Phase A's `httpx.ReadTimeout` cannot catch that case
because the connection is making progress (just very slowly).

Sketch (full design deferred until Phase A's bench tells us whether
this is actually a real failure mode):

- A companion CLI `bffi-pipeline judge-watchdog --decisions-path
  <path> --max-stall-seconds N --target-pid <pid>` tails
  `judge-decisions.jsonl`, sends `SIGUSR1` to the target judge
  process when no new line has been written for `N` seconds.
- The judge installs a `SIGUSR1` handler that cancels the current
  httpx call, emits a `WATCHDOG_EVENT` of `event="heartbeat-stall"`,
  and re-enters the cascade retry path Phase A built.
- `scripts/run-full-pipeline.sh` starts the watchdog as a sibling
  background process and traps on exit to kill it cleanly.

This is documented as the future shape; do not pre-implement.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Default timeout (90 s) is too tight for genuinely long-rationale pairs | Medium | The aggressive-timeout dry run (A8) measures the false-abort rate. If it exceeds 5 % on the slice, raise the default. The retry stack absorbs occasional false aborts cleanly — they don't become wrong verdicts, just extra latency. |
| `httpx.ReadTimeout` not propagated cleanly through `langchain-openai` | Low-medium | A 5-line smoke test before A2 confirms the exception surfaces. If it doesn't, fall back to wrapping `ChatOpenAI.invoke` in `asyncio.wait_for` or equivalent. |
| Cascade retry stack misclassifies a real same-pair retry as a transient error | Low | The existing retry stack is well-tested; we're only adding `ReadTimeout` to its trigger tuple, not changing its semantics. |
| Provenance `watchdog-aborted` stage breaks downstream queries that enumerate stages | Low-medium | Spec § 8 update lands in the same commit as the code. CI's provenance-completeness test (A7) catches missing enum entries. |
| Slow-but-not-dead pathology turns out to be real | Medium | Phase B exists for this. Phase A's logging will surface the pattern if it shows up — pairs that don't time out but take forever will appear in `judge-decisions.jsonl` with abnormally high `latency_seconds`. |
| Sidecar file grows unbounded on a multi-night production run | Low | JSONL is small per event; even 100 k events × ~150 bytes = ~15 MB. Acceptable. If it becomes a problem, log-rotate. |

## Open issues to close before / during execution

- **Watchdog timeout default for M9 (reconciliation)** — the M9
  picker also uses LLM calls but with a different latency profile
  (typically shorter prompts → faster decisions). Should it share
  `LLM_CALL_TIMEOUT_SECONDS`, or have its own
  `LLM_PICKER_TIMEOUT_SECONDS`? Recommend shared default for now;
  split if observed M9 latency p99 is materially different from M6.
- **Should the structured event sidecar use a Pydantic-validated
  shape, or just `json.dumps`?** Pydantic is consistent with the rest
  of the project's data shapes; `json.dumps` is one less import in a
  module called from the hot loop. Recommend Pydantic — the event
  rate is far below the rate at which Pydantic validation overhead
  matters.
- **CLI flag default visibility** — `--abort-budget-seconds` not
  appearing in `--help` would surprise operators looking for an
  override. Confirm typer surfaces it; if not, raise it to top-level.
- **Interaction with --concurrency (vllm-mlx future)** — once
  P-02 ships vllm-mlx, `--concurrency > 1` means multiple in-flight
  LLM calls. Each needs its own timeout clock. The Settings value
  applies per-call (not per-batch), so this should work, but verify
  with an explicit test once vllm-mlx is in the picture.

## Cross-references

- [`docs/proposals/prop-03-m6-stall-watchdog.md`](../../proposals/prop-03-m6-stall-watchdog.md)
  — source proposal, including the empirical evidence section that
  motivates the retry-on-same-model design choice.
- [`docs/marcxml-to-bffi-skosmos-pipeline.md`](../../marcxml-to-bffi-skosmos-pipeline.md)
  § 8 — provenance stage enum; Phase A6 extends it with the
  `"watchdog-aborted"` value.
- [`docs/plans/backlog/p-02-inference-stack-tuning.md`](p-02-inference-stack-tuning.md)
  — the vllm-mlx migration that interacts with this plan (per-call
  timeouts must continue to work under continuous batching).
- [`scripts/run-full-pipeline.sh`](../../../scripts/run-full-pipeline.sh)
  — the orchestrator whose Monitor filter pattern broadens to include
  `WATCHDOG_EVENT` in Phase A4.
