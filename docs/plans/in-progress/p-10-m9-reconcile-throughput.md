# P-10 — M9 reconcile throughput: concurrency + persistent picker cache + tier-0 expansion

**Status**: backlog.
**Source proposal**: [`docs/proposals/prop-10-m9-reconcile-throughput.md`](../../proposals/prop-10-m9-reconcile-throughput.md)
at commit `9ba54d1` (proposal-base `ad4f6c4`, refined with Finto-cadence
evidence at `9ba54d1`).
**Plan-base commit**: `9ba54d1`. To gauge drift before executing,
run
`git diff 9ba54d1..HEAD --
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/stages/local_concept_resolver.py
src/bffi_pipeline/cli.py
prompts/picker_v1.txt
data/finto-dumps/`.
**Phase commits**:

- Phase A (M9 concurrency knob + watchdog wiring): `0f8c3da` (code, 2026-05-12). Bench snapshot at [`docs/performance/2026-05-12-5k-m2-max-phase-a.md`](../../performance/2026-05-12-5k-m2-max-phase-a.md): zero `field_budget_exceeded` events ✓, byte-identical bindings ✓, but **1.05× speedup vs ≥3× target** because the parallelisable tier-2 LLM picker is only ~30 % of M9 wall on this corpus; serial Phase 1 (tier-0 + candidate-query) dominates. See snapshot § "Implications for P-10 Phases B and C" for the re-scoping this introduces.
- Phase A2 (Phase 1 parallelisation — tier-0 + candidate query): `6c14c36` (code, 2026-05-12). Bench snapshot at [`docs/performance/2026-05-12-5k-m2-max-phase-a2.md`](../../performance/2026-05-12-5k-m2-max-phase-a2.md): zero `field_budget_exceeded` events ✓, semantically byte-identical bindings to Phase A ✓ (only `descriptionChangeDate` timestamps differ), wall **3 639 s (60:39)** vs Phase A's 5 460 s — cumulative **1.57× speedup** vs the 5 722 s baseline (Phase A2 alone gives 1.50× over Phase A). Phase 1 dropped ~1.9× (sublinear of the 8× nominal — server-side Finto/VIAF latency dominates throughput). **Still short of the ≥3× cumulative target on this corpus**; Phase B + Phase C projected to close the gap.
- Phase B (persistent picker cache): `8950741` (code, 2026-05-13). SQLite-backed `reconcile-cache.sqlite` mirrors M6's `JudgeCache` (commit `1452a4f`): single-threaded lookup-then-dispatch ordering, `BEGIN IMMEDIATE` writes, per-vocabulary `finto_sha` invalidation, `prov:wasInfluencedBy` provenance triple on cache hits. `--cache/--no-cache` CLI flag + `BFFI_M9_CACHE_DISABLED` env var rollback. VIAF picks deliberately not cached (no local dump to anchor). 2026-05-13 P-10 Phase B + E bench at [`docs/performance/2026-05-13-5k-m2-max-phase-b-e.md`](../../performance/2026-05-13-5k-m2-max-phase-b-e.md): cold→warm **2.88×** speedup; 65.8 % hit rate (below the ≥ 90 % target because the original filter only cached `STAGE_LLM` outcomes); cold↔warm output divergence on 7 works with confidence near the 0.80 threshold flipped tier classification → Phase B.1.
- Phase B.1 (cache every picker decision, not just STAGE_LLM): `<unfilled>`. Removes the `outcome.stage == STAGE_LLM` filter so low-confidence picks that map to `STAGE_FALLBACK` are cached verbatim too. Aligns with M6's `JudgeCache` contract (which caches `uncertain` decisions). Stops cold/warm tier flips from picker non-determinism near the 0.80 confidence threshold. Operator clears the cache via `make clean-caches` after model changes. Audit script (`scripts/p10-phase-b-cold-warm-audit.py`) is the diagnostic that surfaced the need. One new unit test pins the contract: warm-run cache hit on a STAGE_FALLBACK entry produces zero picker calls and the same chosen URI.
- Phase C (tier-0 normalisation + altLabel inclusion): `8e47a69` (code, 2026-05-12; feature-flagged off by default — see below). Resolver-side `BFFI_M9_TIER0_EXPANSION` defaults `False`; `load-finto --fold-pref-labels` materialisation defaults `False` as of the 2026-05-13 flip. Bench *attempt* at [`docs/performance/2026-05-13-5k-m2-max-phase-c-attempt.md`](../../performance/2026-05-13-5k-m2-max-phase-c-attempt.md) (mlx-lm GPU-OOM mid-run on the M2 Max; 1 500 picker calls completed at crash already past Phase A2's 1 348 baseline, suggesting Phase C does **not** reduce tier-2 work on the May 12 corpus while doubling Phase 1 SPARQL traffic). Code-side stays committed; production-readiness validation remains pending the cataloguer audit OR a clean re-bench on the M5 Max 128 GB with smaller mlx-lm `--prompt-cache-size`.
- Phase E (prompt ordering for mlx-lm prefix-cache stickiness): `c07d333` (code, 2026-05-13). Promoted from the deferred-levers list earlier the same day after the Phase C bench attempt confirmed picker-phase wall remains the largest single contributor (no Phase B yet, Phase C provides no picker savings on this corpus). Default ordering flipped to `prefix-cache`; `submission` retained behind `BFFI_M9_PICKER_ORDERING` for bench A/B + rollback. Output Turtle byte-stable under either mode (orchestrator re-sorts results by submission `idx` before graph mutation — Phase A's determinism gate). Bench acceptance gate ≥ 5 % picker-phase wall reduction vs Phase A2 remains pending the next 5 k re-run.

**Owner**: TBD.
**Estimated wall-time**: ~5-6 days end-to-end. Phase A ~1.5 days (concurrency + watchdog wiring + bench) **— shipped at `0f8c3da`**. Phase A2 ~1 day (Phase 1 parallelisation — added after Phase A's bench surfaced that serial Phase 1 work, not tier-2 LLM, is the dominant cost on this corpus). Phase B ~1-1.5 days. Phase C ~1-2 days (rule design + cataloguer-sample audit gate). Phase E ~0.25 day (sort key + env var + byte-stability test; small surface area). Each phase is independently shippable and lands with its own [`docs/performance/`](../../performance/) snapshot.

## Goal

Bring M9 reconcile wall-time on the full 800 k Helmet corpus from a linear-extrapolated ~10 days to **under one overnight window** (~8-10 h), without regressing any of the bind-quality measurements documented in the 2026-05-12 5k snapshot.

Concrete targets, measured on a 5 k re-run against the same sample (`data/sample-5k-marcxml/`, Python `random.seed(42)`, identical Fuseki state):

| Stage | 5k baseline (2026-05-12) | After Phase A (measured) | Target after Phase A2 | Target after Phase B (warm cache) | Target after Phase C |
|---|---|---|---|---|---|
| M9 wall | **5 722 s (95:22)** | 5 460 s (1.05×) | ≤ 1 900 s (≥ 3× cum.) | ≤ 100 s (≥ 90 % cache hit rate) | ≤ 70 s |
| Tier-0 hit count | (baseline-X) | unchanged | unchanged | unchanged | +30 % vs baseline-X |
| Tier-2 (LLM) call count | (baseline-Y) | unchanged | unchanged | unchanged | ≤ 0.7 × baseline-Y |
| Bind-quality on 200 spot-checked decisions | reference | identical | identical | identical | identical (audit gate) |

The Phase A column shows the **measured** result, not a target — see [`docs/performance/2026-05-12-5k-m2-max-phase-a.md`](../../performance/2026-05-12-5k-m2-max-phase-a.md). The Phase A2 target is the **originally-planned Phase A** target (≥3× vs baseline), now expected after both concurrency levers ship — Phase A parallelised tier-2 (~30 % of wall), Phase A2 parallelises Phase 1 (tier-0 + candidate query, ~70 % of wall). The Phase B "warm cache" target is a second consecutive run on the same corpus with no Finto refresh between runs — the realistic operator pattern on the production M5 Max box.

## Definition of done

- All five phases (A, A2, B, C, E) have filled-in phase commits, each on its own commit (no batching) so a partial revert is mechanical.
- The fresh [`docs/performance/`](../../performance/) snapshot taken after the final phase shows M9 ≤ 70 s on the 5k sample with the cache warm, and the extrapolation table in that snapshot projects ≤ 10 h for the full 800 k corpus.
- Phase E's snapshot demonstrates ≥ 5 % picker-phase wall reduction vs Phase A2 at byte-identical output.
- The Phase A2 snapshot demonstrates that the **original Phase A speedup target (≥ 3×)** is achievable once both concurrency levers ship — closing the gap surfaced by the Phase A bench.
- The 200-sample audit from Phase C is committed under `gold/reconcile-audit-200.jsonl` (feeds the P-06 backlog).
- `docs/plans/backlog/p-10-m9-reconcile-throughput.md` has been `git mv`'d through `in-progress/` → `completed/` per the lifecycle convention in [`docs/plans/README.md`](../README.md).
- No regression in pre-existing M9 tests; all new code is covered by unit tests against fixtures (no network).

## Current state (as of plan-base `9ba54d1`)

- **M9 is sequential.** No `M9_CONCURRENCY` knob exists. `reconcile_command` at `src/bffi_pipeline/cli.py:767` does not expose a `--concurrency` flag. The orchestrator at `apply_reconciliation` in `reconcile.py` walks fields one at a time. M6 has run at `c=4` since P-02 § A6.
- **No persistent picker cache.** `data/` carries `judge-cache.sqlite` (M6's cache) but no equivalent for M9. The picker re-pays the LLM cost every time the same `(literal, candidate_set)` recurs.
- **Tier-0 is a literal exact match.** `local_concept_resolver.py:181`'s SPARQL CONSTRUCT does `?uri skos:prefLabel ?label .` with no normalisation on either side. `reconcile.py` *imports* `fold_diacritics` from `bffi_pipeline.blocking` and uses it inside `_normalise_for_similarity` at line 310, but that path is tier-1-only.
- **`bffi-pipeline load-finto`** writes per-vocabulary RDF files under `data/finto-dumps/`. Phase B's cache invalidation hooks into the SHA-256 of these files; Phase C's `bffi:foldedLabel` materialisation extends `load-finto` to emit a parallel folded-form triple at load time.
- The picker uses `LangChainLLMPicker(model_name=primary_model)` (`cli.py:855`), a single 8B model — no cascade. Phase A's thread safety verification is the first place that surfaces if the LangChain client is multi-thread-safe.
- **The watchdog from P-03 is M6-only today.** `src/bffi_pipeline/stages/watchdog.py:1` opens with "Structured event emission for the M6 LLM-call watchdog (plan P-03)." The event vocabulary at line 53 (`timeout`, `retry`, `escalate`, `give_up`, `pair_budget_exceeded`) is generic enough to extend; the budget enforcement lives in `judge.py` and has no M9 counterpart in `reconcile.py`. Phase A.4 closes this gap.
- **Phase 1 (tier-0 + candidate query) is serial.** After Phase A shipped, `apply_reconciliation`'s pre-pass — `local_resolver.resolve` (Fuseki SPARQL) followed by `client.query` / `fallback_client.query` (Finto / VIAF HTTP) — still walks one entity at a time. The 2026-05-12 Phase A bench measured this as ~70 % of M9 wall on the May 12 corpus (5 460 s total; ~1 660 s in parallel tier-2; ~3 800 s in serial Phase 1). Phase A2 closes this gap.
- M6 cache parallel: `data/judge-cache.sqlite` schema + the cross-thread fix in `1452a4f`. Phase B mirrors this exactly, including the same `BEGIN IMMEDIATE` write pattern.

---

## Phase A — M9 concurrency knob + watchdog wiring

Estimated wall-time: ~1.5 days. Independent of B and C; can ship first.

Phase A combines two structurally-linked changes: introducing `c=4` worker concurrency for the picker, **and** wiring the M9 picker call site through the same watchdog pattern P-03 shipped for M6. Running M9 at `c=4` without per-call/per-field timeout enforcement amplifies the hang-blocks-worker risk — one stuck picker call would silently sterilise 25 % of throughput — so the watchdog work has to land in the same phase that introduces concurrency, not as a follow-up.

### A.1. Surface the knob in config + CLI

- Add `m9_concurrency: int = Field(default=4, alias="M9_CONCURRENCY")` to `Settings` in `src/bffi_pipeline/config.py`. Default `4` matches M6's value (the P-02 § A6 throughput knee on M2 Max).
- Add `--concurrency` (Annotated `int`) to `reconcile_command` at `cli.py:767`, defaulting to `get_settings().m9_concurrency`. Document that overriding it is for benching.
- Threading happens *inside* `apply_reconciliation`; the CLI just plumbs the int through.

### A.2. Thread-pool the tier-2 dispatch in `apply_reconciliation`

- Tier-0 / tier-1 / tier-3 (fallback) stay single-threaded — they're cheap and they write back to the graph, so serialising them avoids a lock dance.
- Tier-2 picker calls go through `concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)`. Submit one future per `(EntityRequest, candidate_list)` and gather results before the graph-write phase. Result order must be deterministic — sort by `(work_uri, field_predicate, literal)` on collection so re-runs produce byte-identical Turtle.
- Reuse a single `httpx.Client` across workers (it's thread-safe). Picker LLM client: instantiate one `LangChainLLMPicker` per worker thread (`thread-local`) — the LangChain OpenAI-compat client has no documented thread-safety guarantee, and one client per thread is cheap.

### A.3. Provenance ordering

The provenance writer must produce stable Activity-URI sequencing under concurrency. Two options:

- Pre-mint Activity UUIDs in the orchestrator before dispatch (each future receives its UUID); the writer side stays single-threaded and just emits in collection order.
- Use a writer-side `threading.Lock` and let the futures call into the writer.

Pick option 1 — it preserves the existing writer's single-threaded contract and keeps the determinism gate explicit (sort the futures-result list before serialising).

### A.4. Watchdog integration on the M9 picker call site

Mirror P-03's M6 watchdog wiring at the M9 picker call site so a hung picker call can't sterilise a worker thread indefinitely. The retry behaviour reuses the existing HTTP-timeout stack on the LangChain → mlx-lm path; this sub-step adds the **outer per-field budget**, the **stage marker** for budget-exhausted fields, and the **event emission** that makes the activity visible in `pipeline.log` and `watchdog-events.jsonl`.

Concretely:

- **New config knob**: `llm_m9_field_timeout_seconds: int` in `src/bffi_pipeline/config.py`, alias `LLM_M9_FIELD_TIMEOUT_SECONDS`. Default `180` (picker fields resolve faster than M6 pairs because the model is single-tier, but the budget needs to absorb a retry stack). Parallels the existing `llm_call_timeout_seconds` / `llm_pair_timeout_seconds` pair.
- **Per-call timeout** (`LLM_CALL_TIMEOUT_SECONDS`) already flows through to the LangChain OpenAI-compat client's HTTP timeout — verify the picker honours it before claiming Phase A done (a one-line `client_kwargs={"timeout": settings.llm_call_timeout_seconds}` if missing).
- **Per-field budget wrap**: inside `apply_reconciliation`, wrap the picker dispatch for each `(EntityRequest, candidate_list)` in a `concurrent.futures.Future.result(timeout=field_budget)` call. On `TimeoutError`:
  - Emit a `field_budget_exceeded` watchdog event via `emit_watchdog_event(...)` keyed on `(canonical_work_uri, field_predicate, literal)`.
  - Treat the field as tier-3 fallback (take the highest-lexical candidate and flag the binding with `bffi:descriptionAuthentication = <bib:auth/needs-review>`), and stamp the provenance Activity with `bffi-prov:stage = "watchdog-aborted"` matching M6's contract.
  - Cancel the future if possible (best-effort — `concurrent.futures` can't always cancel an in-flight call).
- **Extend the `WatchdogEvent` Literal** in `src/bffi_pipeline/stages/watchdog.py:53` from the current 5-value enum to include `"field_budget_exceeded"`. This is a one-line change; the event-emitter is already generic.
- **Don't add an `escalate` event for M9** — the picker uses a single 8B model (`cli.py:855`'s `LangChainLLMPicker(model_name=primary_model)`), so there's no cascade hop to escalate to. If a future plan introduces an M9 cascade, that's where the `escalate` event re-applies.

### A.5. Tests

- Unit: synthesise a fixture canonical Work with 12 ambiguous fields; assert M9 produces the same Turtle at `concurrency=1` and `concurrency=4`. Byte-identical.
- Unit: simulate `LangChainLLMPicker` raising on one of N concurrent calls; assert the orchestrator either retries deterministically (per existing M9 retry policy) or aborts with a typed error — never produces a partial graph.
- Unit (watchdog): stub picker that sleeps past the field budget; assert `field_budget_exceeded` event emitted to stderr + `watchdog-events.jsonl`, the field is marked tier-3 fallback with `bffi-prov:stage = "watchdog-aborted"`, the orchestrator continues to the next field cleanly.
- Unit (watchdog): stub picker that times out on the first attempt but succeeds on retry; assert one `timeout` + one `retry` event are emitted, the final binding matches the second-attempt verdict.
- Integration: 50-record slice of the 5 k sample, `concurrency=4`, vs. the sequential baseline. Assert wall-time speedup ≥ 2.5× (margin for serial overhead).

### A.6. Acceptance

- [ ] `M9_CONCURRENCY` env var + `--concurrency` CLI flag wired through.
- [ ] `ThreadPoolExecutor` dispatch in `apply_reconciliation` with deterministic result ordering.
- [ ] One LangChain picker per worker thread; one shared `httpx.Client`.
- [ ] Byte-stability test: identical Turtle at `c=1` and `c=4` on the 12-field fixture.
- [ ] `LLM_M9_FIELD_TIMEOUT_SECONDS` env var wired through `Settings`; per-field budget wrap on picker calls; `WatchdogEvent` Literal extended with `field_budget_exceeded`.
- [ ] Watchdog event-emission test: stubbed-hang fixture produces the expected stderr + JSONL entries; budget-exhausted fields stamp `bffi-prov:stage = "watchdog-aborted"` and fall through to tier-3.
- [ ] 5 k re-run at `c=4` with default budgets fires **zero** `field_budget_exceeded` events (mirrors P-03's "zero events on the 5k production-style run" outcome). Non-zero count → tune budget or investigate before declaring done.
- [ ] 5 k re-run at `c=4` clocks M9 ≤ 1 900 s (≥ 3× speedup vs. 5 722 s baseline).
- [ ] Fresh [`docs/performance/<date>-5k-m2-max-phase-a.md`](../../performance/) snapshot committed.

### A.7. Rollback

Two-step revert mirroring Phase B's pattern:

1. Set `M9_CONCURRENCY=1` (drops back to sequential) + `LLM_M9_FIELD_TIMEOUT_SECONDS=0` (disables the watchdog wrap; `0` is interpreted as "no budget" by the orchestrator). Operationally restores pre-Phase-A behaviour without code revert.
2. If full revert needed: git revert the Phase A commit. `Settings.m9_concurrency` and `Settings.llm_m9_field_timeout_seconds` can stay (unused). The `WatchdogEvent` enum extension is forward-compatible — old data files don't carry the new event type. M6 is unaffected; the changes are scoped to M9's orchestrator and one-line widening of the watchdog event vocabulary.

---

## Phase A2 — Phase 1 parallelisation (tier-0 + Finto/VIAF candidate query)

Estimated wall-time: ~1 day. Builds on Phase A's `ThreadPoolExecutor` pattern, no new mechanisms. Added to the plan after the Phase A bench surfaced that serial Phase 1 (Fuseki SPARQL + Finto/VIAF HTTP) is ~70 % of M9 wall on the May 12 corpus and was not addressed by Phase A's tier-2 lever.

### A2.1. Surface the knob in config + CLI

- Add `m9_phase1_concurrency: int = Field(default=8, alias="M9_PHASE1_CONCURRENCY")` to `Settings`. Default `8` — Fuseki SPARQL is local (lightweight even under load) and Finto's REST API tolerates moderate concurrency. Higher than the picker's `c=4` because mlx-lm is the binding constraint there (GPU); Phase 1's binding constraints are different services with more headroom.
- Add `--phase1-concurrency` (Annotated `int`) to `reconcile_command`. Sentinel `-1` falls through to `settings.m9_phase1_concurrency` — same ergonomics as `--concurrency` in Phase A.
- The CLI doesn't need a separate watchdog sidecar; Phase 1 errors surface as HTTP exceptions handled by the existing `httpx.Client(timeout=10.0)` per-request timeout.

### A2.2. Thread-pool the Phase 1 walk in `apply_reconciliation`

The current `for idx, request in enumerate(request_list):` block becomes:

- A `_phase1_resolve(idx, request) -> tuple[int, Phase1Result]` worker function dispatched through `concurrent.futures.ThreadPoolExecutor(max_workers=phase1_concurrency)`.
- `Phase1Result` is a tagged union: either a `ReconciliationOutcome` (the entity resolved at tier-0 / fictional / lexical / no_candidate paths) **or** a `(EntityRequest, sorted_candidates)` deferred entry for picker dispatch in Phase 2 (Phase A's existing pool).
- Workers share the orchestrator's `client`, `fallback_client`, `local_resolver` instances. These are already built on `httpx.Client` (thread-safe) and stateless SPARQL queries.
- Result collection sorts by `idx` so the canonical graph mutations + provenance emit in submission order — same determinism gate Phase A uses.
- `started_at[idx]` capture happens inside the worker so the provenance Activity's `startedAtTime` is accurate per-request.

### A2.3. Thread safety verification

- `FintoSkosmosClient`, `ViafClient`: review for mutable state. Both wrap an `httpx.Client` (thread-safe) plus an `@lru_cache`-decorated query helper (also thread-safe per CPython).
- `LocalConceptResolver` (Fuseki-backed): SPARQL CONSTRUCT queries are stateless. Verify the connection-pool sizing on `httpx.Client` matches `phase1_concurrency` (default 8) so workers don't queue on connections.
- `_finto_search_query`'s `@lru_cache`: thread-safe by CPython contract. No change needed.
- Per-request HTTP timeouts: existing `httpx.Client(timeout=10.0)` in `cli.py` covers the per-request bound; no Phase-1 watchdog event emission needed (HTTP exceptions already raise cleanly and route to `no_candidate` or fallback).

### A2.4. Tests

- **Byte-stability** (mirrors A.4): fixture with 50+ tier-0-resolved entities and 12+ picker-deferred entities; run at `phase1=1, phase2=4` and `phase1=8, phase2=4`; assert byte-identical canonical Turtle.
- **Mixed-tier distribution**: fixture exercising all five outcome paths (fictional / local / lexical / picker-bound / no_candidate); assert outcome distribution and per-entity outcomes are identical across phase1 concurrency values.
- **Stub-client call counts**: stubs that increment a thread-safe counter on each `query` / `resolve` call; assert N requests yield exactly N `query` invocations and tier-0-resolved entities skip `query` entirely (the tier-0-short-circuit still works under concurrency).
- **Connection-error resilience**: stub `FintoSkosmosClient` raising `httpx.ReadTimeout` on one of N concurrent requests; assert that failure routes to `no_candidate` for that entity, other entities resolve normally, no thread errors.

### A2.5. Acceptance

- [ ] `M9_PHASE1_CONCURRENCY` env + `--phase1-concurrency` CLI flag wired through.
- [ ] Phase 1 walk dispatched through `ThreadPoolExecutor`.
- [ ] All Phase A2 unit tests above pass; existing M9 tests stay green.
- [ ] 5k re-run with `--phase1-concurrency 8 --concurrency 4` on the May 12 corpus (same input the Phase A bench used) clocks M9 ≤ **1 900 s** — the original ≥3× speedup target, now achievable with both concurrency levers in place.
- [ ] Fresh `docs/performance/<date>-5k-m2-max-phase-a2.md` snapshot committed, comparing against both the Phase A snapshot (5 460 s, single-lever) and the 5 722 s baseline (zero-lever).
- [ ] The snapshot's extrapolation table projects A+A2 wall on the full 800k corpus; if it doesn't unblock overnight on its own, the "Implications" section informs whether Phase B/C are still needed for the overnight target or just for production polish.

### A2.6. Rollback

- Set `M9_PHASE1_CONCURRENCY=1` to restore the post-Phase-A serial Phase 1. Phase A's tier-2 parallelism is unaffected; M9 wall reverts to ~5 460 s on the May 12 corpus.
- If full revert needed: git revert the Phase A2 commit. `Settings.m9_phase1_concurrency` stays (unused). No vocabulary or schema changes, so the revert is purely orchestrator-shape.

---

## Phase B — Persistent picker cache (`data/reconcile-cache.sqlite`)

Estimated wall-time: ~1-1.5 days. Independent of A; can be developed in parallel but ship after A so the warm-cache bench is taken at concurrent throughput.

### B.1. Schema

Mirror M6's `JudgeCache`. Single table:

```sql
CREATE TABLE picker_cache (
    key               TEXT PRIMARY KEY,   -- sha256(literal || candidates || prompt || model || finto_sha)
    decision_uri      TEXT,                -- empty string when "uncertain"
    confidence        REAL NOT NULL,
    rationale         TEXT NOT NULL,
    finto_vocab       TEXT NOT NULL,       -- e.g. "yso" / "finaf" / "kauno"
    finto_sha         TEXT NOT NULL,       -- sha256 of data/finto-dumps/<vocab>.ttl at decision time
    prompt_hash       TEXT NOT NULL,
    model_name        TEXT NOT NULL,
    decided_at        TEXT NOT NULL,       -- ISO 8601
    activity_uuid     TEXT NOT NULL        -- provenance Activity that originally produced this verdict
);
CREATE INDEX picker_cache_vocab_sha ON picker_cache(finto_vocab, finto_sha);
```

`key` formula (deterministic, ordered):

```
sha256(
    normalised_literal +     # NFKC + casefold + fold_diacritics
    "|" + sorted(candidate_uris).join(",") +
    "|" + prompt_hash +
    "|" + model_name +
    "|" + finto_vocab + ":" + finto_sha
)
```

`finto_sha` is the SHA-256 of the on-disk dump for the vocabulary the decision belongs to. Computed once per `apply_reconciliation` run (loaded as a `dict[vocab, sha]` at startup, not per-call). Mismatch on the next call → cache miss → re-decide. **No polling, no timestamp chasing.**

### B.2. Lookup-then-dispatch ordering under concurrency

The cache lookup MUST happen *before* the thread-pool dispatch, otherwise N threads can pay for the same uncached decision before any of them writes:

1. Single-threaded loop: for each `(EntityRequest, candidate_list)`, compute `key` and look up in `picker_cache`.
2. Hits → record decision + emit a provenance Activity with `prov:wasInfluencedBy <cached-activity-uuid>` (mirrors how M6's cache traces re-use).
3. Misses → dispatched to the thread pool from Phase A.
4. Pool results → write back to cache (using `BEGIN IMMEDIATE` for atomic insert, matching the M6 fix in `1452a4f`).

### B.3. Operator UX

- New `Makefile` target: `make clean-caches` removes `data/reconcile-cache.sqlite` (and `judge-cache.sqlite` for symmetry). Cache file is gitignored.
- `bffi-pipeline reconcile --no-cache` flag disables cache lookup (useful for bench / debugging).
- After a `bffi-pipeline load-finto` refresh, the cache transparently invalidates for the touched vocabulary on the next call. Documented in `docs/runbook.md`.
- The picker prompt header in `prompts/picker_v1.txt` carries a one-line note: "Changes here invalidate `reconcile-cache.sqlite`." Same contract as `judge_v1.txt`.

### B.4. Tests

- Unit: insert one row, look up by key, get the row back. Look up a non-matching key, get `None`.
- Unit: insert a row with `finto_sha = "deadbeef..."`, look up with `finto_sha = "different..."`, get `None` (vocabulary refresh invalidates).
- Unit: cross-thread write — spawn 4 threads each writing a different key, assert no `InterfaceError` (the M6 regression precedent).
- Integration: 5 k slice, two consecutive runs. First run fills the cache; second run hits ≥ 90 %; M9 wall on the second run ≤ 100 s.

### B.5. Acceptance

- [ ] Schema + indices created via `sqlite3` from Python on first cache miss (no separate migration step).
- [ ] Cache key includes `finto_sha` per vocabulary; mismatch invalidates per-vocabulary cleanly.
- [ ] Lookup-then-dispatch ordering under Phase A's `c=4` concurrency; no double-dispatch on the same key.
- [ ] Provenance Activities for cache hits carry `prov:wasInfluencedBy <cached-activity-uuid>`.
- [ ] Warm-cache 5 k re-run: hit rate ≥ 90 %, M9 wall ≤ 100 s.
- [ ] [`docs/performance/<date>-5k-m2-max-phase-b.md`](../../performance/) snapshot committed.

### B.6. Rollback

Two-step revert:

1. Set `BFFI_M9_CACHE_DISABLED=1` (no-op env knob added in B.1 for emergency-disable; cheap insurance) — re-runs fall back to live picker calls. Operationally restores the pre-Phase-B behaviour without code revert.
2. If full revert needed: git revert the Phase B commit. The cache file is gitignored; no on-disk artefact is committed.

---

## Phase C — Tier-0 normalisation + `skos:altLabel` inclusion

Estimated wall-time: ~1-2 days. Gated by the 200-sample audit. Ship after B so the new tier-0 hits route through the cache from the start.

### C.1. `bffi:foldedLabel` materialisation at load time

Extend `bffi-pipeline load-finto` to emit one extra triple per `skos:prefLabel` (and `skos:altLabel`, see C.3):

```turtle
<concept> bffi:foldedLabel "fold(prefLabel)" .
```

Where `fold(s)` = `NFKC → casefold → fold_diacritics → " ".join(s.split())`.

Stored alongside the original prefLabel in `data/finto-dumps/<vocab>.ttl`. Adds a `--fold-pref-labels` flag, **default off as of 2026-05-13** after the Phase C bench attempt surfaced that the materialised triples doubled Phase 1 SPARQL traffic without offsetting picker-call savings on the May 12 corpus. Idempotent — re-running adds no duplicates because the predicate is deterministic. To use the tier-0 expansion, operators flip **both** `--fold-pref-labels` here AND `BFFI_M9_TIER0_EXPANSION=1` at reconcile time; either alone is a no-op.

Why materialise vs. computing in the SPARQL query: Fuseki has no `fold_diacritics` builtin, so the alternative is fetching every candidate label to Python and folding there — orders of magnitude more SPARQL work per call. Materialisation moves the cost to load-time, paid once per Finto refresh.

### C.2. Tier-0 SPARQL update

`local_concept_resolver.py:153`'s SPARQL becomes:

```sparql
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX bffi:  <http://urn.fi/URN:NBN:fi:schema:bffi#>

SELECT ?uri ?source_vocab WHERE {
  GRAPH ?source_vocab {
    ?uri bffi:foldedLabel ?folded .
    FILTER(?folded = ?folded_literal)
  }
}
ORDER BY ?source_vocab
```

`?folded_literal` is the cataloguer literal passed through the same `fold()` Python-side. Bound positions, not text concatenation, per the existing SPARQL function pattern.

### C.3. Cataloguer-side strip rules (applied to the input literal *before* fold)

Three rules, each independently testable:

1. **Trailing parenthetical dates**: regex `\s*\(\d{4}(?:-\d{4})?\)\s*$` → strip. Example: `"Tolkien, J. R. R. (1892-1973)"` → `"Tolkien, J. R. R."`.
2. **MARC subfield `$e` role markers** appended to 7XX agents: split off everything after the final `,` if the suffix is a single lowercase Finnish/Swedish role word from a whitelist (`ohjaaja`, `säveltäjä`, `näyttelijä`, `kuvaaja`, `käsikirjoittaja`, `dirigent`, `kompositör`, …). Whitelist lives in `src/bffi_pipeline/blocking.py` so it's reusable. Example: `"Hamilton, Guy, ohjaaja"` → `"Hamilton, Guy"`.
3. **Fictional-character qualifier**: `(fiktiivinen hahmo)` / `(fiktiv gestalt)` markers stay routed to the existing `fictional_character` request kind — **no change**, just documented as the rule that holds.

Rules 1 and 2 are ordered (date strip before role strip — `"Hamilton, Guy, ohjaaja (1923-)"` should resolve cleanly).

### C.4. `skos:altLabel` inclusion

Currently tier-0 only matches `skos:prefLabel`. Phase C adds `skos:altLabel` and `skosxl:altLabel` to the materialised set. The fold is applied at load-time to both:

```turtle
<concept> bffi:foldedLabel "fold(prefLabel)" .
<concept> bffi:foldedLabel "fold(altLabel-1)" .
<concept> bffi:foldedLabel "fold(altLabel-2)" .
```

Tier-0 lookup is the same SPARQL — it just sees more candidate labels per concept.

Critical constraint inherited from `local_concept_resolver.py`: if the lookup returns **multiple URIs**, fall through to tier-1/2. Never bind on ambiguity. The existing function preserves this; the test in C.6 pins it.

### C.5. The 200-sample audit gate

**Do not ship Phase C until** a cataloguer (or, fallback, the plan executor with documented uncertainty flagged for the cataloguer team) has reviewed 200 tier-0-promoted hits from the 5 k re-run and confirmed every binding is correct.

Process:

1. Run M9 on the 5 k sample with Phases A + B + a feature-flagged C (`BFFI_M9_TIER0_EXPANSION=1`).
2. Diff the per-record bindings vs. the pre-Phase-C baseline. Sample 200 of the new tier-0 hits uniformly at random (`random.seed(42)`).
3. For each, manually confirm the bound URI matches the cataloguer literal's intended authority. Record verdict in `gold/reconcile-audit-200.jsonl`.
4. **Gate**: zero false-positive bindings. Any disagreement → fix the rule before unflagging.

The 200 audited rows graduate into the P-06 gold-set on completion (`gold/contrib.jsonl` neighbour).

### C.6. Tests

- Unit per strip rule (C.3.1, C.3.2): table-driven with 10 cases per rule, including edge cases (multiple commas, dates without space, etc.).
- Unit for altLabel ambiguity: synthetic fixture with two YSO concepts both carrying "Helsinki" as altLabel; assert tier-0 falls through, no binding committed.
- Integration: 5 k re-run with C enabled — assert tier-0 hit count up by ≥ 30 % of the tier-2 baseline count, no new bindings carry a different URI from the existing tier-2 result on the same literal.
- Byte-stability: re-run twice with C enabled, assert canonical.ttl identical.

### C.7. New bindings carry `needs-review` when the fold loses information

When a tier-0 binding has `fold(literal) == fold(prefLabel) && literal != prefLabel`, the binding's AdminMetadata carries `bffi:descriptionAuthentication <bib:auth/needs-review>` so cataloguers can audit the imperfect matches in Skosmos. The exact-string matches (the pre-Phase-C behaviour subset) are unaffected.

This is the safety net in case the 200-audit misses a rare collision.

### C.8. Acceptance

- [x] `bffi:foldedLabel` materialisation lands behind `--fold-pref-labels` (default **off** post-2026-05-13 — see Phase C commit hash above; flag was originally specified default-on, flipped after the bench-attempt evidence).
- [ ] Tier-0 SPARQL switched to the folded predicate; cataloguer-side fold + strip rules applied.
- [ ] Two altLabel ambiguity tests pass (single-hit binds, multi-hit falls through).
- [ ] 200-sample audit committed at `gold/reconcile-audit-200.jsonl`, zero false-positives.
- [ ] 5 k re-run: tier-0 hit count up ≥ 30 % of baseline tier-2, tier-2 (LLM) count ≤ 0.7 ×, M9 wall ≤ 70 s with warm cache.
- [ ] Fresh `bffi:foldedLabel`-carrying Finto dumps regenerated (re-run `load-finto`).
- [ ] [`docs/performance/<date>-5k-m2-max-phase-c.md`](../../performance/) snapshot committed.

### C.9. Rollback

- Set `BFFI_M9_TIER0_EXPANSION=0` (the feature flag from C.5) — tier-0 falls back to literal exact match on `skos:prefLabel`. Phases A + B stay green.
- The `bffi:foldedLabel` triples in Finto dumps are inert if not queried; no need to regenerate the dumps.
- If full revert needed, `git revert` the Phase C commit. `load-finto` reverts to the prior emission set.

---

## Phase E — Prompt ordering for mlx-lm prefix-cache stickiness

Estimated wall-time: ~0.25 day (~1-2 hours). Pure orchestration-shape change with no new dependencies. Ship before the next 5 k bench so the next snapshot measures the speedup cleanly.

**Motivation (promoted from deferred-levers on 2026-05-13).** mlx-lm's prompt-prefix cache is keyed on the longest-common-prefix of consecutive `POST /v1/chat/completions` calls (per P-02 § "Throughput findings" and the mlx-lm 0.31 server's `--prompt-cache-size` LRU). Today the deferred picker queue is dispatched in the order `apply_reconciliation` walked the canonical Works — effectively random with respect to vocabulary and request kind. Consecutive picker calls therefore frequently share zero usable prefix (e.g. a fictional-character pick on Work A is followed by a YSO subject pick on Work B). Sorting the queue so that calls sharing the system prompt + vocabulary + candidate-list-style cluster together turns the cache LRU from "always cold" into "warm for runs of consecutive same-kind calls".

The Phase C bench attempt (2026-05-13) showed 1 500 picker calls completed in ~1h 35m on the M2 Max — ~3.8 s per call wall. The system prompt + few-shot exemplars are ~95 % of every picker prompt, so even a partial prefix-cache hit collapses per-call wall to roughly the decode time alone. Expected impact: **5-15 % picker-phase wall reduction**, larger on YSO-heavy corpora (long runs of same-kind picks) than on the heterogeneous May 12 sample.

Phase E does **not** change any picker input, prompt text, or candidate list — only the order in which the picker queue is drained. Bindings are byte-stable under any ordering because the orchestrator already gathers futures and sorts by `(work_uri, field_predicate, literal)` before graph-write (per Phase A's A.3 determinism gate).

### E.1. Sort key in `_picker_phase_pool`

In `src/bffi_pipeline/stages/reconcile.py`, sort the `deferred: list[tuple[int, EntityRequest, list[AuthorityCandidate]]]` queue immediately before dispatching to the `ThreadPoolExecutor`. Sort key, in order:

1. **`request.kind`** — clusters fictional-character picks together, then person-author picks, then subject picks, etc. The system prompt + few-shot exemplars vary slightly per kind (the picker prompt has kind-conditional sections in `prompts/picker_v1.txt`), so picks of the same kind share the longest prompt prefix.
2. **`candidates[0].source_vocabulary`** — within a kind, cluster by the dominant candidate vocabulary (`yso`, `finaf`, `kauno`, `viaf`, …). Same-vocabulary candidates share authority-style language in the prompt (vocabulary-specific candidate formatting).
3. **A stable fingerprint of `sorted(c.uri for c in candidates)`** — within a kind+vocab cluster, group calls with overlapping candidate sets. The candidate URIs are rendered into the prompt body; identical or near-identical candidate sets share long prompt-body prefixes.
4. **`request.literal`** — final tie-breaker for byte-stability across runs (the literal varies last in the prompt).

Use a stable sort (Python's `list.sort` is stable). The `idx` field rides along so the post-pool result merge still places each binding at the correct position in the canonical graph.

### E.2. Env var + CLI flag

- Add `m9_picker_ordering: str = Field(default="prefix-cache", alias="BFFI_M9_PICKER_ORDERING")` to `Settings`. Valid values: `"prefix-cache"` (Phase E behaviour, default) and `"submission"` (pre-Phase-E behaviour — submission order, useful for bench A/B and rollback).
- No CLI flag — env var only. Operators don't need per-run override; the bench-time A/B is settable via env.
- Document the env var in `docs/runbook.md` under the M9 reconcile section.

### E.3. Tests

- **Byte-stability**: fixture with 50+ deferred picker entries; run with `BFFI_M9_PICKER_ORDERING=prefix-cache` and `BFFI_M9_PICKER_ORDERING=submission`; assert byte-identical canonical Turtle output (modulo `descriptionChangeDate` timestamps). The orchestrator's existing post-pool sort-by-idx guarantees this — the test pins the contract.
- **Sort key correctness**: synthetic deferred queue with mixed kinds and vocabularies; assert the sorted order matches the documented key (fictional-character before person before subject; within each, `finaf` before `kauno` before `yso` lexicographically; within each, same-candidate-set runs cluster).
- **Empty / single-entry queue**: assert the sort is a no-op (no exceptions, no reordering).

### E.4. Acceptance

- [ ] `BFFI_M9_PICKER_ORDERING` env var wired through `Settings`; default `"prefix-cache"`.
- [ ] `_picker_phase_pool` sorts `deferred` per E.1's key when ordering is `"prefix-cache"`; preserves submission order when `"submission"`.
- [ ] Byte-stability test passes: identical canonical Turtle under both ordering modes on the 50-entry fixture.
- [ ] Sort-key correctness test passes.
- [ ] 5 k re-run with `BFFI_M9_PICKER_ORDERING=prefix-cache` (against Phase A + A2, Phase C still flag-off) clocks picker-phase wall **≥ 5 % below** the Phase A2 baseline. (Smaller speedup is acceptable as documented in the snapshot's "Implications" — but a regression vs Phase A2 fails the acceptance gate.)
- [ ] Fresh [`docs/performance/<date>-5k-m2-max-phase-e.md`](../../performance/) snapshot committed. Includes side-by-side picker-phase wall + per-call median latency vs Phase A2's snapshot, and an mlx-lm cache-hit-rate proxy measurement (P-11's metrics-exporter Prometheus counter if available, otherwise the snapshot documents the proxy used).

### E.5. Rollback

- Set `BFFI_M9_PICKER_ORDERING=submission` — restores pre-Phase-E queue ordering. No code revert needed; bindings remain byte-stable across the flip.
- If full revert needed: git revert the Phase E commit. `Settings.m9_picker_ordering` field stays (unused). No vocabulary, schema, or output-shape changes.

### E.6. Why this is safe (and why Phase D isn't, yet)

Phase E is purely a dispatch-order change — every individual picker call receives the same prompt, candidate list, and few-shot exemplars it would have received in submission order. The picker is deterministic per input (temperature 0 in `prompts/picker_v1.txt`). The orchestrator's existing post-pool result-merge sorts by `idx` before graph-write, so the canonical Turtle is byte-stable regardless of completion order.

Phase D (batched picker — handing N fields per LLM call to amortise prompt-prefix cost) stays deferred because it *does* change picker inputs: the model must now disambiguate N entities in a single response, and long-context quality degradation on the 8B Qwen model is unmeasured against the P-06 gold-set. Phase D's gate is a gold-set big enough to detect a 1-2 % quality regression — the current `gold/contrib.jsonl` is too small. See `docs/plans/backlog/p-06-gold-set-growth.md`.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase A — `LangChainLLMPicker` not thread-safe; M9 produces duplicate Activity URIs or interleaved bindings under `c=4` | Medium | One client per worker thread (`threading.local`). A.4 byte-stability test catches any non-determinism at CI time. |
| Phase A — `httpx.Client` connection-pool exhaustion at `c=4` to Finto | Low | Reuse one client process-wide with `httpx.Limits(max_connections=10)`; the Finto API already throttles us. |
| Phase A — Picker call hangs indefinitely, sterilising one of four worker threads | Medium (mlx-lm precedent during P-02 / P-03 exists) | Watchdog per-field budget (A.4) + `field_budget_exceeded` event. Budget-exhausted fields fall through to tier-3 fallback so the canonical graph stays valid. Mirrors P-03's M6 wiring. |
| Phase A2 — Finto API rate-limit pressure at `phase1=8` | Medium | Finto doesn't publish a hard rate limit; 8 concurrent persistent connections is well within typical web-API tolerances. Existing `httpx.Client(timeout=10.0)` covers per-request stuck calls. On sustained 429s in the bench, drop to `phase1=4` (still 4× speedup over serial). Operator-tunable via env var. |
| Phase A2 — Fuseki connection-pool saturation under concurrent SPARQL | Low | Local Fuseki handles 8 concurrent connections trivially (it's not a production load). If the bench surfaces queue pressure, `httpx.Limits` configured at the orchestrator entry point is the lever. |
| Phase A2 — Non-deterministic ordering on parallel Phase 1 leaks into canonical / provenance graphs | Low | Same submit-then-sort pattern Phase A already uses for tier-2. Byte-stability test in A2.4 pins this. |
| Phase B — Cache key collision (different `(literal, candidates)` pairs hash to the same key) | Effectively zero | 256-bit SHA. Collisions are statistically impossible at corpus scale. |
| Phase B — Over-caching: Finto adds a new authority and the cache returns the old "no match" verdict | Low (Finto refresh is operator-controlled) | `finto_sha` in the key; refresh invalidates per-vocabulary. Operator runbook documents the daily YSO/KANTO refresh cadence. |
| Phase B — Cross-thread SQLite `InterfaceError` (the 2026-05-12 M6 precedent) | Medium-low | Mirror M6's fix in `1452a4f`: `BEGIN IMMEDIATE`, per-thread connection. B.4 cross-thread test pins it. |
| Phase C — Silent false-positive merge (cataloguer literal binds to wrong authority because normalisation collides) | Medium | 200-sample audit gate (C.5) is the primary defence. `needs-review` flag (C.7) is the secondary safety net for fuzzy matches. |
| Phase C — `skos:altLabel` ambiguity at tier-0 causes legitimate exact-prefLabel matches to fall through | Low | The "never bind on multi-hit" constraint in `local_concept_resolver.py` already prevents this; the existing prefLabel path stays in place because folded prefLabels still get materialised. |
| Phase C — Cataloguer audit is delayed by external availability | Medium | Plan executor can perform the audit themselves with documented uncertainty flagged for later cataloguer review. Audit results are reviewable and reversible. |
| Phase E — Diminishing returns at corpus scale: 200-slot mlx-lm prompt-cache fills before clustering pays off | Medium | The 5 k bench is the canary; if picker-phase wall reduction is < 5 %, the snapshot's "Implications" section documents the next lever (cache-size tuning) without blocking other phases. Phase E's cost is ~1-2 hours implementation, so a low-return outcome is cheap. |
| Phase E — Sort key produces unstable dispatch order across runs (e.g. set iteration order leaks in) | Effectively zero | Sort key is fully deterministic (`request.kind` is a string enum, `source_vocabulary` is a string, candidate-URI fingerprint uses `sorted()`). Python's `list.sort` is stable. E.3 byte-stability test pins it. |
| All — Concurrent benches produce noisy timings on the M2 Max dev box | Low | Each phase's bench is repeated 3× and the median reported. Mlx-lm prefix-cache is warmed before measurement (10-call warmup loop). |

## Open issues to close before / during execution

- **Finto refresh policy in the runbook** (Phase B docs): which vocabularies the operator should refresh how often. Plan default: YSO + KANTO/FINAF daily on production; YSE weekly; KAUNO + MUSO opportunistic / on-demand. The plan executor adds the per-vocabulary recipe to `docs/runbook.md` during Phase B.
- **Phase D (batched picker) stays deferred** per the proposal. Phase E (prompt ordering) was promoted into this plan on 2026-05-13 after the Phase C bench attempt confirmed that picker-phase wall is still the largest contributor and Phase E is a zero-quality-risk lever. Phase D remains deferred because batching N picks per LLM call changes picker inputs and risks long-context quality degradation on the 8B model — the gate is a P-06 gold-set big enough to detect a 1-2 % regression, which doesn't exist yet. If A+A2+B+C+E close the gap to ≤ 8 h on the full 800 k corpus, Phase D is not needed and the plan completes. If wall-time is still > overnight after E, file a follow-up plan that picks up D once the gold-set has grown.
- **Provenance for cache-hit Activities**: the spec § 8 enum (`bffi-prov:stage`) currently has `reconciliation-llm`. Cache-hit Activities should carry the same stage (the decision is semantically the LLM's), with the `prov:wasInfluencedBy` pointer distinguishing them. No new stage value needed.
- **Phase C strip rules in non-Finnish corpora**: the `$e` role-marker whitelist (`ohjaaja`, `säveltäjä`, …) is Finnish/Swedish. If P-09 (`prop-09-library-agnostic-source`) graduates and a second library onboards with a different language, the whitelist needs to grow. Out of scope here; document as a P-09 follow-up.

## Cross-references

- [`docs/performance/2026-05-12-5k-m2-max.md`](../../performance/2026-05-12-5k-m2-max.md) — the baseline P-10 measures itself against.
- [`docs/proposals/prop-10-m9-reconcile-throughput.md`](../../proposals/prop-10-m9-reconcile-throughput.md) — source proposal; full lever rationale and the Finto-cadence evidence.
- [`docs/plans/completed/p-02-inference-stack-tuning.md`](../completed/p-02-inference-stack-tuning.md) — the prefix-cache + concurrency lever set P-10 reuses on the M9 side.
- [`docs/plans/backlog/p-06-gold-set-growth.md`](p-06-gold-set-growth.md) — the gold-set backlog that absorbs C.5's 200-sample audit.
- [`docs/proposals/prop-09-library-agnostic-source.md`](../../proposals/prop-09-library-agnostic-source.md) — the per-library decoupling proposal whose `LibrarySource` config interacts with Phase C's Finnish-specific role-marker whitelist.
- [`src/bffi_pipeline/stages/reconcile.py`](../../../src/bffi_pipeline/stages/reconcile.py) — `apply_reconciliation` orchestrator (Phase A) and `LangChainLLMPicker` call site (Phase B).
- [`src/bffi_pipeline/stages/local_concept_resolver.py`](../../../src/bffi_pipeline/stages/local_concept_resolver.py) — tier-0 SPARQL (Phase C).
- [`src/bffi_pipeline/cli.py:767`](../../../src/bffi_pipeline/cli.py) — `reconcile_command` signature (Phases A + B knobs).
- [`prompts/picker_v1.txt`](../../../prompts/picker_v1.txt) — picker prompt whose hash anchors Phase B's cache key.
- M6 cache parallel: `data/judge-cache.sqlite` schema + the cross-thread fix in `1452a4f`.
- [`NatLibFi/Finto-data`](https://github.com/NatLibFi/Finto-data) — the Finto vocabulary source repository whose commit history backs Phase B's per-vocabulary cadence (see proposal § "Finto's actual cadence").
- [`src/bffi_pipeline/stages/watchdog.py`](../../../src/bffi_pipeline/stages/watchdog.py) — the watchdog event-emission module (originally M6-scoped per P-03); Phase A.4 extends the `WatchdogEvent` Literal with `field_budget_exceeded` and wires the M9 picker call site through `emit_watchdog_event`.
- [`docs/plans/completed/p-03-m6-stall-watchdog.md`](../completed/p-03-m6-stall-watchdog.md) — the originating watchdog plan; the M9 wiring in A.4 mirrors its `LLM_CALL_TIMEOUT_SECONDS` + per-outer-budget pattern, with `LLM_M9_FIELD_TIMEOUT_SECONDS` as the M9-side analogue of `LLM_PAIR_TIMEOUT_SECONDS`.
