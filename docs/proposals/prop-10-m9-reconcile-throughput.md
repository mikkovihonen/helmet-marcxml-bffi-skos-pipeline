# P-10 — M9 reconcile throughput: concurrency + persistent picker cache + tier-0 expansion

**Status**: planning (graduated). See
[`docs/plans/backlog/p-10-m9-reconcile-throughput.md`](../plans/backlog/p-10-m9-reconcile-throughput.md)
for the executable plan with sub-step detail, acceptance gates, and rollback procedures per phase.
**Scope**: 3-4 days. Phase A (concurrency knob + thread-pool the orchestrator) is half a day. Phase B (persistent picker cache, analogous to `judge-cache.sqlite`) is 1 day. Phase C (tier-0 normalisation expansion + `skos:altLabel` inclusion) is 1-2 days including the sample audit. Phases D+E are deferred until A+B+C are measured.
**Proposal-base commit**: `ad4f6c4`. The "Motivation" reasons about the M9 stage as it ran in the [2026-05-12 5k snapshot](../performance/2026-05-12-5k-m2-max.md). If `main` has moved before this is acted on, re-verify with
`git diff ad4f6c4..HEAD --
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/stages/local_concept_resolver.py
src/bffi_pipeline/cli.py
prompts/picker_v1.txt`.

Material updates since drafting:

- `9ba54d1` — Phase B cache invalidation backed with Finto-cadence
  evidence from `NatLibFi/Finto-data` git history. Simplified from
  `prov:generatedAtTime` chasing to a per-row `sha256` of the
  on-disk Finto dump (the operator-controlled `bffi-pipeline
  load-finto` refresh is the realistic invalidation trigger).
- (graduation commit) — graduated into the plan; the proposal's
  Motivation, lever rationale, and Finto-cadence table are preserved
  here, the Approach / Prerequisites / Risks / Open-questions content
  is carried over into the plan with execution detail per phase
  (env vars, function names, sub-steps, acceptance checklists,
  rollback procedures).
- This commit — folded the watchdog pattern from P-03 into Phase A
  as sub-step A.4. Running M9 at `c=4` without per-call / per-field
  timeout enforcement amplifies the hang-blocks-worker risk (one
  stuck picker call would sterilise 25 % of throughput), so the
  watchdog wiring has to land in the same phase as concurrency.
  Adds `LLM_M9_FIELD_TIMEOUT_SECONDS` (analogous to M6's
  `LLM_PAIR_TIMEOUT_SECONDS`) and extends the `WatchdogEvent`
  Literal in `watchdog.py` with `field_budget_exceeded`. Phase A
  wall-time estimate bumped from ~1 day to ~1.5 days; plan total
  from 3-4 days to 3.5-4.5 days.

## Motivation

The 2026-05-12 5k run identified M9 reconcile as the new wall: **5 722 s (82.9 % of total wall-time)**, vs M6 judge at 258 s (3.7 %). Linear extrapolation to the full 800k Helmet corpus gives **~10 days** for M9 alone — well outside the "fits overnight" target that everything else now satisfies.

M9's call shape is already optimised at the decision-tier level (`reconcile.py:7-22`):

- **Tier 0** (`reconciliation-local`) — exact `skos:prefLabel` match against the locally-loaded Finto graphs. No HTTP, no LLM.
- **Tier 1** (`reconciliation-lexical`) — exactly one candidate with lexical similarity ≥ 0.95 and all others below. No LLM.
- **Tier 2** (`reconciliation-llm`) — multiple high-similarity candidates → picker.
- **Tier 3** (`reconciliation-fallback`) — picker said `uncertain` or confidence < 0.80; flag for review.

The 5 722 s is dominated by **Tier 2**, i.e. fields where tier-0 and tier-1 both failed. Three structural facts about the current implementation drive most of that cost:

1. **M9 runs sequentially.** No `M9_CONCURRENCY` flag exists in `reconcile.py` or `cli.py:767`'s `reconcile_command`; the orchestrator processes fields one at a time. M6 runs at `c=4` (the P-02 § A6 throughput knee on M2 Max).
2. **No persistent picker cache.** `data/` contains `judge-cache.sqlite` (M6's `(work_pair, prompt_hash)` cache) but no equivalent for M9. The picker re-pays the LLM call every time the same literal + candidate set comes up, and authority literals repeat heavily across a corpus.
3. **Tier 0 is a literal exact match.** `local_concept_resolver.py:153` SPARQLs `?uri skos:prefLabel ?label` with the raw cataloguer literal. No NFKC, no `fold_diacritics`, no casefold, no `skos:altLabel`, no date-stripping. `reconcile.py` *imports* `fold_diacritics` and uses it inside `_normalise_for_similarity` (line 310) for tier-1 lexical scoring, but the same normalisation isn't applied at tier-0.

## Approach

Three sequenced phases. Each is independently shippable, has its own re-run-the-5k acceptance test, and lands with a fresh `docs/performance/` snapshot so the speedup is on the record.

### Phase A — Concurrency knob on the M9 orchestrator

- New `M9_CONCURRENCY` env var (default `4`, matching M6) read by `reconcile_command` at `cli.py:767`. Surface it on the CLI as `--concurrency`.
- `apply_reconciliation` thread-pools the per-field work. Tier-0 / tier-1 stays single-threaded (cheap); only tier-2 picker calls go through the pool.
- Two thread-safety items to handle:
  - `httpx.Client` for the Finto / VIAF clients — share one `httpx.Client` per process (it's thread-safe).
  - `LangChainLLMPicker` — confirm thread safety of the underlying mlx-lm OpenAI-compat client; if not, build one picker per worker thread.
- Acceptance: 5k re-run at `c=4` clocks M9 in ≤ 1 900 s (≥ 3× speedup vs 5 722 s; some serial overhead expected).

### Phase B — Persistent picker cache (`data/reconcile-cache.sqlite`)

Mirror M6's `JudgeCache` pattern:

- Cache key: `sha256(literal_normalised || sorted_candidate_uris || prompt_hash || model_name)`.
- Cache value: the `PickerDecision` JSON + provenance fields (timestamp, decision, confidence).
- Lookup happens *inside* the LLM tier, before the HTTP call. A cache hit reuses the prior decision verbatim and writes the same `prov:Activity` (with `prov:wasInfluencedBy` pointing at the cached decision's Activity for traceability — matches how the judge cache handles re-decisions).
- Cache is regenerable, so a `make clean-caches` target wipes it; the cache file is gitignored.
- `prompt_hash` in the key means a prompt edit invalidates the cache, same contract as the judge cache. Documented in `prompts/picker_v1.txt` header.
- **Finto-graph invalidation key**: each cache row stores the `sha256` (or `mtime`) of the local authority graph file (`data/finto-dumps/<vocab>.ttl`) for the vocabulary the decision belongs to. Lookup checks the row's hash against the current file; mismatch → cache miss. Operationally, `bffi-pipeline load-finto` refreshes the dump and the cache transparently invalidates for the touched vocabulary on the next call. No polling, no timestamp chasing.

**Why the simple invalidation key is enough — Finto's actual cadence**:

Empirical evidence from the [`NatLibFi/Finto-data`](https://github.com/NatLibFi/Finto-data) git history (recent commits, May 2026) shows wildly different update frequencies across the vocabularies M9 hits:

| Vocabulary | Observed cadence | Most recent update |
|---|---|---|
| YSO published version (`yso-julkaisuversio`) | ~daily on weekdays | 2026-05-08 |
| FINAF (= KANTO, persons + corporate bodies) | daily | 2026-05-12 |
| YSO places (`YSO-paikat`) | daily | 2026-05-12 |
| YSE (educational) | 1-3 × per week | 2026-05-09 |
| **KAUNO** (fiction) | **≤ quarterly** | **2025-12-19** (prior: 2024-11, 2024-06, 2024-01) |
| **MUSO** (music) | **near-dormant** | **2021-09-28** (years between updates) |

So daily-or-coarser cache validity is the right model: the picker cache only invalidates when an operator runs `load-finto`, and operators can sensibly refresh YSO/KANTO daily while leaving KAUNO/MUSO alone for months. The cache's load-from-disk hash check is O(file-stat) so the overhead per call is negligible.

- Acceptance: 5k re-run after seeding the cache (i.e. a second consecutive run on the same corpus, no Finto refresh in between) has cache hit rate ≥ 90 % and M9 wall in ≤ 100 s. On a fresh corpus the cache fills as it goes; the steady-state hit rate on the full 800k will be measured at the first full run.

### Phase C — Tier-0 normalisation + `skos:altLabel` inclusion

Goal: push fields out of tier-2 into tier-0, where each hit is free.

- **Normalise both sides at tier-0**: NFKC → casefold → `fold_diacritics` → collapse internal whitespace. Apply to both the cataloguer literal and to the SPARQL `?label` value (via `LCASE(STR(?label))` plus a Python-side fold of the diacritics list — Fuseki doesn't have a `fold` builtin, so do the candidate fold once at load time and store an extra `bffi:foldedLabel` literal alongside each `skos:prefLabel` in the local authority graph).
- **Strip cataloguer-side decoration** before tier-0 lookup:
  - Trailing parenthetical dates: `Tolkien, J. R. R. (1892-1973)` → `Tolkien, J. R. R.`
  - MARC role markers in subfield `$e`: `Hamilton, Guy, ohjaaja` → `Hamilton, Guy`
  - `(fiktiivinen hahmo)` / `(fiktiv gestalt)` qualifiers stay routed to the existing `fictional_character` marker path — no change.
- **Also match `skos:altLabel` and `skosxl:altLabel`** at tier-0, not just `skos:prefLabel`. KAUNO and MUSO especially carry a lot of cataloguer-facing variants under altLabel.
- **Quality audit gate** before Phase C ships: sample 200 tier-0-promoted hits from the 5k run, manually verify that each new normalisation rule lands on the correct authority URI. A false-positive merge here pollutes the canonical graph silently, so the audit is the rollback signal. The 200-sample is added to the gold-set on completion (feeds into the P-06 backlog).
- Acceptance: 5k re-run shows the tier-0 hit count up by ≥ 30 % of the previous tier-2 count, and the sample audit finds zero new false-positive bindings. (Some shift from tier-1 to tier-0 is also expected and is a win — same correctness, same speed at this stage's resolution, but it makes tier-1 a smaller surface to maintain.)

## Prerequisites

- The 2026-05-12 5k baseline is the comparison point. The 5k sample (`data/sample-5k-marcxml/`) and the recorded Fuseki state at run start are reproducible.
- mlx-lm 8B server still running at `127.0.0.1:8001` per the P-02 final config (already the case).
- No active plan touches `reconcile.py` or `local_concept_resolver.py` (checked at `docs/plans/in-progress/` — empty besides the README).
- Phase C's `bffi:foldedLabel` materialisation needs `bffi-pipeline load-finto` to learn a `--fold-prefLabels` flag; that's a small extension, not a new stage.

## Risks

- **Phase A — Thread safety of `LangChainLLMPicker`**: M6's `JudgeCache` already had a SQLite cross-thread bug fixed in `1452a4f` (see the 5k snapshot). Phase A's PR re-validates the picker on the same axis before flipping the default.
- **Phase B — Cache key brittleness**: any byte change in the picker prompt or the candidate ordering invalidates the cache. The key already includes `prompt_hash`; candidate URIs are sorted to make ordering deterministic. A second risk is *over*-caching — if Finto adds a new authority and we keep returning the cached "no match" verdict from before the update. Mitigation: store the `sha256` of `data/finto-dumps/<vocab>.ttl` per cache row; mismatch → cache miss. Finto's actual cadence (see Phase B table) means the daily YSO/KANTO refresh is the realistic invalidation rhythm, and KAUNO/MUSO go months without touch — so the file-hash check fires rarely and refreshes cleanly when the operator chooses to.
- **Phase C — Silent false-positive merges**: aggressive normalisation could bind a literal to the wrong URI (e.g. two distinct authors collapsed onto one KANTO record because their normalised names match). The sample audit is the primary gate; on top of that, the new bindings carry `bffi:descriptionAuthentication = <bib:auth/needs-review>` if `(fold(literal) == fold(prefLabel)) && (literal != prefLabel)` so cataloguers can audit the imperfect matches in Skosmos.
- **Phase C — `skos:altLabel` ambiguity**: a single altLabel may be shared across multiple authority URIs (e.g. two YSO concepts both carrying "Helsinki" as alt). At tier-0, multiple-hit means we *cannot* commit deterministically and must fall through to tier-1/2 (already the behaviour in `local_concept_resolver.py`'s "skipped when no `local_resolver` is wired" branch). The tier-0 expansion must preserve this — never bind when the match count > 1.
- **Phase A+B interaction**: the cache should be looked up *before* the concurrent dispatch, otherwise N threads can pay for the same uncached decision before any of them writes. Lookup → dispatch → write atomically; or use the same `BEGIN IMMEDIATE` pattern the judge cache uses.

## Open questions

- **Is the steady-state cache hit rate on the full 800k actually high?** Phase B's value depends on author/subject literal repetition across the corpus. A pre-Phase-B `bffi-pipeline reconcile --dry-run --report-key-frequencies` over the 5k could project the 800k hit rate from the literal-repetition distribution. Lean ship-Phase-B-anyway: the hit rate has to be quite low (< 20 %) to make the persistent cache not worth it, and that scenario is implausible for authority data.
- **Should Phase C tier-0 expansion run a parallel "shadow tier-2" for the first 1k records?** I.e. promote to tier-0 *and* still run the picker, compare verdicts, log disagreement. Gives empirical evidence for the false-positive risk on real data instead of relying on a manual audit. Cost: one full M9 run paid twice. Worth it if the audit surfaces any disagreement at all; skip if the first audit comes back clean.
- **Counterpoint — what if 800k M9 is fine on the production M5 Max 128 GB?** The 5 722 s figure is M2 Max 64 GB; M5 Max with `mlx-lm` and richer prefix-cache config is meaningfully faster. Even at a generous 2× speedup, though, M9 still extrapolates to 5 days on the full corpus — not overnight. So P-10 holds regardless.
- **Deferred Phase D — batched picker**: hand N fields per call, N decisions returned. Captures the prompt-overhead amortisation that batched M6 saw in P-02. Quality risk (longer prompt context degrades the per-decision care the picker takes), so deferred until Phases A–C are measured and we know whether further reduction is needed. Belongs in a follow-up plan if A+B+C don't close the overnight gap.
- **Deferred Phase E — prompt ordering for mlx-lm prefix-cache stickiness**: order picker calls by candidate-vocabulary so consecutive calls share more prefix bytes. P-02's `--prompt-cache-size 200 --prompt-cache-bytes 1073741824` is already on; this lever is "make the cache hit-rate higher within the run." Smaller wins than A/B/C; defer until they're measured.

## Cross-references

- [`docs/performance/2026-05-12-5k-m2-max.md`](../performance/2026-05-12-5k-m2-max.md) — the baseline P-10 measures itself against. The "Where the time goes" chart is the headline picture.
- [`src/bffi_pipeline/stages/reconcile.py`](../../src/bffi_pipeline/stages/reconcile.py) — `apply_reconciliation` orchestrator (Phase A) and `LangChainLLMPicker` call site (Phase B).
- [`src/bffi_pipeline/stages/local_concept_resolver.py`](../../src/bffi_pipeline/stages/local_concept_resolver.py) — tier-0 SPARQL (Phase C).
- [`src/bffi_pipeline/cli.py:767`](../../src/bffi_pipeline/cli.py) — `reconcile_command` signature, where `--concurrency` lands.
- [`prompts/picker_v1.txt`](../../prompts/picker_v1.txt) — picker prompt whose hash anchors Phase B's cache key.
- [`docs/plans/completed/p-02-inference-stack-tuning.md`](../plans/completed/p-02-inference-stack-tuning.md) — the prefix-cache + concurrency lever set P-10 reuses on the M9 side.
- [`docs/plans/backlog/p-06-gold-set-growth.md`](../plans/backlog/p-06-gold-set-growth.md) — Phase C's 200-sample audit feeds into the gold-set backlog.
- M6 cache parallel: `data/judge-cache.sqlite` schema + the cross-thread fix in `1452a4f` — the model Phase B replicates for M9.
- [`NatLibFi/Finto-data`](https://github.com/NatLibFi/Finto-data) on GitHub — the Finto-vocabulary source repository whose commit history backs the Phase B cadence table. Per-vocabulary cadence via `git log -- vocabularies/<name>/`.
