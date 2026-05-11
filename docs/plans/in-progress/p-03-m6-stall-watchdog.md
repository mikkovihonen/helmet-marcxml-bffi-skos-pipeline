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

- Phase A (in-process per-call timeout + retry + structured logging): `f3367b1` (code, tests, docs; overnight-grade dry-run measurement is the remaining operator step).
- Phase B (in-process per-pair budget): `051e834` (code, tests, plan revision).

**Owner**: TBD.
**Estimated wall-time**: ~1-1.5 days for Phase A; ~0.5 day for
Phase B (promoted from contingent after the preview-373 evidence).
Both phases are shipped; the remaining operator task is the dry-run
calibration of the defaults against a 5,000-pair slice.

## Goal

Make unattended overnight M6 runs safe to start: neither a single
stuck call nor a slow pile-up of legitimate calls on one pair must
silently consume hours, and the operator must be able to see
that the watchdog fired without sifting through prose log spam.

Four concrete capabilities, partitioned across **two ceilings** —
per-call (Phase A) and per-pair (Phase B):

1. **A configurable per-call ceiling** (`LLM_CALL_TIMEOUT_SECONDS`,
   default 90 s) for any single LLM round-trip. Catches the
   "one call wedged forever" pathology — the preview-373 incident's
   signature. Exposed via env var and `--abort-budget-seconds` flag.
2. **A configurable per-pair ceiling**
   (`LLM_PAIR_TIMEOUT_SECONDS`, default 300 s) for the cumulative
   wall-time of a single pair's cascade. Catches the orthogonal
   "many legitimate-but-slow calls pile up on one pair" pathology
   — observed empirically on preview-373 at ~2.75 min/pair without
   any single call exceeding the per-call ceiling, which means
   Phase A's per-call ceiling missed it entirely. Exposed via env
   var and `--pair-budget-seconds` flag.
3. **Auto-recovery via kill-and-retry** for per-call timeouts, per
   the recovery-evidence finding in the source proposal: timed-out
   calls retry on the same model first (transient Ollama wedges
   resolve this way), then escalate to the fallback model, and only
   finally land as `uncertain`. Per-pair timeouts abandon the
   cascade for that pair and emit a `pair_budget_exceeded` event;
   no further retry because the pair has already had ample
   opportunity.
4. **Structured log events** the operator can tail with the same
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
- An overnight-grade dry run succeeds against **the production
  backend in play at the time** (currently Ollama; v2's M6 pass
  runs against Ollama). The run uses the committed defaults
  (`LLM_CALL_TIMEOUT_SECONDS=90`, `LLM_PAIR_TIMEOUT_SECONDS=300`)
  against a 5,000-pair slice; goal is **measurement**, not stress
  — count how often each event type fires at the defaults and
  decide whether to tighten / loosen for the production pass.

  P-03's done-state is *backend-scoped* by design. When P-02
  Phase A ships and mlx-lm becomes the production backend, the
  watchdog budgets get **re-pinned** as part of P-02's
  "Concurrency setting" sweep — not by re-running this P-03 dry-
  run. The watchdog code itself is backend-agnostic, so no P-03
  code changes are involved; only the defaults move. See
  "Review questions Q3" below for the rationale (Option A
  trade-off — pin the Ollama-backend defaults now for v2's M6,
  let P-02 own the mlx-lm-backend re-pin once that backend
  ships).

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

Extend the `bffi-prov:stage` enum referenced in `CLAUDE.md` (and
preserved in the archived spec at
[`docs/archived/marcxml-to-bffi-skosmos-pipeline.md`](../../archived/marcxml-to-bffi-skosmos-pipeline.md)
§ 8) with the `"watchdog-aborted"` value. Add a constant to
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

## Phase B — Per-pair wall-time budget (in-process)

Estimated wall-time: ~0.5 day. **Promoted from "contingent" to a
tracked sub-phase** after the preview-373 resume surfaced the exact
pathology the original Phase B contemplated: pairs taking ~2.75 min
of wall time without any single LLM call exceeding the 90 s per-call
ceiling — Phase A's per-call watchdog is blind to this case by
design.

Empirical signature: the cascade fires multiple sequential LLM
calls per pair (primary + fallback + up-to-2 validation retries +
exponential-backoff connection retries). Each call individually
completes under the 90 s ceiling, so Phase A emits no events.
Cumulatively the pair burns 3-5 calls × ~30-50 s = 2-4 min,
producing the observed slow patches.

Design: an in-process per-pair budget, checked at natural
breakpoints inside `cascade_judge` and `judge_pair` (between
cascade tiers, before each `chain.invoke()`). Much simpler than the
out-of-process SIGUSR1 design the source proposal sketched —
single process, no signal handlers, no sibling supervisor — and
sufficient for our observed failure mode because the slow pairs
ARE making progress at the call level; we just need to give up when
cumulative wall time exceeds the budget.

### B1. Add `llm_pair_timeout_seconds` to Settings

In `src/bffi_pipeline/config.py`:

```python
llm_pair_timeout_seconds: int = Field(
    default=300,
    alias="LLM_PAIR_TIMEOUT_SECONDS",
    description=(
        "Per-pair wall-time ceiling for the whole M6 cascade "
        "(primary + fallback + retries). 300 s is ~3-5× a typical "
        "all-tier pass; raise for hard-rationale-heavy corpora, "
        "lower for tighter overnight bounds."
    ),
)
```

`.env.example` gets a matching entry alongside `LLM_CALL_TIMEOUT_SECONDS`.

### B2. CLI flag

`bffi-pipeline judge --pair-budget-seconds N` overrides the env for
the run, mirroring `--abort-budget-seconds` from Phase A. Same
plumbing: mutate the cached Settings singleton.

### B3. Pair deadline plumbing

`cascade_judge` records `pair_started_at = time.monotonic()` at
entry, computes a deadline, and passes `pair_deadline: float | None
= None` to each `judge_pair` invocation. `judge_pair`'s retry loop
checks `if pair_deadline and time.monotonic() > pair_deadline:
break` at the top of each iteration (before invoking the chain).

When the budget fires:

- Emit a `pair_budget_exceeded` watchdog event (extending the
  Phase A vocabulary).
- Mark the pair `decision="uncertain"`, `stage="watchdog-aborted"`
  (same provenance stage as Phase A — operationally the operator
  treats both the same: manual review).
- Rationale text: `"M6 pair budget exceeded after N s — manual
  review required"`.

No re-entry into the cascade after a `pair_budget_exceeded` event.
The pair has had its budget; further attempts would just be more
sunk cost.

### B4. Event-vocabulary extension

`stages/watchdog.py`'s `WatchdogEvent` Literal grows
`"pair_budget_exceeded"`. The event payload shape stays
identical; semantically the `event` field distinguishes which
ceiling fired.

### B5. Tests

`tests/unit/test_judge.py`:

- Synthetic chain that simulates a sequence of calls each taking
  ~30 s (via injected sleep callable + `time.monotonic` monkeypatch)
  with the budget at 100 s. Assert: cascade aborts after the 3rd or
  4th call, emits a `pair_budget_exceeded` event, returns
  `uncertain`.
- Negative case: budget is generous (1 hour); a multi-call pair
  completes normally with no `pair_budget_exceeded` event.

### B6. Dry-run on the preview-373 corpus

Re-run the preview-373 slice with `LLM_PAIR_TIMEOUT_SECONDS=180`
(3 min; tighter than the observed average). Expected:

- Most pairs complete normally.
- ~5-15 % of pairs (the slow patches we observed) hit the budget
  and land as `pair_budget_exceeded` `uncertain` decisions.
- Total M6 wall time drops from the previous "5+ hours due to
  slow tail" to a bounded value.

This is the actual unblocker for unattended overnight runs: no
single pair can drag the batch out longer than the budget allows.

### B7. Phase B acceptance

- [ ] `LLM_PAIR_TIMEOUT_SECONDS` Settings field + `.env.example`
      entry.
- [ ] `--pair-budget-seconds` CLI flag.
- [ ] `pair_deadline` plumbed through `cascade_judge` → `judge_pair`.
- [ ] `pair_budget_exceeded` event emits to stderr + sidecar.
- [ ] Provenance carries `bffi-prov:stage = "watchdog-aborted"`
      for pair-budget-aborted pairs.
- [ ] Tests in `test_judge.py` cover the budget-exceeded and
      budget-not-exceeded cases.
- [ ] preview-373 dry-run shows the per-pair budget firing on the
      observed slow patches without spuriously aborting healthy
      pairs.

### B8. Rollback

Revert the judge.py + config.py + CLI changes. The Settings field
defaulting to no-timeout (`None`) preserves the existing behavior
if removed from `.env`.

---

## End-to-end smoke verification (Phase A + B, 2026-05-11)

Post-implementation smoke against real Ollama + real cascade, run
with deliberately aggressive timeouts (`--abort-budget-seconds 1`
+ `--pair-budget-seconds 5`) to force the watchdog paths to fire
on every escalate-band pair. **Not the calibration dry-run** the
plan defines as Phase A8 / B6 — that one uses default budgets
against a 5000-pair slice and is still pending operator action.
The aggressive smoke verifies the *plumbing*; the proper dry-run
verifies the *defaults*.

**Setup**: 10 candidates from `preview-373/embed-candidates.jsonl`
fed through `bffi-pipeline judge` with the aggressive flags.

**Outcome**:

| Check | Result |
|---|---|
| Per-pair budget fires on escalate-band pairs | ✓ 6 ``pair_budget_exceeded`` events across 3 unique pairs |
| Events stream to both stderr (``WATCHDOG_EVENT `` prefix) and ``watchdog-events.jsonl`` | ✓ 6 lines on each surface; payloads identical |
| Cascade escalates primary → fallback after primary aborts | ✓ Each affected pair has events from both ``qwen3:8b-q4_K_M`` and ``qwen3:32b-q4_K_M`` |
| Fallback also enforces the shared per-pair deadline | ✓ Fallback events fire with ``elapsed_s=0.0`` because the shared deadline is already in the past at entry |
| Final decisions land as ``uncertain`` with the watchdog rationale | ✓ 3/3 escalate-band pairs landed as ``uncertain`` with ``"pair budget exceeded — cumulative cascade wall time passed LLM_PAIR_TIMEOUT_SECONDS"`` |
| Auto-merge-band pairs still short-circuit normally | ✓ The 1 auto-merge candidate got a synthetic ``same_work`` instantly without an LLM call |
| Reject-band pairs filtered out before M6 (no spurious events) | ✓ 6 reject-band candidates from the input never reached the judge |
| ``judge_batch`` summary counts events accurately | ✓ "completed: 3 / auto-merged: 1 / cascade used: 3 / decision counts: same_work=1 uncertain=3" |
| Process exits cleanly, no crash | ✓ |

**Empirical calibration findings** (relevant for the real dry-run):

- **Path A** — budget exhausted at cascade entry (the
  ``elapsed_s=0.0, retry_n=0`` events). Happens when an earlier
  call in the cascade already burned through the budget; the
  next-tier ``judge_pair`` checks the deadline before invoking and
  aborts immediately. Clean.
- **Path B** — budget exhausted mid-retry (the
  ``elapsed_s=6.331, retry_n=1`` event). The pair's primary fired
  one ``chain.invoke()`` (which timed out at the 1 s per-call
  ceiling), the existing connection-retry stack waited 5 s before
  retry, the deadline check fired before the second invoke.
  **Insight**: the 5/30/120 s connection-retry backoff is what
  burns the per-pair budget in practice. At default budgets (90 s
  call / 300 s pair) the cascade has room for ~3 tier-call
  sequences before the per-pair budget kicks in — exactly the
  intended behaviour.

**Conclusion**: Phase A + Phase B's implementation is verified
end-to-end against real backend + real cascade. The remaining
acceptance step is the operator's overnight-grade dry-run
measurement at the committed defaults (90 s / 300 s) against a
5000-pair slice from the v2 corpus once M6 starts.

**Per Q3 (Option A)**: that dry-run runs against Ollama since v2's
M6 pass is Ollama-backed. P-02 Phase A's later mlx-lm re-pin
happens inside P-02's A6 sweep; it doesn't reopen P-03. The
calibration is backend-scoped by design.

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
- **Interaction with --concurrency (mlx-lm future)** — once
  P-02 ships mlx-lm, `--concurrency > 1` means multiple in-flight
  LLM calls. Each needs its own timeout clock. The Settings value
  applies per-call (not per-batch), so this should work, but verify
  with an explicit test once mlx-lm is in the picture.

## Review questions

Surfaced during review of the Phase A + B implementation. Answers
folded back into the plan so a future reader / re-implementer
doesn't re-discover them.

### Q1. How does the implementation handle concurrency?

Three layers, each handled differently:

1. **Threads within one judge process** (the only model M6 actually
   uses today, via `ThreadPoolExecutor` at `--concurrency > 1`).
   Every piece of P-03 state is either:
   - Thread-local (`pair_deadline` is a `float` passed by-value
     through the `cascade_judge → judge_pair` call chain — each
     worker carries its own copy on its stack);
   - Atomic-int (`Settings.llm_call_timeout_seconds` and
     `llm_pair_timeout_seconds` are simple `int`s, atomically loaded
     in CPython; the CLI override mutates once at startup before
     threads spawn);
   - Reliant on POSIX `O_APPEND` atomicity (the
     `watchdog-events.jsonl` sidecar opens-writes-closes per event;
     payloads are ~200 bytes, well under `PIPE_BUF` ~4 KB, so
     concurrent writes never interleave at the byte level).
   `time.monotonic()` is per-process but consistent across threads,
   so deadlines set in one thread are comparable in another. No new
   locks needed beyond what was already in M6 (SQLite cache lock).
2. **Multiple judge processes running side-by-side** (operator
   kicks off two `bffi-pipeline judge` invocations against disjoint
   slices). Each process owns its own `Settings`, thread pool,
   httpx client, deadline computations. The only shared resource is
   the sidecar JSONL — `O_APPEND` semantics apply across processes
   the same way they apply across threads, so writes don't
   interleave. If both processes target the same `BFFI_DATA_DIR`,
   events from both end up in one file; if different, each gets
   its own.
3. **Future `ProcessPoolExecutor` for M6** (not currently in use).
   `pair_deadline` is a plain `float` and pickles trivially — no
   marshalling work needed to ship the deadline to a worker
   process. Settings is re-instantiated per worker; the override
   plumbing happens once in the parent, so workers see the right
   value via env-var inheritance.

### Q2. Are there differences in concurrency handling between Ollama and mlx-lm?

Yes — at the budget-*calibration* level, not at the watchdog-code
level. The watchdog code is backend-agnostic (both speak OpenAI-
compatible HTTP, both surface `httpx.ReadTimeout` the same way).
The budgets behave differently because the backends queue requests
differently:

- **Ollama** serializes at the server. `OLLAMA_NUM_PARALLEL` caps
  how many requests one loaded model handles in parallel (default
  4 on recent releases; was 1 on older ones). When the pipeline's
  `--concurrency` exceeds that, requests queue server-side. The
  `httpx` per-call timeout includes the queue wait, so at high
  concurrency the per-call watchdog can fire on benign server-side
  queueing rather than on a real wedge. Current M6 runs at
  `--concurrency=1` on Ollama precisely to avoid this.
- **mlx-lm** continuous-batches: N concurrent requests share GPU
  time at the token level with effectively no head-of-line waiting
  (until the scheduler starts swapping batches at memory pressure).
  Per-call wall time reflects real generation time; the budgets
  apply cleanly without inflation.

Calibration guidance:

| Scenario | Budget setting |
|---|---|
| Ollama, `--concurrency=1` (current default) | 90 s / 300 s (defaults). Budgets reflect real wall time. |
| Ollama, `--concurrency > 1` | Raise proportionally — `--concurrency=4` ≈ 180 s / 600 s — otherwise expect false-positive timeouts from queueing rather than wedging. |
| mlx-lm, any concurrency (post-P-02 Phase A) | Re-pin via the Phase A8 / B6 dry-run on the new backend. Expect tighter, not looser, defaults given prefix-caching + speculative-decoding wins. |

This question intersects with P-02's "Concurrency setting" open
issue — budget calibration and concurrency tuning should be done
together once mlx-lm is in operation; treat them as one
coordinated bench sweep, not two separate ones.

### Q3. Does shipping P-02 reopen P-03's done check?

No — but the *calibration* of P-03's defaults is backend-scoped.
The decision recorded here (Option A): pin the defaults against
**Ollama now** so v2's M6 pass has a calibrated watchdog, and
**let P-02 own the mlx-lm re-pin** once that backend ships.

The trade-off was between two pragmatic options:

| Option | Description | Trade-off |
|---|---|---|
| **A** (chosen) | Do the dry-run against Ollama now; mark P-03 done; re-pin defaults inside P-02 Phase A6's concurrency sweep when mlx-lm lands. | Calibrated watchdog *immediately*, relevant because v2's M6 pass starts before P-02 Phase A's operator setup completes. The Ollama-backend defaults become "stale" once P-02 ships, but P-02 already owns that re-pin — no double-work. |
| **B** (not chosen) | Defer P-03's dry-run until after P-02 Phase A ships; run once against mlx-lm; calibration is final. | Cleaner single-calibration story, but P-03 stays ``in-progress`` until P-02 Phase A ships (multi-day operator setup). v2's M6 in the interim would run with un-dry-run-validated defaults — implementation is verified by the smoke (see "End-to-end smoke verification" section above), but the *defaults* aren't pinned by a real overnight slice. |

What this means in practice:

- P-03 done = "Ollama-backend defaults pinned by overnight-grade
  dry-run on a 5,000-pair slice", regardless of what backend
  P-02 eventually serves.
- P-02 Phase A ships → its A6 / "Concurrency setting" open
  issue inherits the responsibility of re-pinning the budgets
  for the new backend. **The re-pin does NOT reopen P-03**;
  it's a P-02 deliverable that touches P-03's default values
  in-place.
- The watchdog code itself stays untouched across the
  transition. Only the two integer defaults in
  `src/bffi_pipeline/config.py` move.

If a later backend swap (say, P-04's full Ollama removal) happens
without going through P-02's bench, the operator runs the dry-
run again on whichever backend is current at the time. The
"backend-scoped calibration" framing keeps re-pins lightweight
without re-litigating P-03's design.

## Cross-references

- [`docs/proposals/prop-03-m6-stall-watchdog.md`](../../proposals/prop-03-m6-stall-watchdog.md)
  — source proposal, including the empirical evidence section that
  motivates the retry-on-same-model design choice.
- [`docs/archived/marcxml-to-bffi-skosmos-pipeline.md`](../../archived/marcxml-to-bffi-skosmos-pipeline.md)
  § 8 — provenance stage enum (archived spec); Phase A6 extends it with
  the `"watchdog-aborted"` value, and the live enum reference lives in
  `CLAUDE.md` § "Committed identifiers".
- [`docs/plans/in-progress/p-02-inference-stack-tuning.md`](p-02-inference-stack-tuning.md)
  — the mlx-lm migration that interacts with this plan (per-call
  timeouts must continue to work under continuous batching).
- [`scripts/run-full-pipeline.sh`](../../../scripts/run-full-pipeline.sh)
  — the orchestrator whose Monitor filter pattern broadens to include
  `WATCHDOG_EVENT` in Phase A4.
