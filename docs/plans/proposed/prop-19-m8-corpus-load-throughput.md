# P-19 — M8 BFFI-corpus load throughput: collapse 19 570 file opens into one stream

**Status**: proposed.
**Scope**: 1-2 days. Surface options + verification benches + pick one approach + ship the fix.
**Proposal-base commit**: `99d8152`. To gauge drift before acting, run
`git diff 99d8152..HEAD --
src/bffi_pipeline/stages/merge.py
src/bffi_pipeline/stages/bf_to_bffi.py`.

## Motivation

The 2026-05-13 20 k-record overnight bench measured M8's BFFI-corpus load as **~8 minutes wall** before M8 could emit its first event (see [prop-18](prop-18-m8-emit-start-before-corpus-load.md) for the dashboard symptom). The cost decomposes as:

- 19 570 separate ``open(<bib>.ttl)`` syscalls
- 19 570 rdflib ``Graph().parse(format="turtle")`` invocations
- 19 570 ``_extract_work_record(graph)`` walks, each over a ~50-triple subgraph

Linear-extrapolating to the 800 k full corpus: **~5.5 hours just to load M8's input** before any merge work happens. On the same M2 Max with a warm OS file cache. That's a meaningful chunk of the P-10 "under one overnight window" target.

The slowdown is dominated by **per-file overhead**, not by graph size. The total BFFI corpus is ~600 MB serialised — rdflib can parse a single 600 MB Turtle file in 30-60 seconds, depending on the dataset's shape. The 100× difference between "many small files" and "one stream" is the open / parser-init / dataclass-construction cost per file.

The file-per-record layout is *deliberate* — it makes M2/M3 idempotent, lets the operator re-process a single record, and keeps per-record provenance debuggable. **The fix is to layer a stream representation on top of the file-per-record store**, not to replace it.

## Approach — four candidates

Each has a wall-time / complexity / side-effect trade-off worth measuring against a 20 k bench before committing.

### A. Concatenated corpus file written at M3 finalisation

After M3 writes the 19 570 per-record ``.ttl`` files, append a `_corpus.ttl` artefact in the same directory: every per-record Turtle concatenated, deduplicating prefix declarations. M8 reads only `_corpus.ttl`.

**Pros**:
- Single ``Graph().parse()`` call inside M8. Expected wall: 30-60 s vs 8 min on 20 k → **8-16× speedup**.
- File-per-record still on disk for inspection / debugging / partial-resume.
- No M8 code change beyond switching the input path.

**Cons**:
- M3 has to (a) serialise the concatenation single-threaded after the parallel per-record write completes, or (b) maintain an append-only stream throughout. Option (b) breaks M3's parallelism (worker threads can't safely interleave appends without ordering or locking). Option (a) adds ~30-60 s to M3 wall.
- The concat file goes stale if M3 re-runs a single record. Cache invalidation pattern: mtime of `_corpus.ttl` vs newest per-record `.ttl`; rebuild if any per-record file is newer.

### B. Multi-process Turtle parse pool

Spawn a multiprocessing pool of workers; each parses a subset of the per-record files, producing partial ``dict[str, WorkRecord]`` results. The main process merges the dicts.

**Pros**:
- No on-disk artefact change.
- Scales with CPU count. On the M2 Max (12 cores) expected wall: ~1-2 min.

**Cons**:
- Process startup overhead (~2-3 s per worker — eats into the gain on small corpora).
- Pickling ``WorkRecord`` (a frozen dataclass) is cheap but pickling 800 k of them across process boundaries adds memory pressure and copy cost.
- More code, more failure modes (worker death, partial results, etc.).
- The actual bottleneck is *per-file open + parse init*, not CPU. Workers help but not as much as a single concatenated file would.

### C. Bespoke streaming parser for the BFFI subset

M8 only reads ~5 predicates per ``bffi:Work`` (``bf:identifiedBy``, ``bffi:contribution``, ``skos:prefLabel``, ``rdfs:label``, ``bffi:hasExpression``). Instead of rdflib's general-purpose Turtle parser, write a small regex- or hand-rolled parser that streams over `_corpus.ttl` (or each per-record file) and extracts only those predicates into a ``WorkRecord``.

**Pros**:
- Fastest possible — measured rdflib-vs-handrolled on Turtle subsets has shown 10-30× speedups for predicate-specific extractors.
- Composes with Option A (concatenated file) for a combined 50-100× win on the full corpus.

**Cons**:
- Maintenance cost — the parser has to keep up with BFFI ontology changes that affect M8's predicates.
- Subtle Turtle features (blank nodes spanning lines, literal-with-newlines, language tags on prefLabel) need careful handling.
- Probably overkill for the current scale; revisit only if Options A+B together aren't enough.

### D. Status quo + accept the wall-time

Document the ~5.5 h load phase as the M8 floor on the 800 k corpus. Operator builds it into the overnight schedule. No code change.

**Pros**: zero work.
**Cons**: 5.5 h is 1/2 of a single-night M9 budget. Worth real engineering effort to recover.

## Recommendation

Ship **Option A** as Phase A of the eventual plan. It's the smallest code surface, the largest single-step win, and doesn't preclude C as a follow-up. Re-measure on the 20 k bench; if M8 load drops from 8 min to <60 s, the full-corpus extrapolation goes from 5.5 h to ~40 min — well within the overnight budget without further work.

Phase B (Option C) becomes a possible follow-up if the operator runs the full corpus and Phase A's win isn't enough.

## Prerequisites

- A reproducible 20 k bench (the overnight-sample selection script from prop-17 / the existing ``scratchpad/overnight-sample-2026-05-13/`` directory).
- A clean wall-time measurement of the current M8 load — captured incidentally by tonight's run (8 min wall before first M8 event). Pin this as the "before" number.

## Risks

- **R1 — concat file goes stale silently.** If M3 re-runs a single record's ``.ttl`` but the concat doesn't get rebuilt, M8 reads outdated data. Mitigation: mtime check in M8 (``concat_mtime < newest_per_record_mtime`` → rebuild from scratch). Operator-side ``make clean-caches``-style escape hatch.
- **R2 — disk space cost.** The concat file roughly doubles M3's on-disk footprint (concat ~= sum of per-record). For 800 k records that's an extra ~600 MB. Mitigation: trivially small at modern disk sizes; document in the M3 stage's storage spec.
- **R3 — prefix-declaration dedup edge cases.** The per-record files declare prefixes with ``@prefix bffi: <...>`` headers. Naive concatenation produces 800 k duplicate prefix lines. rdflib tolerates redeclarations but slows the parse. Mitigation: M3's concat writer dedupes prefixes (write the header once, drop subsequent ``@prefix`` lines).
- **R4 — broken-corpus failure mode.** If M3 crashes mid-concat-write, the concat is truncated. M8 would either fail to parse or silently miss records. Mitigation: write to ``.tmp`` then atomic rename (pattern matches M3's existing per-record write).

## Open questions

- Should the concat file live in ``<BFFI_DATA_DIR>/bffi-corpus.ttl`` (alongside the per-record ``bffi/`` dir) or inside ``bffi/_corpus.ttl`` (co-located)? Probably the former — symmetric with ``canonical.ttl`` / ``canonical-reconciled.ttl`` at the same level.
- Does Option A change the operator's debugging workflow? The per-record files stay (per-record provenance and inspection) — the concat is a derived view. So no.
- Does P-08's `300$a extent regex fallback layer` work compose? The M3 cascade output (per-record ``.ttl``) is unchanged; the concat is just a view over those files. So yes.
- Should M2 also emit a concatenated `bibframe-corpus.rdf`? Probably no — only M8 reads the BFFI corpus; M3 reads per-record BIBFRAME files but M3's wall is dominated by SPARQL CONSTRUCT cost, not file-open overhead.

## Acceptance criteria (drafted; refine on graduation)

- [ ] ``M3.run()`` writes a ``<BFFI_DATA_DIR>/bffi-corpus.ttl`` atomically (``.tmp`` then rename) after the per-record write loop completes.
- [ ] Prefix declarations deduplicated in the concat (single ``@prefix`` block at the top, per-record blocks stripped).
- [ ] ``M8.run()`` reads ``bffi-corpus.ttl`` via a single ``Graph().parse()``, replacing the per-record glob walk.
- [ ] M8's mtime check: if ``bffi-corpus.ttl`` is older than the newest per-record ``.ttl``, fall back to the per-record walk (defensive — covers the partial-rerun case).
- [ ] 20 k bench re-run: M8 load wall drops from ~8 min to <90 s.
- [ ] No regression on the cataloguer-audit's 19-record bench (small N where the speedup is marginal but no regression should land).
- [ ] Full-corpus extrapolation in the snapshot's "Implications" section projects the new M8 wall + revised overnight budget.
- [ ] Fresh [`docs/performance/<date>-m8-corpus-load.md`](../../performance/) snapshot committed.

## What this proposal does NOT do

- Doesn't touch the per-record ``.ttl`` layout. File-per-record stays the canonical artefact; the concat is a derived view.
- Doesn't try to parallelise M8's union-find or graph-mutation phases. Those are downstream of the load and have their own perf characteristics.
- Doesn't change M3's per-record output format or the M3 SPARQL CONSTRUCTs.
- Doesn't reach into rdflib's parser internals (Option C) — that's a separate follow-up if Option A's win isn't enough at full scale.
