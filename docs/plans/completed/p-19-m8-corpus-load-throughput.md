# P-19 — M8 BFFI-corpus load throughput: collapse 19 570 file opens into one stream

**Status**: completed (2026-05-14).
**Source proposal**: `prop-19-m8-corpus-load-throughput` (deleted on graduation under the pre-2026-05-14 workflow; recover via `git show 9a0601d:docs/plans/proposed/prop-19-m8-corpus-load-throughput.md`).
**Plan-base commit**: `18f53bf`. To gauge drift before re-executing or backporting, run
`git diff 18f53bf..HEAD --
src/bffi_pipeline/stages/merge.py
src/bffi_pipeline/stages/bf_to_bffi.py`.
**Phase commits**:

- Phase A (Option A — `bffi-corpus.ttl` concat at M3 finalisation + M8 fast-path read + mtime-based fallback + 3 unit tests): `5148746` (code, 2026-05-14). Bundled with P-18 Phase A.
- Phase B (remove vestigial BIBFRAME walk from `_load_work_records_from_corpus` + new unit test + bench writeup): `<pending merge>` (this session's commit).
- Phase C (Option C — bespoke streaming parser): **not needed**. Phase A+B together reach 18 s corpus-load on the 20 k bench (decisively under the < 90 s AC); there's no remaining engineering budget to recover.

**Owner**: shipped this session.
**Estimated wall-time**: 1-2 days per the proposal. Actual: ~3 h across both phases including the surprise diagnosis + Phase B re-bench.

## Goal achieved

The 2026-05-13 20 k overnight bench measured M8's BFFI-corpus load as **~8 minutes wall** before M8 could emit its first event. Linear-extrapolating to the 800 k full corpus: **~5.5 hours just to load M8's input** — a meaningful chunk of the P-10 "under one overnight window" target.

Phase A (concat file + fast-path) brought the corpus-load wall from ~460 s to 315 s on the 20 k bench — a real but underwhelming 1.5x. Diagnosis revealed the proposal had missed a load-bearing detail: `_load_work_records_from_corpus` walked BOTH `bffi/*.ttl` AND `bibframe/*.rdf`, and the BIBFRAME walk (19 572 RDF/XML file opens, 290.9 s) dominated. Phase B removes the BIBFRAME walk after verifying its output is dead weight (P-15's authority-URI propagation made M3's BFFI Turtle self-sufficient; the BIBFRAME walk added no information to `extract_work_metadata`).

Phase A+B together: M8 corpus-load **18 s** on the 20 k bench → **~25x** speedup vs the original ~460 s. 800 k linear projection: ~12 min wall (`Graph().parse()` scales sub-linearly so realistic estimate is 8-12 min). AC threshold met with significant margin. Full snapshot: [`docs/performance/2026-05-14-m8-corpus-load.md`](../../performance/2026-05-14-m8-corpus-load.md).

## Definition of done

- [x] `bf_to_bffi.run()` writes `<BFFI_DATA_DIR>/bffi-corpus.ttl` atomically (`.tmp` then rename) after the per-record write loop completes. Helper: `_write_bffi_corpus(bffi_dir, corpus_path)`.
- [x] Prefix declarations deduplicated in the concat (single `@prefix` block at the top, per-record blocks stripped). Also handles `@base` for robustness.
- [x] `merge.py:_load_work_records_from_corpus` reads `bffi-corpus.ttl` via a single `Graph().parse()`, replacing the per-record glob walk on the fast path.
- [x] M8's mtime check: if `bffi-corpus.ttl` is older than the newest per-record `.ttl`, fall back to the per-record walk. Defensive — covers the partial-rerun case.
- [x] `BFFI_CORPUS_FILENAME` exported from `bf_to_bffi`; M8 holds its own `_BFFI_CORPUS_FILENAME` constant per CLAUDE.md "Stage isolation" rule. Values matched.
- [x] Unit test (`test_p19_write_bffi_corpus_concatenates_with_deduped_prefixes`): two per-record files concatenate with prefix declarations deduplicated; concat parses as valid Turtle.
- [x] Unit test (`test_p19_write_bffi_corpus_is_idempotent_when_fresh`): re-running with a fresh concat returns 0 without rewriting.
- [x] Unit test (`test_p19_load_work_records_uses_corpus_fast_path`): when concat is fresh, M8 reads it.
- [x] Unit test (`test_p19_load_work_records_falls_back_when_concat_stale`): when concat is older than a per-record file, M8 falls back without raising.
- [x] Unit test (`test_p19_load_work_records_ignores_bibframe_dir`): Phase B — a poison `bibframe/*.rdf` file that would crash rdflib's XML parser is silently ignored, proving the loader never opens it.
- [x] `make lint && make test` green (ruff + mypy strict + 967 pytest passed).
- [x] **20 k bench re-run**: M8 load wall drops from ~460 s to **18 s** (Phase A+B). `canonical-map.jsonl` byte-identical to the pre-P-19 baseline modulo run-time `merged_at` timestamps (16 652 / 437 / 905 / 2 563 counts match exactly).
- [x] Full-corpus extrapolation snapshot at [`docs/performance/2026-05-14-m8-corpus-load.md`](../../performance/2026-05-14-m8-corpus-load.md) projects the new M8 wall + revised overnight budget.

## What shipped

**Phase A** at `5148746`:
- `_write_bffi_corpus(bffi_dir, corpus_path)` in `src/bffi_pipeline/stages/bf_to_bffi.py`: walks `bffi/*.ttl`, deduplicates `@prefix` + `@base` declarations, writes the concat atomically. Idempotent: skips when the existing concat's mtime is ≥ every per-record `.ttl` mtime.
- Wire-in at the end of M3's `run()`, before the `end` event emit.
- M8's `_load_work_records_from_corpus` branches on `bffi-corpus.ttl` mtime: fast-path reads the concat once; slow-path walks per-record .ttl files (partial-rerun safety).

**Phase B** at `<pending merge>`:
- Deletes the `for path in bibframe_dir.glob("*.rdf"): g.parse(...)` loop in `_load_work_records_from_corpus`. M3's CONSTRUCT preserves every predicate `extract_work_metadata` reads (`bffi:Work` typing, `bf:identifiedBy`, `bf:source`, `bf:role`, plus the `bffi:*` triples) into the per-record BFFI Turtle, so the BIBFRAME walk was vestigial.
- New unit test pins the no-walk behaviour by placing a poison `bibframe/poison.rdf` containing invalid RDF/XML in the fixture; the loader must ignore it.
- Perf snapshot `docs/performance/2026-05-14-m8-corpus-load.md` records the 18 s corpus-load + the diagnostic experiment that proved output equivalence (16 652 / 437 / 905 / 2 563 counts match modulo `merged_at`).

## Risks (residual)

- **R1 — concat file goes stale silently**. Mitigated by the M8 mtime check + per-record fallback. Operator-side `make clean-caches`-style escape hatch (force re-write any output by touching its input).
- **R2 — disk space cost**. ~600 MB extra at full corpus (53 MB on the 20 k bench scaled 40x). Trivially small at modern disk sizes.
- **R3 — prefix-declaration dedup edge cases**. Tested against two per-record files with identical prefix declarations; the test pins single-occurrence in the concat and successful rdflib parse.
- **R4 — broken-corpus failure mode**. Atomic `.tmp` then rename. Mid-write crash leaves the previous concat intact.
- **R5 (post-mortem) — predicate drift**. If a future M3 refactor stops preserving some `bf:*` predicate that `extract_work_metadata` reads, the BIBFRAME-walk-absent loader will silently produce wrong canonical Works. Mitigation: the M3 → M8 contract is implicit; not test-pinned. A future P-28-style audit fixture or end-to-end integration test would catch drift. Out of scope here; flagged for follow-up.

## What this plan does NOT do (deferred)

- **Option B (multiprocessing pool)**: dropped. The proposal's analysis concluded multiprocessing helps less than concatenation; Phase A+B's 25x win makes B moot.
- **Option C (bespoke streaming parser for the BFFI subset)**: **dropped as not-needed**. Phase A+B reach 18 s on the 20 k bench (~12 min projected on 800 k corpus); there's no remaining win to chase.
- **Option D (status quo)**: rejected.
- **M2 concat (`bibframe-corpus.rdf`)**: dropped. M8 no longer reads BIBFRAME; M3 still walks per-record BIBFRAME files but M3's wall is dominated by SPARQL CONSTRUCT cost, not file-open overhead.
- **End-to-end M3 → M8 predicate-coverage contract test**: flagged in R5. Worth a follow-up plan if the M3 BFFI shape continues to evolve.

## Post-mortem note: how the proposal missed the BIBFRAME side

The proposal's measurement model was that M8's load was dominated by the BFFI Turtle walk (~600 MB serialised). The wall-time observation (~8 min on 20 k) was consistent with that model AT THE SCALE OF THE BENCH but didn't decompose into "BFFI walk vs BIBFRAME walk" — both halves walked the same number of files, so the proposal-time eyeball couldn't tell which dominated.

Phase A shipped, the re-bench surfaced 315 s (way over the < 90 s AC), and timing the two halves independently produced the 290 s / 14.7 s split that proved BIBFRAME was the load-bearing side. The diagnostic experiment (rename `bibframe/` aside, re-run M8, compare outputs) then proved BIBFRAME was DEAD WEIGHT, not just slow.

Generalisable lesson: when a proposal's "Approach" section reasons about ONE side of a multi-input function, instrument both sides at the verification step. The Phase A bench would have surfaced this immediately if the bench script had emitted per-load-step timing — which is a P-30-territory observability gap (no per-sub-stage timing inside M8's load).
