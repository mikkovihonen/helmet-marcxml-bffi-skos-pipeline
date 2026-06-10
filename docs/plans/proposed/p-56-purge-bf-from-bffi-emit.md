# P-56 — Purge `bf:*` (BIBFRAME) terms from the BFFI emit

**Status**: proposed. **Ship-ready via existing BFFI vocabulary** — the mapping doc resolves every former gap with property-discriminator routings using terms already in `lkd.rdf`. NLF will not add new BFFI terms on the basis of this work, so the routings ARE the canonical BFFI shape; NLF input is welcomed for confirmation and per-record-context defaults (e.g., Series axis pick) but does not block ship.

**Scope**: every triple in `canonical.ttl` (the pipeline's BFFI-only emit) must use BFFI / SKOS / DC Terms / PROV-O terms — zero `bf:*` types, zero `bf:*` predicates. BIBFRAME (`bf:*`) stays inside the M3 conversion's INPUT graph (the marc2bibframe2 output the SPARQL CONSTRUCT reads); it must not appear in the output.

**Proposal-base commit**: `527b270` (HEAD of the AdminMetadata + genid-leak work).

**Driver**: NLF guidance (2026-06-09 conversation): "bf:Hub should not be used with BFFI at all, and NO bf entities should be used at all." Current pipeline emits 2,216 `bf:*`-typed instances and ~3,300 `bf:*` predicates per the 500-record sample (extrapolating to ~3.5M `bf:*` triples on the 800k corpus). The 20k-record run that just got killed was stuck on a high-`bf:Hub`-cardinality record (b10007428: 100 × MARC 730 → 200 `bf:Hub` entities), so the symptom — cross-product blow-up — and the structural problem — `bf:*` in BFFI output — share a root.

## How BFFI represents BIBFRAME — the re-anchor pattern

BFFI's design is that **every major BIBFRAME class has a `bffi:*` anchor that is `owl:equivalentClass bf:X`, with the BFFI-native refinements declared as `rdfs:subClassOf` the anchor.** Twenty-two anchors in `lkd.rdf` (verified by rdflib parse). Example:

```
bffi:Person          rdfs:subClassOf  bffi:Agent
                                       │
bffi:Agent           owl:equivalentClass  bf:Agent
```

A consumer that needs the BIBFRAME view of a `bffi:Person` instance recovers it by two-hop OWL inference: `Person ⊑ Agent` (subClass) and `Agent ≡ bf:Agent` (equivalence) → every `bffi:Person` is an `bf:Agent`. Same shape for Work / Expression / Manifestation (anchored to `bffi:BibframeWork ≡ bf:Work`, `bffi:Manifestation ≡ bf:Instance`).

**Implication for this migration:** we do NOT need to dual-type with `bf:*` to preserve the BIBFRAME view. Emitting the **leaf BFFI subclass alone** is sufficient — the BIBFRAME class is reachable by OWL reasoning. Phase 1 is therefore a mechanical "emit BFFI subclass, drop redundant `bf:*` type" pass.

Phase 4 (true gaps) is now precisely the cases where this anchor-and-subclass pattern is incomplete:

- **No anchor at all** — `bf:Hub`, `bf:KeyMode` (no `bffi:Hub` / `bffi:KeyMode` exists)
- **Anchor exists but no leaf subclass for what we need** — `bffi:Identifier` is the anchor for `bf:Identifier`, but BFFI only declares `bffi:Local` + `bffi:ShelfMark` as subclasses; the standard MARC identifier types (`bf:Isbn`, `bf:Ean`, `bf:AudioIssueNumber`, `bf:Issn`, `bf:IssnL`, `bf:OtherIdentifier`) have no BFFI subclass. Same shape for `bffi:Title` anchor vs missing `bffi:VariantTitle`.
- **Semantic shift only** — `bf:Series` has no anchor; BFFI's `bffi:SeriesWork` + `bffi:SeriesExpression` are subclasses of `bffi:Work` / `bffi:Expression` (axis split), reachable only via `bffi-meta:broadMatch`.

## Mapping reference

The term-by-term mapping of every `bf:*` token to its BFFI counterpart lives in **[`docs/bf_to_bffi_mapping.md`](../../bf_to_bffi_mapping.md)** — an rdflib-generated reference covering: every `bf:*` class and predicate the conversion encounters; the five property-discriminator routing callouts that resolve every formerly-gap term using existing BFFI vocabulary (Hub routing, Identifier-scheme routing, Title-variant routing, Series-link routing, Music-medium and music-key routing); the DC Terms → BFFI alternatives section; and the complete re-anchor cluster tree view (22 trees + 56 standalone anchors). This proposal points at that document for term-level reference and focuses on the migration plan and the NLF asks behind the routing choices.

### Migration surface area — 500-record-sample counts

What each class / predicate contributes to the migration work (sample bib counts; extrapolating to ~3.5M `bf:*` triples on the 800k corpus):

| `bf:*` class | Instance count | `bf:*` predicate | Triple count |
|---|---|---|---|
| `bf:Hub` | 1,214 | `bf:source` | 1,271 |
| `bf:ProvisionActivity` | 1,006 | `bf:place` | 500 |
| `bf:Isbn` | 684 | `bf:issuance` | 500 |
| `bf:Series` | 386 | `bf:date` | 500 |
| `bf:Language` | 362 | `bf:note` | 248 |
| `bf:AudioIssueNumber` | 236 | `bf:language` | 181 |
| `bf:Work` | 190 | `bf:hasSeries` | 120 |
| `bf:VariantTitle` | 152 | | |
| `bf:Manufacture` | 98 | | |
| `bf:Ean` | 72 | | |
| `bf:Arrangement` | 28 | | |
| `bf:Distribution` | 4 | | |
| `bf:Source` | 1 | | |
| **Total** | **2,216** | **Total** | **~3,300** |

## Migration path

Four phases. Phase 1 is the bulk of the mechanical rename and ships standalone. Phases 2 and 3 are per-case decisions on the smaller residue. Phase 4 (former true-gap classes) is resolved by the routing patterns in the mapping doc using existing BFFI vocabulary.

### Phase 1 — clean rename (no semantic change)

Search-and-replace every class and predicate where lkd.rdf has either `owl:equivalentClass`/`owl:equivalentProperty` **OR** `rdfs:subPropertyOf bf:X` — the latter is BFFI's convention for "this is the refined BFFI-native predicate whose range is the equivalent-class". Both shapes are loss-free renames.

- **Classes** (8): `bf:ProvisionActivity`, `bf:Language`, `bf:Manufacture`, `bf:Arrangement`, `bf:Distribution`, `bf:Source` (rename); `bf:Work` (drop redundant secondary type; we already dual-type as `bffi:Work`); `bf:Note` (already used as secondary type — drop).
- **Predicates** (~38): all `owl:equivalentProperty` rows + the `subPropertyOf bf:X` ones (`bf:source`, `bf:place`, `bf:date`, `bf:note`, `bf:carrier`, `bf:content`, `bf:musicMedium`, `bf:agent`).

Touchpoints: `sparql/bf_to_bffi_work.rq`, `sparql/bf_to_bffi_expression.rq`, `sparql/bf_to_bffi_manifestation.rq`, `sparql/bf_to_bffi_aggregation.rq`. Plus the Skosify display passes in `src/bffi_pipeline/stages/m10/skosify_run.py` that read these triples. Plus the round-trip converter (`src/bffi_pipeline/marc_roundtrip/converter.py`) that reconstructs MARC from canonical.

Estimated effort: ~half day. Risk: low — semantics are preserved or strictly tightened; tests catch any reader still pinned to `bf:X`.

### Phase 2 — broadMatch predicates (semantic shift, per-case review)

Predicates where the only alias is `bffi-meta:broadMatch` or `bffi-meta:closeMatch` — the BFFI term means something *related but not identical*:

- `bf:issuance` → `bffi:issuance` (broadMatch — fine, BFFI's is broader)
- `bf:instanceOf` → `bffi:expressionManifested` OR `bffi:workManifested` — choose based on whether the M3 emit is currently pointing at a Work or Expression.

Risk: medium — each broadMatch case needs human review of "is the semantic shift OK for our usage?".

### Phase 3 — axis-split classes (`bf:Series`, `bf:Cartography`, `bf:MovingImage`, `bf:MusicAudio`)

Four BIBFRAME classes that BFFI splits into Work-axis and Expression-axis pairs (no single anchor; only `bffi-meta:broadMatch` / `closeMatch` links):

| BIBFRAME | BFFI Work-axis | BFFI Expression-axis | Link kind |
|---|---|---|---|
| `bf:Series` | `bffi:SeriesWork` | `bffi:SeriesExpression` | `broadMatch` |
| `bf:Cartography` | `bffi:CartographyWork` | `bffi:CartographyExpression` | `broadMatch` |
| `bf:MovingImage` | `bffi:MovingImageWork` | `bffi:MovingImageExpression` | `broadMatch` |
| `bf:MusicAudio` | `bffi:MusicWork` *(closeMatch)* | `bffi:MusicAudioExpression` | mixed |

The M3 SPARQL must decide which axis applies per emit. For Series specifically: most Helmet series links are Expression-level (Finnish translation of an English series → the Expression is in the series); some are Work-level (manifold edition of the same Work). For Cartography / MovingImage / MusicAudio: the M3 content-typing path (`bf_to_bffi_work.rq` P-52 Phase A/B) already chooses Work vs Expression — extend the existing typing tables to emit BOTH the Work-axis and Expression-axis BFFI subclasses on the corresponding URIs.

Recommended default: emit the Expression-axis subclass for axis-ambiguous cases and revisit per `L-###` case study after the first 20k-record run.

Risk: medium — wrong choice affects how cataloguers query "what's in series X?" / "what video Expressions exist?".

### Phase 4 — former true-gap classes (resolved by routing patterns)

The classes once considered Phase-4 blockers (`bf:Hub`, the Identifier subclasses, `bf:VariantTitle`, `bf:Audio`, `bf:KeyMode`, and the predicate `bf:hasSeries`) all have working routings in the mapping doc using existing BFFI vocabulary. Since NLF will not add new BFFI terms on the basis of this work, the routings are not provisional — they are the canonical BFFI shape for each term.

The Phase-4 implementation is mechanical: emit the property-discriminator shape from the mapping doc (Hub routing, Identifier-scheme routing, Title-variant routing, Series-link routing, Music-medium and music-key routing). The SPARQL emits already use existing BFFI vocabulary; no closed-namespace test changes.

This phase affects the marc-roundtrip converter — the routing currently reads BIBFRAME-side typing (`bf:Hub`, `bf:VariantTitle`, etc.) as a routing key for MARC reconstruction. After Phase 4 ships, the converter reads BFFI-side predicates instead (`bffi:marcKey` first-3-chars, `bffi:source` URI, the structured-relation `bffi:relationship`, etc.). The mapping doc's routing callouts show the read-side for each case.

## Open questions for NLF

NLF will not add new BFFI terms on the basis of this migration work. Every gap is therefore resolved by the routing patterns in the mapping doc using existing BFFI vocabulary; the questions below are for **confirmation and per-context defaults**, not term additions.

### Per-class routing confirmations

For each former gap term, the mapping doc's routing is the working canonical shape. NLF confirmation requested for each:

| BIBFRAME term | Used for | Canonical BFFI shape (per mapping doc) |
|---|---|---|
| `bf:Hub` (1,214 in sample) | MARC 240 / 730 / 740 / aggregate-Work component | Hub routing: route to `bffi:Expression` (when language / arrangement / key / medium / version present in `bflc:marcKey`) or `bffi:Work` (otherwise). Forward the marcKey verbatim as `bffi:marcKey`. |
| `bf:Isbn` / `bf:Issn` / `bf:Ean` / `bf:AudioIssueNumber` / `bf:OtherIdentifier` | MARC 020 / 022 / 024 / 028 | Identifier-scheme routing: `bffi:Identifier` + `bffi:source <http://id.loc.gov/vocabulary/identifiers/{scheme}>`. The LoC scheme URI is the discriminator. |
| `bf:VariantTitle` (152) | MARC 246 / 740 ind2=0 (and the other three BIBFRAME Title subclasses: ParallelTitle, KeyTitle, CollectiveTitle) | Title-variant routing: `bffi:Title` + `bffi:marcKey` carrying the original MARC field encoding; the first 3 chars discriminate title kind. |
| `bf:Series` (386) | MARC 490 / 800 / 810 / 830 series links | Routes via the existing axis-split classes `bffi:SeriesWork` (Work-axis) and `bffi:SeriesExpression` (Expression-axis). |
| `bf:Audio` | Content-type parent of `bf:MusicAudio` / `bf:NonMusicAudio` | Audio routing: route to `bffi:NonMusicAudio*` (marc2bibframe2 emits `bf:Audio` only for non-music; music emits `bf:MusicAudio` directly). |
| `bf:KeyMode` / `bf:keyMode` | Structured key-mode block (newly defined in BIBFRAME 3.0 Dec 2025 via PMO absorption) | Music-key routing: fold into the existing `bffi:musicKey` Literal datatype property (`owl:equivalentProperty bf:musicKey`). |
| `bf:mediumOfPerformance` / `bf:mediumComponent` / `bf:ensemble` (and the BIBFRAME 3.0 PMO siblings: `bf:Ensemble`, `bf:EnsembleSize`, `bf:Mode`, `bf:Tempo`, `bf:DramaticRole`, `bf:MediumComponentQualifier`) | MARC 382 medium of performance | Music-medium routing: collapse into `bffi:musicMedium` → `bffi:MusicMedium` → `bffi:readMarc382` literal carrying the verbatim MARC 382 string. |
| `bf:hasSeries` (120) | Series-link predicate | Series-link routing: `bffi:relation` → `bffi:Relation` bnode with `bffi:relationship <http://id.loc.gov/vocabulary/relationship/series>` + `bffi:associatedResource <series>`. |

### Policy-level questions

1. **`bf:Series` axis default** — when MARC 490/830 produces a series link, should BFFI default to `bffi:SeriesWork` or `bffi:SeriesExpression`? The mapping doc recommends `bffi:SeriesExpression` for the common case where the bib is a localised Expression-in-series; NLF confirmation would lock the default.

2. **Transition window** — should the rename ship as a hard cut (emit only `bffi:*`), or a one-release transition where both `bffi:X` and the now-inferred parent `bf:X` are emitted, letting downstream consumers migrate? Strict reading of NLF guidance: hard cut.

3. **Confirmation per routing** — each row in the table above is the working canonical shape. NLF review can flag any routing that diverges from BFFI's design intent. (If a routing must change, the alternative would still need to use existing BFFI vocabulary — no new terms.)

## Verification

The script below regenerates `docs/bf_to_bffi_mapping.md`'s Classes / Predicates tables and Re-anchor cluster trees from `docs/lkd.rdf`. Re-run it whenever the ontology changes; treat any new GAP row as a Phase 4 ask until reviewed.

```python
# scripts/scan_bffi_bf_mappings.py (proposed; not yet committed)
from rdflib import Graph, URIRef, Namespace
from rdflib.namespace import OWL, RDFS

BFFI = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")
BF = Namespace("http://id.loc.gov/ontologies/bibframe/")

# IMPORTANT: bffi-meta:* uses a colon separator, NOT a hash. Constructing
# the namespace with Namespace("…/bffi-meta#") silently mis-aliases every
# broadMatch / closeMatch / exactMatch / narrowMatch URI and the scan
# reports false gaps for the axis-split classes. Use the colon-suffixed
# prefix directly when building URIRefs.
BFFI_META_PREFIX = "http://urn.fi/URN:NBN:fi:schema:bffi-meta:"

g = Graph()
g.parse("docs/lkd.rdf", format="xml")

# For each bf:* term referenced in sparql/*.rq CONSTRUCTs, look up every
# bffi:* relation in lkd.rdf:
#   owl:equivalentClass / equivalentProperty   (both directions)
#   rdfs:subClassOf / subPropertyOf            (both directions; the
#                                               re-anchor pattern is
#                                               bffi:Sub subClassOf
#                                               bffi:Anchor where
#                                               bffi:Anchor ≡ bf:X)
#   bffi-meta:broadMatch / closeMatch /
#                exactMatch / narrowMatch      (both directions; use
#                                               BFFI_META_PREFIX above)
#   owl:sameAs                                  (both directions)
# Print one row per (bf_term, link_kind, bffi_term) triple. Any bf:*
# token that produces zero rows is a Phase 4 NLF-ask candidate.
```

Future revisions of the mapping doc and this proposal MUST start from a fresh rdflib parse, not from cached tables. Mapping data drifts whenever `lkd.rdf` is updated (e.g., NLF absorbing the BIBFRAME 3.0 PMO model would change every Music-medium-and-music-key routing decision).

## Risks

- **Round-trip fidelity**: Phase 4 affects how `bffi:Hub`'s replacement renders in MARC 240/730/740 round-trip. The current `_emit_analytical_added_entry_from_hub` converter pass reads `bf:Hub` typing as a routing key. The replacement type needs the same routing behaviour.
- **Skosmos rendering**: many of the M10 display passes (the genid-leak fixes from yesterday) walk specific `bf:X` types — see `_synthesise_related_resource_display`, `_synthesise_main_title_pref_label`, etc. Each pass needs to walk the new BFFI type too (or only).
- **External consumers**: if anyone downstream is reading the BFFI emit and expecting `bf:Hub`-shaped data (NLF's own tooling? cataloguer scripts?), the rename breaks them. **Question for NLF**: should the rename ship with a one-release transition where both sides emit, or hard-cut?
- **Test fixtures**: ~50+ unit tests assert specific `bf:*` triples on M3 output. Each needs the rename.

## Rollback

- **All phases**: pure rename. `git revert` restores `bf:*` emit. No data migration needed (the canonical graph is per-run, rebuilt from MARCXML each invocation).

## Suggested next step

Three items to do in parallel:

1. **NLF conversation** (blocker for Phase 4): take the open questions above to NLF. Ideal channel: the same conversation that triggered this proposal. Confirm the five true-gap classes.
2. **Phase 1 prototype** (no NLF dependency): land the clean-equivalence renames on a feature branch (or direct-to-main if confident) and confirm tests pass + 500-record canonical still loads into Skosmos correctly. Validates the mechanical migration approach.
3. **Closed-namespace test update**: `tests/unit/test_bffi_namespace_discipline.py` currently only checks `bffi:*` terms exist in lkd.rdf. Extend it to check that NO `bf:*` term appears in `sparql/*.rq` outputs (CONSTRUCT clauses) OR in `src/bffi_pipeline/provenance/vocab.py`'s emit set. This becomes the regression gate that prevents `bf:*` re-introduction.

Implementation order after NLF responds: Phase 1 (clean rename) → Phase 3 (`bf:Series` axis pick) → Phase 2 (broadMatch per-case) → Phase 4 (true-gap classes, depends on NLF).
