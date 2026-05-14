# P-19 — M8 BFFI-corpus load throughput: collapse 19 570 file opens into one stream

**Status**: in-progress (started 2026-05-14).
**Source proposal**: `prop-19-m8-corpus-load-throughput` (deleted on graduation; recover via `git show 9a0601d:docs/plans/proposed/prop-19-m8-corpus-load-throughput.md`).
**Plan-base commit**: `18f53bf`. To gauge drift before re-executing or backporting, run
`git diff 18f53bf..HEAD --
src/bffi_pipeline/stages/merge.py
src/bffi_pipeline/stages/bf_to_bffi.py`.
**Phase commits**:

- Phase A (Option A — `bffi-corpus.ttl` concat at M3 finalisation + M8 fast-path read + mtime-based fallback + 3 unit tests): `5148746` (code, 2026-05-14). Bundled with P-18 Phase A.
- Phase B (20 k bench wall-time validation): `<unfilled>` — operator-side bench re-run on the M5 Max.
- Phase C (Option C — bespoke streaming parser): `<unfilled>` — re-evaluate only if Phase B's win isn't enough at full corpus.

**Owner**: shipped this session.
**Estimated wall-time**: 1-2 days per the proposal. Actual for Phase A code: ~1 h including tests.

## Goal

The 2026-05-13 20 k overnight bench measured M8's BFFI-corpus load as **~8 minutes wall** before M8 could emit its first event. Cost decomposes as:

- 19 570 separate `open(<bib>.ttl)` syscalls
- 19 570 rdflib `Graph().parse(format="turtle")` invocations
- 19 570 `_extract_work_record(graph)` walks

Linear-extrapolating to the 800 k full corpus: **~5.5 hours just to load M8's input**. That's a meaningful chunk of the P-10 "under one overnight window" target.

The slowdown is dominated by **per-file overhead**, not by graph size. The total BFFI corpus is ~600 MB serialised — rdflib parses a single 600 MB Turtle file in 30-60 s. The 100× difference between "many small files" and "one stream" is the open / parser-init / dataclass-construction cost per file.

The file-per-record layout is *deliberate* — it makes M2/M3 idempotent, lets the operator re-process a single record, and keeps per-record provenance debuggable. **The fix is to layer a stream representation on top of the file-per-record store**, not to replace it.

Approach: write `<BFFI_DATA_DIR>/bffi-corpus.ttl` at M3 finalisation (the concat is a derived view; per-record `.ttl` files stay canonical). M8 reads the concat via fast-path when its mtime is at least as new as every per-record `.ttl`; otherwise falls back to the per-record walk (partial-rerun safety net).

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
- [x] `make lint && make test` green.
- [ ] **Phase B — 20 k bench re-run**: M8 load wall drops from ~8 min to <90 s. Operator action; requires a full pipeline re-run on `scratchpad/overnight-sample-2026-05-13/` (or a fresh equivalent bench) on the M5 Max.
- [ ] No regression on the cataloguer-audit's 19-record bench (small N where the speedup is marginal but no regression should land). Operator action.
- [ ] Full-corpus extrapolation in a `docs/performance/<date>-m8-corpus-load.md` snapshot projects the new M8 wall + revised overnight budget.

## What shipped at 5148746

- New helper `_write_bffi_corpus(bffi_dir, corpus_path)` in `src/bffi_pipeline/stages/bf_to_bffi.py`: walks `bffi/*.ttl`, deduplicates `@prefix` + `@base` declarations, writes the concat atomically. Idempotent: skips when the existing concat's mtime is ≥ every per-record `.ttl` mtime.
- Wire-in at the end of M3's `run()`, before the `end` event emit.
- M8's `_load_work_records_from_corpus` now branches on `bffi-corpus.ttl` mtime: fast-path reads the concat once; slow-path walks per-record .ttl files (partial-rerun safety).

## Risks (residual)

- **R1 — concat file goes stale silently**. Mitigated by the M8 mtime check + per-record fallback. Operator-side `make clean-caches`-style escape hatch already exists (force re-write any output by touching its input).
- **R2 — disk space cost**. ~600 MB extra at full corpus. Trivially small at modern disk sizes.
- **R3 — prefix-declaration dedup edge cases**. Tested against two per-record files with identical prefix declarations; the test pins single-occurrence in the concat and successful rdflib parse.
- **R4 — broken-corpus failure mode**. Atomic `.tmp` then rename. Mid-write crash leaves the previous concat intact.

## What this plan does NOT do (deferred)

- **Option B (multiprocessing pool)**: dropped. The bottleneck is per-file overhead, not CPU; the proposal's analysis concluded multiprocessing helps less than concatenation.
- **Option C (bespoke streaming parser for the BFFI subset)**: deferred to Phase C. Re-evaluate only if Phase B's bench shows Phase A's win isn't enough at full corpus scale.
- **Option D (status quo)**: rejected. 5.5 h is half of a single-night M9 budget.
- **M2 concat (`bibframe-corpus.rdf`)**: not in scope. Only M8 reads the BFFI corpus; M3 reads per-record BIBFRAME files but M3's wall is dominated by SPARQL CONSTRUCT cost, not file-open overhead.
- **`bf_to_bffi` emitting a corpus-records counter**: not added. The concat write is silent in the M3 stage-events stream; the per-record `processed` / `converted` counters tell the operator what mattered. If a "concat write took N seconds" gauge becomes important, add it then.
