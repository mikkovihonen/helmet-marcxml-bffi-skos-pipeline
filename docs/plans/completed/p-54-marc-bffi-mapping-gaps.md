# P-54 — Close MARC ⇆ BFFI round-trip gaps from the corpus inventory

**Status**: completed (2026-06-09; six implementations shipped, two
upstream-gated gaps deferred + documented).

**Source**: `scratchpad/2026-06-08-marc-bffi-mapping-gap-audit.md`
(the corpus-wide gap audit derived from
`scratchpad/2026-05-18-sierra-corpus-marc-inventory.md`). No formal
proposal preceded this plan — the audit graduated directly into
execution per the user's "jump straight into Phase 1" directive.

**Plan-base commit**: `d4b5303` — HEAD of the P-53 plan-promotion
work, after the BFFI namespace-alias migration shipped end-to-end.

**Phase commit**: **`175e4e8`** — all seven phases shipped as a
single direct-to-main commit (per the project's solo-developer
workflow). The phase split is a verification-and-narrative
structure, not a merge boundary.

## Goal

Audit the round-trip emit surface against the full 800,436-record
Helmet MARCXML corpus inventory and close the highest-leverage gaps
where M3 was already capturing data (or marc2bibframe2 was emitting
it) that the round-trip wasn't reconstructing. **Done when** every
shipped phase verifies on the 500-record stratified sample —
new MARC rows appear in `marc-roundtrip/reconstructed/*.xml` for
records that carried them in source.

## Current state (as of Plan-base commit)

The round-trip emit set covered 49 MARC tags. The corpus inventory
shows ~200 distinct tags in the Helmet corpus. Many gaps were tiny
(single-digit record counts) and not worth covering, but several
high-volume gaps existed:

- **084** YKL classification (98.49 % corpus) — emitted as empty
  `bffi:classification` bnode by M3, dropped by round-trip.
- **024** Other Standard Identifier (12.97 %, 103 k records) — not
  in M3 SPARQL or round-trip.
- **022** ISSN — same.
- **130** main-entry uniform title (2.44 %) — folded into 240 emit
  with no distinguishing routing.
- **830** series added entry uniform title (2.37 %) — folded into
  490 emit.
- **534** original version note (2.00 %) — `mnotetype/orig` tail
  not in `_MNOTETYPE_TO_MARC_5XX` map.
- **588** source of description note (0.97 %) — `mnotetype/descsource`
  same gap.
- **880** vernacular (4.41 %, 35 k records) — marc2bibframe2
  preserves the data as language-tagged literals on the same
  BIBFRAME entity; M3 SPARQL preserves both forms through; round-
  trip never reads the language-tagged companions.

Two gaps cannot be closed without M2-post synthesis because
marc2bibframe2 drops the source data at the BIBFRAME boundary:

- **09X Helmet-local classifications** (091/092/093/094/095/097,
  ~99 % corpus combined).
- **574 / 575 Helmet-local broadcaster notes** (~42 % corpus
  combined).

`make lint && make test` green at Plan-base (1656 tests).

## Phases

### Phase 1 — classifications

**Goal**: emit MARC 084 (and 080 / 082 / 050 when present in
source) from the canonical `bffi:classification` chain.

**Implementation**:

- `sparql/bf_to_bffi_work.rq` — WHERE clause now reads
  `bf:classification → bf:classificationPortion + bf:source →
  bf:Source → bf:code`. CONSTRUCT emits the inner chain so canonical
  carries content under `bffi:classification` (was empty bnode).
- `src/bffi_pipeline/stages/m8/mint.py` — new
  `_propagate_work_classifications` pass copies the chain from raw
  Work URI → canonical Work URI (parallel to existing
  `_propagate_work_typing`).
- `src/bffi_pipeline/stages/m8/apply.py` — wires the new pass into
  the M8 post-loop.
- `src/bffi_pipeline/marc_roundtrip/converter.py` — new
  `_emit_classifications` walker keyed on
  `_CLASSIFICATION_SOURCE_CODE_TO_MARC_TAG` (ykl → 084, udc → 080,
  dewey/ddc → 082, lcc → 050).
- `tests/unit/test_marc_roundtrip_converter.py` — 3 new tests.

**Verification on 500-sample**: 611 × MARC 084 rows now emit
(was 0). Spot-checked: `b10068004` → `084 $a 78 $2 ykl` ✓.

**Deferred**: 09X Helmet-local classifications. Documented as
L-10 in `docs/bffi_limitations.md`.

### Phase 2 — 022 / 024 identifiers

**Implementation**:

- `sparql/bf_to_bffi_manifestation.rq` — CONSTRUCT extended with
  ?issn (`bf:Issn`) and ?otherStd (`bf:Ean` / `bf:OtherIdentifier`).
- `src/bffi_pipeline/marc_roundtrip/converter.py` — new
  `_emit_issns` (MARC 022) and `_emit_other_std_identifiers` (MARC
  024 — ind1=3 for `bf:Ean`, ind1=8 for `bf:OtherIdentifier`)
  walkers.

**Verification**: 36 × 024 rows on 500-sample (b18685389 → `024
ind1=3 $a 4607148461517` ✓). No 022 records in 500-sample (low
ISSN coverage in the stratified subset).

### Phase 3 — note `mnotetype` additions

**Implementation**: two new tails added to
`_MNOTETYPE_TO_MARC_5XX` in
`src/bffi_pipeline/marc_roundtrip/converter.py`:

- `orig` → 534 (original version note)
- `descsource` → 588 (source of description note)

**Verification**: 9 × 534 rows + 7 × 588 rows on 500-sample.
Spot-checked b18104769 (534), b23825583 (588).

**Notes**: 518 / 521 / 532 / 538 / 540 don't get a dedicated
mnotetype tail from marc2bibframe2 — they flow as generic `bf:Note`
and route to MARC 500 via the existing `_emit_notes` walker.

### Phase 4 — 574 / 575 Helmet broadcaster notes

**Deferred.** `third_party/marc2bibframe2/xsl/ConvSpec-5XX.xsl`
has no template for these Helmet-local 5XX tags; the data is
dropped at the BIBFRAME boundary. Same pattern as 09X
classifications. Documented as **L-11** in
`docs/bffi_limitations.md`. M2-post synthesis path described in
the limitation; ~3-4 h follow-on work.

### Phase 5 — 130 main-entry uniform title

**Implementation**: `_emit_uniform_title` in
`src/bffi_pipeline/marc_roundtrip/converter.py` now inspects the
Hub URI segment:

- `#Hub130-N` → MARC 130 (ind1=0, ind2=blank)
- `#Hub240-N` (default) → MARC 240 (ind1=1, ind2=0)

**Verification**: 13 rows previously folded into 240 now route to
130; net 240 count drops correspondingly (140 → 127). Spot-checked
b17864768 (130 with `$l suomi`) ✓.

### Phase 6 — 830 series added entry

**Implementation**: `_emit_series_statement` now inspects the
`bf:hasSeries` target node URI:

- `#Hub830-N` → MARC 830 (ind1=0, ind2=blank)
- bnode (490 source) → MARC 490 (ind1=0, ind2=blank)

**Verification**: 7 rows previously folded into 490 now route to
830; net 490 count drops correspondingly (206 → 199). Spot-checked
b15531363 ✓.

**Deferred subfield**: `$v` series enumeration. Lives on the
`bf:Relation` node (`bf:seriesEnumeration`), not on the
`bf:hasSeries` target. Round-trip would need to walk the Relation
chain to recover it; small surface, deferred.

### Phase 7 — 880 vernacular emit (Solution A)

**Implementation**: new `_emit_vernacular_880` walker +
`_detect_marc_script_code` Unicode-block helper in
`src/bffi_pipeline/marc_roundtrip/converter.py`. Walks the
canonical for language-tagged literal companions of untagged
primaries on key predicates:

- `bf:mainTitle` on `bffi:title` → MARC 880 ↔ 245
- `bffi:responsibilityStatement` on Manifestation → 880 ↔ 245
- `bffi:publicationStatement` + `bflc:simplePlace` /
  `bflc:simpleAgent` / `bflc:simpleDate` on ProvisionActivity →
  880 ↔ 264
- `rdfs:label` on PrimaryContribution agent → 880 ↔ 100
- `rdfs:label` on non-primary Contribution agent → 880 ↔ 700

`$6 <primary-tag>-NN/<script>` reconstructed via Unicode-block
detection on the literal text:
- Cyrillic → `(N`
- Hebrew → `(2`
- Arabic → `(3`
- Greek → `(S`
- CJK Unified Ideographs / Hiragana / Katakana / Hangul → `$1`
- default → `(B` (Latin)

**Pre-implementation finding**: 348 `@ru`-tagged literals already
in the 500-sample canonical (marc2bibframe2 pairs 880s natively
via `xml:lang`-tagged literals on the same BIBFRAME entity; M3
SPARQL preserves them). Phase 7 is purely a round-trip-emit
extension — no M3 changes.

**Verification**: 144 × 880 rows on 500-sample. Spot-checked
b18685389 (Russian/Cyrillic recording) → 8 × 880 rows including
`880 $6 245-01/(N $a режиссер Сергей Урсуляк`,
`880 $6 264-02/(N $a Москва: ЦЕНТРАЛ ПАРТНЕРШИП, 2007`, etc. ✓.

**Pairing heuristic gap**: documented as **L-09** in
`docs/bffi_limitations.md`. Records with two non-Latin scripts in
different fields (a Russian record citing an Arabic-script book in
700) can mis-pair. Estimated <0.1 % corpus frequency.

## Pipeline verification

Full pipeline rerun on `runs/20260608-0810-f8d8eb` (M3 → M5 → M6 →
M8 → M9 → Skosify → load → marc-roundtrip → export →
cataloguer-bundle), wall: 37 min (2231 s).

Stage timings:

| Stage | Wall |
|---|---|
| m3 | 1213 s |
| m5 | 28 s |
| m6 | 13 s |
| m8 | 4 s |
| m9 | 583 s |
| skosify | 8 s |
| load | 25 s |
| marc-roundtrip | 355 s |

Tag-by-tag emit count (before → after, across 502 recon files):

| Tag | Before | After | Δ |
|---|--:|--:|--:|
| 084 | 0 | 611 | +611 |
| 024 | 0 | 36 | +36 |
| 130 | 0 | 13 | +13 |
| 240 | 140 | 127 | −13 |
| 490 | 206 | 199 | −7 |
| 830 | 0 | 7 | +7 |
| 534 | 0 | 9 | +9 |
| 588 | 0 | 7 | +7 |
| 880 | 0 | 144 | +144 |

Net: **827 new MARC rows** recoverable across 500 records. The
240→130 and 490→830 net-zeros confirm correct re-routing of the
existing rows (not duplication). 080 / 082 / 022 show zero emit in
the 500-sample because the stratified subset has no records
carrying those tags — the code path is verified by unit tests
instead.

## Risk register

1. **`_emit_vernacular_880` emits `$a "2007"@<lang>` for digit-only
   vernacular companions**. Both the Latin and the language-tagged
   forms of "2007" are byte-identical (digits only). The current
   walker emits the 880 row anyway because it correctly detects an
   `@<lang>`-tagged literal with an untagged sibling. Net effect:
   harmless additional 880 row with the same value as the primary.
   Mitigation if it gets noisy: skip companions whose value equals
   the untagged primary.

2. **`$6 <primary-tag>-NN` sequence counter doesn't match source**.
   The walker assigns sequence `01`, `02`, … as it visits entities;
   the source MARC's $6 occurrences track the cataloguer's ordering
   of primary rows in the record. The reconstructed `$6` numbers
   are deterministic per recon emit-order but won't always match
   what the source had. The PAIRING (primary tag ↔ 880) is still
   correct; only the numerical sequence may differ. Acceptable per
   L-09's "heuristic pairing" framing.

3. **Phase 1 SHACL shape unchanged**. The
   `bffi:classification` propagation surfaces new triples on the
   canonical Work; `config/shapes/bffi.shape.ttl` has no
   `bffi:classification` rule today, so the new emit is unconstrained
   by SHACL. Future work could add a shape rule asserting that
   every Work carrying `bffi:classification` has a non-empty
   `classificationPortion` + a `bf:source/bf:code` chain.

## Rollback procedure

Per-phase revert is not straightforward because all seven shipped
in commit `175e4e8`. To revert:

```bash
git revert 175e4e8
make lint && make test  # confirm clean
bffi-pipeline run --from-stage m3 --force-stages m3,m9,skosify \
  -i marcxml/samples/helmet/500/marcxml/
```

Then diff `runs/.../marc-roundtrip/reconstructed` against the
pre-P-54 snapshot to confirm restoration.

## Out of scope (follow-up plans)

1. **L-10 / Phase 1B**: M2-post synthesis of Helmet-local 09X
   classifications (091/092/093/094/095/097, ~99 % corpus combined).
   Estimated ~3-4 h. Self-contained — just needs to walk source
   MARC for these tags and mint `bf:Classification` nodes with a
   Helmet-local source URI, attach to `bf:Work`. The existing
   `_emit_classifications` walker handles the round-trip once the
   Helmet-local source codes are added to
   `_CLASSIFICATION_SOURCE_CODE_TO_MARC_TAG`.
2. **L-11 / Phase 4 follow-up**: same M2-post synthesis for 574 /
   575 Helmet broadcaster notes. Mint `bf:Note` nodes typed
   `<mnotetype/helmet-broadcaster>` /
   `<mnotetype/helmet-broadcaster-orig>`. Add the two new tails to
   `_MNOTETYPE_TO_MARC_5XX`. ~1-2 h.
3. **830 `$v` series enumeration**. Walk the `bf:Relation` chain
   for `bf:seriesEnumeration` and add the value as `$v` on the
   emitted 830 row.
4. **Phase 7 refinements** (if production data shows heuristic
   mis-pairs):
   - Skip 880 emit when the language-tagged literal equals the
     untagged primary byte-for-byte (cosmetic).
   - Track entity-emit order properly to align `$6` sequence
     counters with source MARC order.
   - Extend to 130 / 240 / 600 / 610 / 611 / 630 / 730 / 740 / 800
     / 810 / 830 vernacular companions (lower volume; mechanical
     extension).
   - Escalate to Solution B (parallel Expression linked via
     `bffi:AggregatingExpression`) or Solution C (propose
     `bffi:vernacularOf` to NLF) if mis-pairs measurably reduce
     cataloguer-review trust.
