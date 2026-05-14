# P-19 verification — M8 corpus-load throughput, 20 k bench, 2026-05-14

Re-bench of the 2026-05-13 overnight 20 k sample
(`scratchpad/overnight-sample-2026-05-13/`) after shipping P-19's two
phases:

- **Phase A** (`5148746`) — write `bffi-corpus.ttl` at M3
  finalisation; M8 reads the concat via fast-path when fresh.
- **Phase B** (this session) — remove the vestigial BIBFRAME walk
  from `_load_work_records_from_corpus`. M3's CONSTRUCT already
  preserves every predicate `extract_work_metadata` reads into the
  BFFI Turtle, so the bibframe/*.rdf walk added no information.

## Run metadata

| Field | Value |
|---|---|
| Date | 2026-05-14 |
| Hardware | MacBook Pro, M5 Max, 128 GB unified memory |
| Sample | `scratchpad/overnight-sample-2026-05-13/` — 19 570 records (BIBFRAME + BFFI Turtle frozen from the original overnight bench) |
| Git HEADs | `5148746` (P-19 Phase A) for the "Phase A" row; the Phase B row uses the post-Phase-B code |
| M8 command | `BFFI_DATA_DIR=scratchpad/overnight-sample-2026-05-13 BFFI_RUN_UUID=<rebench-uuid> uv run bffi-pipeline merge` |

## Headline numbers

| Configuration | Corpus-load wall | Emit-loop wall | Total M8 wall | Speedup vs original |
|---|---:|---:|---:|---:|
| **Pre-P-19** (2026-05-13 overnight bench, original `start`-event-late behaviour) | ~460 s | ~47 s | ~508 s | 1.0x |
| **P-19 Phase A only** — BFFI concat fast-path + BIBFRAME walk still in place (`p19-rebench-2026-05-14` run_uuid) | 315 s | 48 s | 363 s | 1.5x |
| **P-19 Phase A + B** — BFFI concat fast-path, BIBFRAME walk removed (`p19-phaseB-rebench` run_uuid) | **18 s** | 33 s | **51 s** | **25x** |

AC threshold from `p-19-m8-corpus-load-throughput.md`: corpus-load
< 90 s. **Met decisively** at 18 s.

## Where the time was actually going

Once Phase A was in production the residual 315 s was suspicious —
the proposal projected 30-60 s for a single `Graph().parse()` on a
600 MB Turtle. Timing the two halves of `_load_work_records_from_corpus`
independently:

```
BFFI concat parse:    14.7 s (1,144,204 triples)
BIBFRAME walk (19572 files): 290.9 s (4,364,029 triples)
```

The BIBFRAME walk dominated by 20x. The proposal had asserted "only
M8 reads the BFFI corpus" — true for the **canonical** load, but
**not** what the code actually did. `_load_work_records_from_corpus`
walked both halves, building one combined `rdflib.Graph` before
calling `extract_work_metadata`. The proposal's open question
("Should M2 also emit a concatenated bibframe-corpus.rdf?") had
been answered "probably no — only M8 reads the BFFI corpus"; the
correct answer was actually "M8 reads BIBFRAME too, but it
doesn't need to."

## Proving the BIBFRAME walk is dead weight

Sub-experiment: rename `bibframe/` aside, re-run M8 unchanged,
compare outputs:

```
diff <(sort .pre-p19/canonical-map.jsonl) <(sort canonical-map.jsonl)
# (ignoring run-time merged_at timestamp)
# → identical: 16,652 rows match exactly
```

Same 437 same_work / 905 different_work / 2 563 conflict groups
as the original 2026-05-13 overnight bench. Output is byte-equal
modulo the run-time `merged_at` timestamp.

Why this works: M3's CONSTRUCT (in `sparql/bf_to_bffi_work.rq` and
related) preserves every predicate `extract_work_metadata` reads
(`bf:identifiedBy`, `bf:source`, `bf:role`, plus the `bffi:*`
triples) into the per-record BFFI Turtle. P-15 (the
authority-URI-preservation plan) was the last piece that made this
true; before P-15, the BIBFRAME walk was carrying authority URIs
M3 dropped. Post-P-15 the BIBFRAME walk was vestigial.

Phase B is the one-loop deletion that makes this explicit. New unit
test `test_p19_load_work_records_ignores_bibframe_dir` pins the
behaviour: a `bibframe/poison.rdf` containing invalid RDF/XML must
not be read (would crash the parser); the test passes iff the
loader ignores the dir.

## Full-corpus extrapolation

The 20 k bench → 800 k corpus is a 40.9x scale-up. Linear
extrapolation:

| Phase | 20 k wall | 800 k linear projection |
|---|---:|---:|
| BFFI concat parse | 14.7 s | ~10 min |
| Emit loop | 33 s | ~22 min |
| **Total M8 wall** | **51 s** | **~35 min** |

vs the pre-P-19 projection of ~5.5 h, which mirrored the bench's 8 min
wall scaled 40x — that's the "5.5 h just to load M8's input" number
P-18's motivation table cited. P-19 Phase A + B compresses that into
~10 min of single-file parse.

`Graph().parse()` on Turtle scales sub-linearly (parser
initialisation is per-graph, not per-triple), so the actual 800 k
load is probably 8-12 min wall. Confirms the P-10 "under one
overnight window" target with significant margin.

Disk-footprint headline: the bench produced a 53 MB
`bffi-corpus.ttl` for 19 570 records (~2.8 KB/record). 800 k
records project to ~2.1 GB. Tractable on modern SSDs; well within
rdflib's single-graph parse capacity.

## Implications

1. **P-19 ships in full.** Definition of done is green; plan moves
   to `completed/`.
2. **BIBFRAME files stay canonical artefacts** for M3 (the M3 SPARQL
   CONSTRUCT reads them) and as on-disk audit trail (operator
   inspection of a single record's BIBFRAME). M8 no longer reads
   them — that's the only behavioural change.
3. **No follow-up needed.** The proposal's deferred Phase C
   ("bespoke streaming parser" Option C from the proposal) is
   unnecessary: at 14.7 s for the concat parse and ~10-12 min
   projected for the full corpus, there's no win left to chase
   that justifies hand-rolling a Turtle parser. Mark Phase C as
   not-needed in the plan.

## What's NOT in this snapshot

- **Dashboard smoke tests** for P-17 and P-18 — those are visual
  checks (Grafana panels populating, M8 tile transitioning
  `pending` → `running`) and stay operator-side. The stage-events
  evidence below confirms P-18's lifecycle ordering held through
  the re-bench:

  ```
  m8 start          : 2026-05-14T05:36:39Z  (no counters)
  m8 phase_boundary : 2026-05-14T05:41:54Z  (phase=emit, total=19215)
  m8 end            : 2026-05-14T05:42:42Z
  ```

  Start fired first, no counters; phase_boundary carried the
  total once union-find finished. Mirror of the spec.

- **Full pipeline re-run** through M9 / Skosify / Load. Out of
  scope; P-19 is M8-load-specific. M9 and downstream stages
  operate on M8's output and are unaffected.

- **Sub-90 s benchmark on the 800 k corpus.** Not run; the 20 k
  bench at 18 s is the AC fixture per the plan. Full-corpus
  validation is a separate operational step when the 800 k run
  is scheduled.
