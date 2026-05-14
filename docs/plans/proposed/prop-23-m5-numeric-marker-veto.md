# P-23 — M5 numeric-marker veto: volume # + in-title year

**Status**: proposed.
**Scope**: 1-2 days.
**Proposal-base commit**: `6b6be25`. To gauge drift before acting, run
`git diff 6b6be25..HEAD -- src/bffi_pipeline/stages/embeddings.py src/bffi_pipeline/text/`.
**Source data**: `scratchpad/merge-cluster-verdicts/verdicts.jsonl`.

## Motivation

The merge-cluster audit on the 2026-05-13 overnight bench found **37
clusters (20.2 %)** where the M5 auto-merge band collapsed records
that differ on a *numeric marker* embedded in the title or subtitle:

- **`series_volumes_collapsed` — 34 clusters.** Two or more volumes of
  the same series merged as one canonical Work. Examples:
  - **b16833934 / b18868253** — Naruto Vol. 2 vs Vol. 11
  - **b18141390 / b18332250 / b22681541** — Fushigi Yuugi 3 vs 8 vs
    (no vol number)
  - **b1605748x / b16057818 / b16057843 / b16057958** —
    Kirkonarkistot ES 284 / 295 / 298 / 330 (volume marker in 245$b)
  - **b11475304 / b11476424 / b11476710** — Bach: Das Kantatenwerk
    Vol. 21 / 26 / 30
- **`annual_series_collapsed` — 3 clusters.** Year-specific titles
  merged year-over-year:
  - **b17035612 / b21940642** — Gary Moore: Live at Montreux **1990**
    vs **2010**
  - **b15647316 / b17247986** — Vuoden **1992** valtiopäivät vs Vuoden
    **2003** valtiopäivät (Finnish parliamentary records)
  - **b1289509x / b12895131** — Opetuslaitokset **1947-48 / 1948-49**
    vs **1953-54**

Each volume / each year's edition is a **distinct Work in FRBR**;
merging them flattens the catalog and breaks every downstream
Work-level link (skos:narrower / skos:broader, expression-of, holdings
counts).

Linear-extrapolation to 800 k: ~37 / 20 000 = **~1 480 false merges**
projected on the full corpus.

### Why M5 misses

`embedding_input_string` (`src/bffi_pipeline/stages/embeddings.py:212`)
puts the title in one field. BGE-M3 down-weights short numeric tokens
("2", "11", "1990") because the bulk of the lexical context is the
shared series prefix. Cosine similarity for "Naruto Vol. 2" vs
"Naruto Vol. 11" lands at ~0.94 — inside the auto-merge band.

The audit script's volume-detection patterns
(`scripts/audit-merge-clusters.py:_VOLUME_PATTERNS`) and the
`_years_in_title` helper both fire cleanly on these records *as a
post-hoc audit*. Lifting them into the M5 cascade as a veto closes
the audit-vs-production loop.

## Approach

When the M5 auto-merge band hits, extract two numeric markers from
each record's title + subtitle:

1. **Volume number** — `Vol. N`, `Volume N`, `osa N`, `tom(e) N`,
   `Band N`, `ES N`, `KK N`, `TK N`, trailing `. N`, trailing `N`.
2. **Year tokens** — `(19|20)\d{2}` anywhere in the title or
   subtitle text.

If either marker is present on at least one record and *differs*
between the pair, demote from `auto-merge` to `escalate`.

### Phases

**A.1 Lift markers into a shared module.** Move `_VOLUME_PATTERNS` and
`_years_in_title` from `scripts/audit-merge-clusters.py` into
`src/bffi_pipeline/text/markers.py` with public functions
`extract_volume(text) -> int | None` and `extract_years(text) ->
frozenset[str]`. Audit script imports from the new module.

**A.2 Extend `WorkEmbeddingInput`** to carry the title + subtitle
*raw text*, not just the joined string. Or: parse the marker eagerly
at extraction time and store `volume: int | None` and `years_in_title:
frozenset[str]` on the dataclass. The eager-parse option is cleaner
because it keeps the cascade decision a pure function over the
WorkEmbeddingInput type.

**A.3 Add the veto** at the auto-merge decision point:

```python
def _marker_veto(a: WorkEmbeddingInput, b: WorkEmbeddingInput) -> bool:
    if a.volume is not None and b.volume is not None and a.volume != b.volume:
        return True
    if a.years_in_title and b.years_in_title and a.years_in_title != b.years_in_title:
        return True
    # One-sided: a volume on one record only is still suspicious
    # (vol 1 + "the series as a whole" is rarely the same Work).
    if (a.volume is None) ^ (b.volume is None):
        return True
    return False
```

When `_marker_veto` is True at the auto-merge band, demote to
`escalate`. Configurable via `BFFI_M5_MARKER_VETO=0` (default on) for
emergency rollback.

**A.4 Re-bench on `scratchpad/overnight-sample-2026-05-13/`.**
Regression oracle is the 37-row subset of the audit JSONL. Expect:
- All 34 `series_volumes_collapsed` clusters escalate.
- All 3 `annual_series_collapsed` clusters escalate.
- Legitimate-reedition clusters (`legitimate_reedition` = 64) where
  the title carries no volume marker and no year-in-title remain
  unaffected.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** prop-17, prop-18, and prop-19 must be implemented, and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; until the observability surfaces are verified non-misleading, downstream work that consumes bench numbers is faith-based. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- prop-20 ships first if `bffi:fullTitle` is going to source the
  subtitle text; otherwise the veto reads from `245$b` via a
  parallel M3 SPARQL extension. Either works — prop-20 is the cleaner
  path.
- The 2026-05-13 overnight 20 k sample + audit baseline at
  `scratchpad/merge-cluster-verdicts/`.

## Risks

- **R1 — multi-volume legitimately-merged sets.** Rare: a publisher
  may issue "Volume 1 (complete edition)" as a re-issue of the whole
  series — same Work, two manifestations. M6 would handle this
  correctly. Net: extra M6 cost on a small handful.
- **R2 — one-sided volume markers.** A record with "Vol. 1" + a
  record without any vol marker: the conservative move is to
  escalate (could be vol 1 vs the series-as-a-whole reference), and
  the audit confirms this is usually the right call. Operator can
  disable the one-sided branch via a config flag if cataloguer review
  shows excessive escalation.
- **R3 — year-in-title edge cases.** Forsman lectures "den 18 december
  **1889**" vs "den 19 december **1889**" — both share `1889` so the
  year-set check correctly skips. But a record citing "Encyclopedia
  2020 edition" vs "Encyclopedia (2020 reprint of 1965 edition)"
  could have asymmetric year-sets and escalate. Acceptable — M6 is
  the right place.
- **R4 — regex coverage drift.** New languages introduce new volume
  conventions (Korean "권", Japanese "巻", Hebrew "כרך"). Mitigation:
  log a counter of "veto fired" per pattern; if a language pair
  consistently auto-merges series, extend the patterns.

## Open questions

- Should the volume-marker extractor live with the embedding code
  (`src/bffi_pipeline/stages/`) or in a domain-text module
  (`src/bffi_pipeline/text/markers.py`)? Suggest the latter so the
  audit script and any future cataloguer tooling can re-use without
  importing stage code.
- Does this need its own structured-output stage for telemetry, or is
  a "marker veto fired" counter on M5's existing observability sidecar
  enough? Probably the counter is enough.
- Composes with prop-20 + prop-22: all three demote `auto-merge` →
  `escalate` on different conditions. Implementation: chain them in
  the order **prop-23 (markers, cheapest) → prop-22 (token overlap) →
  prop-20 (year-distance)**, short-circuiting on the first hit.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `src/bffi_pipeline/text/markers.py` exports `extract_volume`
      and `extract_years`. Audit script imports from there.
- [ ] `WorkEmbeddingInput` carries `volume` and `years_in_title`
      (or M5 has access to them via another mechanism).
- [ ] M5 auto-merge band demotes to `escalate` when the volume veto
      or the year veto fires.
- [ ] Re-bench: all 34 `series_volumes_collapsed` and all 3
      `annual_series_collapsed` audit rows escalate.
- [ ] Legitimate-reedition spot check: ≥ 95 % of the 64
      `legitimate_reedition` rows continue to auto-merge.

## What this proposal does NOT do

- Doesn't change M8 union-find semantics — Works escalated by the veto
  go through M6 like any other escalated pair.
- Doesn't redesign the MARC volume conventions (Helmet has at least 8
  different ways of recording volume; we extract what we observe and
  log gaps).
- Doesn't propose volume-aware canonical Work URIs (each volume gets
  its own SHA-1 because `bf:mainTitle + subtitle` already differs;
  the bug is purely at M5/M8 cascade, not at URI minting).
