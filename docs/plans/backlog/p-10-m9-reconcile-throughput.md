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

- Phase A (M9 concurrency knob): `<unfilled>`
- Phase B (persistent picker cache): `<unfilled>`
- Phase C (tier-0 normalisation + altLabel inclusion): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: ~3-4 days end-to-end. Phase A ~1 day (½ code + ½ bench). Phase B ~1-1.5 days. Phase C ~1-2 days (rule design + cataloguer-sample audit gate). Each phase is independently shippable and lands with its own [`docs/performance/`](../../performance/) snapshot.

## Goal

Bring M9 reconcile wall-time on the full 800 k Helmet corpus from a linear-extrapolated ~10 days to **under one overnight window** (~8-10 h), without regressing any of the bind-quality measurements documented in the 2026-05-12 5k snapshot.

Concrete targets, measured on a 5 k re-run against the same sample (`data/sample-5k-marcxml/`, Python `random.seed(42)`, identical Fuseki state):

| Stage | 5k baseline (2026-05-12) | Target after Phase A | Target after Phase B (warm cache) | Target after Phase C |
|---|---|---|---|---|
| M9 wall | **5 722 s (95:22)** | ≤ 1 900 s (≥ 3× speedup) | ≤ 100 s (≥ 90 % cache hit rate) | ≤ 70 s |
| Tier-0 hit count | (baseline-X) | unchanged | unchanged | +30 % vs baseline-X |
| Tier-2 (LLM) call count | (baseline-Y) | unchanged | unchanged | ≤ 0.7 × baseline-Y |
| Bind-quality on 200 spot-checked decisions | reference | identical | identical | identical (audit gate) |

The Phase B "warm cache" target is a second consecutive run on the same corpus with no Finto refresh between runs — the realistic operator pattern on the production M5 Max box.

## Definition of done

- All three phases have filled-in phase commits, each on its own commit (no batching) so a partial revert is mechanical.
- The fresh [`docs/performance/`](../../performance/) snapshot taken after Phase C shows M9 ≤ 70 s on the 5k sample with the cache warm, and the extrapolation table in that snapshot projects ≤ 10 h for the full 800 k corpus.
- The 200-sample audit from Phase C is committed under `gold/reconcile-audit-200.jsonl` (feeds the P-06 backlog).
- `docs/plans/backlog/p-10-m9-reconcile-throughput.md` has been `git mv`'d through `in-progress/` → `completed/` per the lifecycle convention in [`docs/plans/README.md`](../README.md).
- No regression in pre-existing M9 tests; all new code is covered by unit tests against fixtures (no network).

## Current state (as of plan-base `9ba54d1`)

- **M9 is sequential.** No `M9_CONCURRENCY` knob exists. `reconcile_command` at `src/bffi_pipeline/cli.py:767` does not expose a `--concurrency` flag. The orchestrator at `apply_reconciliation` in `reconcile.py` walks fields one at a time. M6 has run at `c=4` since P-02 § A6.
- **No persistent picker cache.** `data/` carries `judge-cache.sqlite` (M6's cache) but no equivalent for M9. The picker re-pays the LLM cost every time the same `(literal, candidate_set)` recurs.
- **Tier-0 is a literal exact match.** `local_concept_resolver.py:181`'s SPARQL CONSTRUCT does `?uri skos:prefLabel ?label .` with no normalisation on either side. `reconcile.py` *imports* `fold_diacritics` from `bffi_pipeline.blocking` and uses it inside `_normalise_for_similarity` at line 310, but that path is tier-1-only.
- **`bffi-pipeline load-finto`** writes per-vocabulary RDF files under `data/finto-dumps/`. Phase B's cache invalidation hooks into the SHA-256 of these files; Phase C's `bffi:foldedLabel` materialisation extends `load-finto` to emit a parallel folded-form triple at load time.
- The picker uses `LangChainLLMPicker(model_name=primary_model)` (`cli.py:855`), a single 8B model — no cascade. Phase A's thread safety verification is the first place that surfaces if the LangChain client is multi-thread-safe.
- M6 cache parallel: `data/judge-cache.sqlite` schema + the cross-thread fix in `1452a4f`. Phase B mirrors this exactly, including the same `BEGIN IMMEDIATE` write pattern.

---

## Phase A — M9 concurrency knob

Estimated wall-time: ~1 day. Independent of B and C; can ship first.

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

### A.4. Tests

- Unit: synthesise a fixture canonical Work with 12 ambiguous fields; assert M9 produces the same Turtle at `concurrency=1` and `concurrency=4`. Byte-identical.
- Unit: simulate `LangChainLLMPicker` raising on one of N concurrent calls; assert the orchestrator either retries deterministically (per existing M9 retry policy) or aborts with a typed error — never produces a partial graph.
- Integration: 50-record slice of the 5 k sample, `concurrency=4`, vs. the sequential baseline. Assert wall-time speedup ≥ 2.5× (margin for serial overhead).

### A.5. Acceptance

- [ ] `M9_CONCURRENCY` env var + `--concurrency` CLI flag wired through.
- [ ] `ThreadPoolExecutor` dispatch in `apply_reconciliation` with deterministic result ordering.
- [ ] One LangChain picker per worker thread; one shared `httpx.Client`.
- [ ] Byte-stability test: identical Turtle at `c=1` and `c=4` on the 12-field fixture.
- [ ] 5 k re-run at `c=4` clocks M9 ≤ 1 900 s (≥ 3× speedup vs. 5 722 s baseline).
- [ ] Fresh [`docs/performance/<date>-5k-m2-max-phase-a.md`](../../performance/) snapshot committed.

### A.6. Rollback

Revert the `cli.py` + `reconcile.py` diff. The `Settings.m9_concurrency` field can stay (unused). M6 is unaffected — the changes are scoped to M9's orchestrator.

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

Stored alongside the original prefLabel in `data/finto-dumps/<vocab>.ttl`. Adds a `--fold-prefLabels` flag (default on for new dumps; idempotent — re-running adds no duplicates because the predicate is deterministic).

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

- [ ] `bffi:foldedLabel` materialisation lands behind `--fold-prefLabels` (default on).
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

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phase A — `LangChainLLMPicker` not thread-safe; M9 produces duplicate Activity URIs or interleaved bindings under `c=4` | Medium | One client per worker thread (`threading.local`). A.4 byte-stability test catches any non-determinism at CI time. |
| Phase A — `httpx.Client` connection-pool exhaustion at `c=4` to Finto | Low | Reuse one client process-wide with `httpx.Limits(max_connections=10)`; the Finto API already throttles us. |
| Phase B — Cache key collision (different `(literal, candidates)` pairs hash to the same key) | Effectively zero | 256-bit SHA. Collisions are statistically impossible at corpus scale. |
| Phase B — Over-caching: Finto adds a new authority and the cache returns the old "no match" verdict | Low (Finto refresh is operator-controlled) | `finto_sha` in the key; refresh invalidates per-vocabulary. Operator runbook documents the daily YSO/KANTO refresh cadence. |
| Phase B — Cross-thread SQLite `InterfaceError` (the 2026-05-12 M6 precedent) | Medium-low | Mirror M6's fix in `1452a4f`: `BEGIN IMMEDIATE`, per-thread connection. B.4 cross-thread test pins it. |
| Phase C — Silent false-positive merge (cataloguer literal binds to wrong authority because normalisation collides) | Medium | 200-sample audit gate (C.5) is the primary defence. `needs-review` flag (C.7) is the secondary safety net for fuzzy matches. |
| Phase C — `skos:altLabel` ambiguity at tier-0 causes legitimate exact-prefLabel matches to fall through | Low | The "never bind on multi-hit" constraint in `local_concept_resolver.py` already prevents this; the existing prefLabel path stays in place because folded prefLabels still get materialised. |
| Phase C — Cataloguer audit is delayed by external availability | Medium | Plan executor can perform the audit themselves with documented uncertainty flagged for later cataloguer review. Audit results are reviewable and reversible. |
| All — Concurrent benches produce noisy timings on the M2 Max dev box | Low | Each phase's bench is repeated 3× and the median reported. Mlx-lm prefix-cache is warmed before measurement (10-call warmup loop). |

## Open issues to close before / during execution

- **Finto refresh policy in the runbook** (Phase B docs): which vocabularies the operator should refresh how often. Plan default: YSO + KANTO/FINAF daily on production; YSE weekly; KAUNO + MUSO opportunistic / on-demand. The plan executor adds the per-vocabulary recipe to `docs/runbook.md` during Phase B.
- **Phase D (batched picker) and Phase E (prompt ordering)** stay deferred per the proposal. If A+B+C close the gap to ≤ 8 h on the full 800 k corpus, they're not needed and the plan completes. If wall-time is still > overnight after C, file a follow-up plan (P-11?) that picks up D + E.
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
