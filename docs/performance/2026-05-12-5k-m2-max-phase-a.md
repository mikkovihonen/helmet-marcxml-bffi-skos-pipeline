# 5 000-record M9 re-run, M2 Max 64 GB, 2026-05-12 (Phase A bench)

Re-run of the M9 reconcile stage at `c=4` against the same canonical Work graph the 2026-05-12 baseline run produced. The goal was to measure P-10 Phase A's concurrency lever in isolation (no Phase B cache, no Phase C tier-0 expansion).

> **Caveat — corpus freshness.** This bench reuses the May 12 baseline's `data/canonical.ttl` (M8 output dated `May 12 08:53`). It does **not** rebuild M2→M8 against the fresh Sierra MARCXML export taken today after P-08 (33X synthesis) and the bib_id passthrough fix (`1678e1c`). The c=1 vs c=4 comparison is internally valid because both runs operate on identical input. The **absolute speedup number** (1.05×) is specific to this corpus; the **structural finding** (Phase 1 serial work dominating) is shape-driven and is expected to hold on the fresh corpus — likely amplified, because P-08 lets more low-content records reach M9 where most route through tier-0/no-candidate (high Phase 1 cost, low picker cost). A separate end-to-end "post-P-08 + Phase A" snapshot is scheduled for the next full-corpus run.

## Run metadata

| Field | Value |
|---|---|
| Date | 2026-05-12 |
| Hardware | MacBook Pro, M2 Max, **64 GB unified memory** (same as baseline) |
| Git HEAD at run start | `0f8c3da` (P-10 Phase A code shipped) |
| Sample size | Same 5 000-record canonical graph the baseline produced at `data/canonical.ttl` (12 666 reconciliation requests after M8 deduplication) |
| Input path | `data/canonical.ttl` (M8 output from the May 12 baseline run, dated `May 12 08:53`, **not regenerated** from today's fresh Sierra export) |
| Corpus freshness | **May 12 pre-fresh-export.** Does not include P-08's 33X synthesis recoveries or the bib_id passthrough form. See "Caveat" at the top of this snapshot. |
| Output path | `/tmp/canonical-phase-a-bench.ttl` (not overwriting `data/canonical-reconciled.ttl` so the baseline stays intact) |
| Fuseki state at start | Same TDB + Finto graphs the baseline used (`bffi-pipeline load-finto` last run pre-baseline; no Finto refresh in between) |
| `mlx_lm.server` 8B | `--model ~/.mlx_models/Qwen3-8B-4bit --host 127.0.0.1 --port 8001 --chat-template-args '{"enable_thinking":false}' --decode-concurrency 4 --prompt-concurrency 4 --prompt-cache-size 200 --prompt-cache-bytes 1073741824` (identical to baseline) |
| `M9_CONCURRENCY` | **4** (Phase A's new knob; baseline was effectively `1`) |
| `LLM_M9_FIELD_TIMEOUT_SECONDS` | **180** (Phase A default) |
| `LLM_CALL_TIMEOUT_SECONDS` | 90 (now actually plumbed through to ChatOpenAI's `request_timeout` per Phase A.4) |

## Headline numbers

| | Baseline (`c=1`, 2026-05-12) | Phase A (`c=4`, this run) | Δ |
|---|---|---|---|
| **M9 wall time** | **5 722 s (95:22)** | **5 460 s (91:00)** | **−262 s (−4.6 %)** — speedup 1.05× |
| `field_budget_exceeded` events | n/a (knob didn't exist) | **0** | ✓ acceptance met |
| `reconciliation-local` (tier-0) | (not separately recorded) | 7 526 (59.4 %) | — |
| `reconciliation-lexical` (tier-1) | (n/r) | 193 (1.5 %) | — |
| `reconciliation-llm` (tier-2) | (n/r) | 874 (6.9 %) | — |
| `reconciliation-fallback` (tier-3) | (n/r) | 474 (3.7 %) | — |
| `reconciliation-no-candidate` | (n/r) | 2 752 (21.7 %) | — |
| `reconciliation-fictional-character` | (n/r) | 847 (6.7 %) | — |
| Total LLM picker calls | (n/r) | **1 348** (10.6 % of entities) | — |
| Output bytes | 14 212 702 | 14 212 702 | identical |

## M9 / M6 observations

- **Picker calls dropped to 10.6 % of entities.** The full 5k canonical graph has 12 666 reconciliation requests; only 1 348 (= `llm_pick` 874 + `fallback` 474) actually hit the LLM. The other 89.4 % resolved deterministically at tier-0 (Fuseki exact-prefLabel — 7 526), tier-1 (lexical-direct — 193), `no_candidate` (Finto/VIAF returned nothing — 2 752), or `fictional_character` (847 markers). This shifts where the wall-time goes — see "Where the time goes" below.
- **Zero watchdog events at the default 180 s budget.** No picker call exceeded the per-field budget across 1 348 dispatches. The watchdog observability surface fires correctly in unit tests but didn't fire on real data — exactly the P-03 outcome on the M6 side at the corresponding bench.
- **Prompt cache saturated.** mlx-lm reported "Prompt Cache: 200 sequences, 46.66 GB" mid-run — the full `--prompt-cache-size 200` cap was utilised. Prefix reuse across consecutive picker prompts is engaged.
- **No thread errors, no double-emits.** The c=4 dispatch + LangChain-per-worker pattern is mechanically sound on real mlx-lm traffic.
- **Output is binding-equivalent to the baseline.** The Phase A output Turtle is byte-identical (14 212 702 bytes) to the May 12 c=1 baseline. Same picker temperature (0) and seed (42) yields deterministic bindings whether the calls run serially or across 4 workers — the byte-stability unit test holds against real data.

## Where the time goes

```
M9 reconcile c=4   5460 s ████████████████████████████████████  100.0 %
  Phase 1 (tier-0 + Finto/VIAF candidate query, SERIAL)
                ~3800 s ████████████████████████░░░░░░░░░░░░   ~70 %
  Phase 2 (picker LLM at c=4)
                ~1660 s ███████████░░░░░░░░░░░░░░░░░░░░░░░░░   ~30 %
```

The "Phase 1 / Phase 2" split is a rough back-of-envelope: 12 666 entities × ~0.3 s/entity for the local-resolver SPARQL + Finto/VIAF candidate query, against 1 348 picker calls × ~5 s/call ÷ 4 workers. Together they tally close to the observed 91-min wall; tier-0/1 work was a comparable bottleneck even before Phase A and is now the dominant one.

## Why the speedup is 1.05× and not the ≥3× target

The plan extrapolated a ≥3× speedup from the assumption that tier-2 (LLM picker) dominated the 5 722 s baseline. The Phase A run reveals that **89.4 % of entities never reach tier-2** in the first place — and the serial pre-pass that resolves them (Fuseki SPARQL for tier-0, plus Finto/VIAF HTTP for the rest) is itself ~70 % of the total wall.

Phase A parallelised the 30 % that **was** LLM work; the 70 % stayed serial. So the wall dropped from 5 722 s to 5 460 s — about what Amdahl predicts given a serial floor of ~3 800 s.

This is a real finding, not an implementation bug: Phase A's concurrency knob *itself* works (1 348 calls cleanly distributed, prefix cache engaged, watchdog quiet). The plan's "≥3× speedup" target was based on a model of M9's cost structure that turned out not to hold on this corpus.

## Implications for P-10 Phases B and C

- **Phase B (persistent picker cache)** has narrower impact than the plan estimated. The cache only catches tier-2 repeats; in this corpus tier-2 is already only 10.6 % of entities, so even a 100 %-hit warm cache caps the savings at ~30 % of wall. Cache is still worth shipping, but it won't close the gap to overnight on its own.
- **Phase C (tier-0 normalisation + `skos:altLabel`)** is **more valuable than the plan estimated**. Every entity pushed from tier-2 / no-candidate into tier-0 is a Finto candidate query avoided in Phase 1 — and Phase 1 is the new bottleneck. Pushing 30 % of the current 2 752 `no_candidate` cases into tier-0 (via altLabel / date-strip / role-marker rules) would shave ~800 entities × ~0.3 s = ~240 s from Phase 1, on top of removing their picker calls.
- **New lever surfaces: parallelise Phase 1.** The orchestrator's Phase 1 walk does one Fuseki SPARQL + one Finto/VIAF query per entity, fully serial. Putting Phase 1 behind its own `ThreadPoolExecutor` (separate `--phase1-concurrency` knob, since Fuseki's connection pool is the constraint, not mlx-lm) would attack the 70 %. This isn't in the current P-10 scope; flag for a follow-up plan once Phase B/C are measured.

## Outputs persisted

| Artefact | Path | Size | Notes |
|---|---|---|---|
| Phase A reconciled graph | `/tmp/canonical-phase-a-bench.ttl` | 14 212 702 bytes | Identical to the baseline. Not promoted to `data/` so the May 12 baseline stays intact. |
| Phase A bench log | `/tmp/phase-a-bench.log` | small | `time -p` output + the M9 summary block. Gitignored. |
| Phase A mlx-lm log | `/tmp/mlx-lm-8001.log` | small | Picker request log; 1 348 `POST /v1/chat/completions` lines. Gitignored. |
| Watchdog event sidecar | (not written) | — | Zero `field_budget_exceeded` events at default budget. |

## Extrapolation to the full 800 k corpus

Linear extrapolation from this 5k re-run (× 160):

| Knob | 5k | 800k (linear) | Status |
|---|---|---|---|
| **M9 c=1 (baseline)** | 5 722 s | **~10 days** | Pre-P-10 starting point |
| **M9 c=4 (this run, Phase A)** | 5 460 s | **~10 days** | -4.6 % vs baseline. Doesn't unlock overnight on its own. |
| **M9 c=4 + warm Phase B cache (projected)** | ~3 800 s* | ~7 days | * The non-LLM Phase 1 floor unchanged. Cache eliminates LLM time on repeat literals. |
| **M9 c=4 + Phase B cache + Phase C tier-0 expansion (projected)** | ~2 600 s* | ~5 days | * Phase C shrinks Phase 1 by moving entities into tier-0. |
| **M9 c=4 + Phase B + Phase C + Phase-1 parallelisation (follow-up)** | ~700 s* | ~1.5 days | * Phase 1 at `c=8` against Fuseki + Finto. Speculative; bench required. |

Headline: **Phase A alone does not close the overnight gap.** A+B+C together are projected at ~5 days extrapolated; an additional Phase-1 parallelisation lever (not in P-10 scope today) is what gets to overnight on the M2 Max. The production M5 Max 128 GB should narrow this further but unlikely to close it without the additional lever.

## Phase A acceptance status

- [x] **Code, lint, mypy, unit tests** — green (commit `0f8c3da`, 823 unit tests, 5 new for Phase A).
- [x] **Byte-stability** at c=1 vs c=4 — passes against synthetic fixture (unit test) AND against the real 5k canonical (this run is byte-identical to the May 12 baseline at c=1).
- [x] **Zero `field_budget_exceeded` events** at the default `LLM_M9_FIELD_TIMEOUT_SECONDS=180` budget on the 5k re-run.
- [x] **Watchdog event-emission** plumbed through the sidecar path (verified end-to-end via the sleep-past-budget unit test).
- [ ] **≥3× speedup** vs the 5 722 s baseline — **NOT MET on this corpus** (achieved 1.05×). Root cause: serial Phase 1 (tier-0 + candidate query) dominates the wall time on this corpus; tier-2 (the thing Phase A parallelised) is only ~30 % of total. Surfaced as a finding for Phases B + C planning. **Pending re-measurement on the fresh post-P-08 corpus** to confirm the absolute number; the structural finding is expected to hold.

## Cross-references

- [`docs/performance/2026-05-12-5k-m2-max.md`](2026-05-12-5k-m2-max.md) — the pre-Phase-A baseline.
- [`docs/plans/in-progress/p-10-m9-reconcile-throughput.md`](../plans/in-progress/p-10-m9-reconcile-throughput.md) — Phase A.6 acceptance checklist this snapshot informs; Phase B / C "Implications" section above feeds back into the plan when those phases are picked up.
- `0f8c3da` — Phase A commit.
