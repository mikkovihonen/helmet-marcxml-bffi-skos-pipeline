# P-56 — Purge `bf:*` (BIBFRAME) terms from the BFFI emit

**Status**: proposed. Ship-ready via existing BFFI vocabulary; one documented exception (music — see Phase 5).

**Scope**: every triple in `canonical.ttl` (the pipeline's BFFI-only emit) must use BFFI / SKOS / DC Terms / PROV-O terms — zero `bf:*` classes, zero `bf:*` predicates. BIBFRAME (`bf:*`) stays inside the M3 conversion's INPUT graph (the marc2bibframe2 output the SPARQL CONSTRUCT reads); it must not appear in the output.

**Proposal-base commit**: `cce9709` (HEAD after the bf_to_bffi_mapping.md NLF-review-ready pass).

**Mapping reference**: every term-level routing decision in this plan is grounded in **[`docs/bf_to_bffi_mapping.md`](../../bf_to_bffi_mapping.md)** — an rdflib-generated reference covering every `bf:*` class and predicate encountered in the conversion, the five property-discriminator routing callouts that resolve formerly-gap terms using existing BFFI vocabulary (Hub, Identifier-scheme, Title-variant, Series-link, Music-medium-and-music-key), the DC Terms → BFFI alternatives table, and the complete re-anchor cluster tree view. This proposal points at that document and does not duplicate its tables.

## Driver

NLF guidance (2026-06-09 conversation): *"bf:Hub should not be used with BFFI at all, and NO bf entities should be used at all."* The pipeline currently emits 2,216 `bf:*`-typed instances and ~3,300 `bf:*` predicate triples per a 500-record sample (extrapolating to ~3.5 M `bf:*` triples on the 800 k Helmet corpus). A 20 k-record run was recently killed on a high-`bf:Hub`-cardinality record (b10007428: 100 × MARC 730 → 200 `bf:Hub` entities → cross-product blow-up in the M3 SPARQL CONSTRUCT). The symptom and the structural problem — `bf:*` in BFFI output — share a root.

## The BFFI re-anchor pattern (why this works without dual-typing)

BFFI's design is that every major BIBFRAME class has a `bffi:*` anchor that is `owl:equivalentClass bf:X`, with BFFI-native refinements declared as `rdfs:subClassOf` the anchor. Twenty-two anchors in `lkd.rdf` (verified by rdflib parse; see the re-anchor tree view in the mapping doc). Example:

```
bffi:Person  rdfs:subClassOf  bffi:Agent  owl:equivalentClass  bf:Agent
```

A consumer that needs the BIBFRAME view of a `bffi:Person` instance recovers it by two-hop OWL inference: `Person ⊑ Agent` (subClass) and `Agent ≡ bf:Agent` (equivalence) → every `bffi:Person` is a `bf:Agent`. Same shape for Work / Expression / Manifestation (anchored to `bffi:BibframeWork ≡ bf:Work`, `bffi:Manifestation ≡ bf:Instance`).

**Implication**: dropping `bf:*` from the emit does not lose the BIBFRAME view. Emitting the leaf BFFI subclass alone is sufficient — the BIBFRAME class is reachable by OWL reasoning. The migration is therefore a sequence of mechanical replacements, not a re-modelling.

## Migration surface area — 500-record sample

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

## Migration phases

Five phases. Phases 1–4 use the routings already worked out in the mapping doc and ship against BFFI 1.0.0 as the canonical shape. Phase 5 covers the one term family (music) where the routing is provisional because BFFI 1.1.0 (planned) will align with BIBFRAME 3.0's PMO absorption and add structured equivalents.

### Phase 1 — clean rename (no semantic change)

Apply to every row marked **clean** in the mapping doc's Classes and Predicates tables — terms with `owl:equivalentClass` / `owl:equivalentProperty` or `rdfs:subPropertyOf` to `bf:*`. Both shapes are loss-free renames (the `subPropertyOf` cases are BFFI's convention for "refined-BFFI predicate whose range is the equivalent-class").

- **Classes** — ~30 clean entries including `bf:ProvisionActivity`, `bf:Language`, `bf:Manufacture`, `bf:Arrangement`, `bf:Distribution`, `bf:Source`, `bf:Work`, `bf:Person`, `bf:Place`, `bf:Topic`, `bf:Temporal`, `bf:Note`, `bf:Title`, `bf:Identifier` (anchor), and the standalone anchors enumerated in the mapping doc's "Standalone anchors" list.
- **Predicates** — all `owl:equivalentProperty` rows + the `rdfs:subPropertyOf bf:X` ones (`bf:source`, `bf:place`, `bf:date`, `bf:note`, `bf:carrier`, `bf:content`, `bf:musicMedium`, `bf:agent`). Around 38 entries total.

Touchpoints: `sparql/bf_to_bffi_work.rq`, `sparql/bf_to_bffi_expression.rq`, `sparql/bf_to_bffi_manifestation.rq`, `sparql/bf_to_bffi_aggregation.rq` (CONSTRUCT clauses); the Skosify display passes in `src/bffi_pipeline/stages/m10/skosify_run.py` (read-side); the round-trip converter `src/bffi_pipeline/marc_roundtrip/converter.py` (read-side).

Estimated effort: ~half day. Risk: low — semantics preserved or strictly tightened; tests catch any reader pinned to `bf:X`.

### Phase 2 — broadMatch / closeMatch (semantic-shift, per-case review)

Apply to rows marked ***semantic-shift*** in the mapping doc — the BFFI alternative carries `bffi-meta:broadMatch` or `closeMatch` only, meaning the term is related but not identical:

- `bf:issuance` → `bffi:issuance` (broadMatch; BFFI's is broader)
- `bf:instanceOf` → `bffi:expressionManifested` OR `bffi:workManifested` — choose per emit context (whether M3 currently points at a Work or Expression)
- `bf:expressionOf` → `bffi:expressionOf` (broadMatch) vs `bffi:representativeExpressionOf` (`owl:equivalentProperty`, but only applies when the Expression is the representative one)
- `bf:extent` / `bf:intendedAudience` / `bf:musicMedium` — equivalentProperty for the "of representative expression" variants; closeMatch for the unscoped variant

Risk: medium — each broadMatch case needs human review of "is the semantic shift OK for our usage?"

### Phase 3 — axis-split classes (`bf:Series`, `bf:Cartography`, `bf:MovingImage`, `bf:MusicAudio`)

Four BIBFRAME classes that BFFI splits into Work-axis and Expression-axis pairs (no single anchor; `bffi-meta:broadMatch` / `closeMatch` links only):

| BIBFRAME | BFFI Work-axis | BFFI Expression-axis |
|---|---|---|
| `bf:Series` | `bffi:SeriesWork` | `bffi:SeriesExpression` |
| `bf:Cartography` | `bffi:CartographyWork` | `bffi:CartographyExpression` |
| `bf:MovingImage` | `bffi:MovingImageWork` | `bffi:MovingImageExpression` |
| `bf:MusicAudio` | `bffi:MusicWork` *(closeMatch)* | `bffi:MusicAudioExpression` |

The M3 SPARQL must decide which axis applies per emit. For Series specifically: most Helmet series links are Expression-level (Finnish translation of an English series — the Expression is in the series); some are Work-level (manifold edition of the same Work). For Cartography / MovingImage / MusicAudio: the existing content-typing logic in `sparql/bf_to_bffi_work.rq` already chooses Work vs Expression — extend the typing tables to emit both the Work-axis and Expression-axis BFFI subclasses on the corresponding URIs.

Recommended default: emit the Expression-axis subclass for axis-ambiguous cases (matches Helmet's predominant pattern) and revisit per case study after the first 20 k-record run.

Risk: medium — wrong choice affects how cataloguers query "what's in series X?" / "what video Expressions exist?"

### Phase 4 — discriminator-routed classes (canonical via existing vocab)

Five term families that have no direct BFFI subclass but route to existing BFFI vocabulary via a per-instance discriminator. Each routing is documented in the mapping doc and is the canonical BFFI shape — NLF will not be adding new terms for these; the routings ARE the answer.

| Routing | Terms covered | Discriminator | BFFI emit shape |
|---|---|---|---|
| [Hub routing](../../bf_to_bffi_mapping.md#hub-routing-bfhub--bffiexpression-vs-bffiwork) | `bf:Hub` (1,214 / sample) | `bflc:marcKey` first 3 chars + embedded subfield codes | `bffi:Work` or `bffi:Expression` (or a leaf subclass) + `bffi:marcKey` |
| [Identifier-scheme routing](../../bf_to_bffi_mapping.md#identifier-scheme-routing-bfisbn--bfissn--bfean--bfaudioissuenumber--bfotheridentifier--bffiidentifier--bffisource) | `bf:Isbn` (684) / `bf:Issn` / `bf:Ean` (72) / `bf:AudioIssueNumber` (236) / `bf:OtherIdentifier` | LoC scheme URI | `bffi:Identifier` + `bffi:source <…/identifiers/{scheme}>` |
| [Title-variant routing](../../bf_to_bffi_mapping.md#title-variant-routing-bfvarianttitle--bffititle--bffimarckey) | `bf:VariantTitle` (152) plus `bf:ParallelTitle` / `bf:KeyTitle` / `bf:CollectiveTitle` | MARC tag in `bffi:marcKey` (first 3 chars) | `bffi:Title` + `bffi:marcKey` |
| [Series-link routing](../../bf_to_bffi_mapping.md#series-link-routing-bfhasseries--bffirelation--bffiserieswork--bffiseriesexpression) | `bf:hasSeries` (120) | `bffi:relationship <…/relationship/series>` | `bffi:relation` → `bffi:Relation` bnode |
| `bf:Audio` content-type | `bf:Audio` | marc2bibframe2 emit convention (music gets `bf:MusicAudio` direct) | `bffi:NonMusicAudioWork` / `bffi:NonMusicAudioExpression` |

Each row's BFFI emit shape uses only terms already in `lkd.rdf`; no closed-namespace test changes required. Phase 4 affects the round-trip converter — its current routing reads BIBFRAME-side typing (`bf:Hub`, `bf:VariantTitle`, etc.) as a key for MARC reconstruction. After Phase 4 ships, the converter reads BFFI-side predicates (`bffi:marcKey` first-3-chars, `bffi:source` URI, the structured-relation `bffi:relationship`, etc.). The mapping doc's routing callouts document the read-side for each case.

Risk: medium — discriminator-based routing is more verbose than type-based routing; tests that previously matched on `?x a bf:Hub` need rewriting.

### Phase 5 — music exception (interim against BFFI 1.0.0; revisit at BFFI 1.1.0)

`bf:KeyMode` and `bf:mediumOfPerformance` / `bf:mediumComponent` / `bf:ensemble` (plus the BIBFRAME 3.0 PMO additions: `bf:Ensemble`, `bf:EnsembleSize`, `bf:Mode`, `bf:Tempo`, `bf:DramaticRole`, `bf:MediumComponentQualifier`) have no BFFI-namespace equivalents in BFFI 1.0.0. The mapping doc's [Music-medium and music-key routing](../../bf_to_bffi_mapping.md#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) collapses them into the existing literal-carrier vocabulary:

- Medium-of-performance → `bffi:musicMedium` → `bffi:MusicMedium` → `bffi:readMarc382` literal (verbatim MARC 382).
- Music key → `bffi:musicKey` literal (`owl:equivalentProperty bf:musicKey`).

**This routing is interim, not canonical.** BFFI 1.1.0 (planned, per NLF roadmap) will align with BIBFRAME 3.0's December 2025 PMO absorption and add BFFI-namespace equivalents on the established re-anchor pattern: `bffi:MediumOfPerformance owl:equivalentClass bf:MediumOfPerformance`, `bffi:MediumComponent`, `bffi:Ensemble`, `bffi:EnsembleSize`, `bffi:KeyMode`, `bffi:Mode`, `bffi:Tempo`, `bffi:DramaticRole`, `bffi:MediumComponentQualifier`. When BFFI 1.1.0 ships, the literal-collapse routing will be replaced with a structured PMO-shaped chain.

Implementation against BFFI 1.0.0: emit the literal-collapse shape from the mapping doc.

Future work (after BFFI 1.1.0 ships): replace the `bffi:readMarc382` literal emit with the structured `bffi:MediumOfPerformance` / `bffi:mediumComponent` / `bffi:Ensemble` tree; replace the `bffi:musicKey` literal with the structured `bffi:KeyMode` block. Source MARC stays pristine; no data migration needed because canonical Turtle is rebuilt from MARCXML each run.

Risk for Phase 5: low against BFFI 1.0.0 (the literal-collapse is identity round-trip). The BFFI-1.1.0 transition is the structured rewrite, scoped as a follow-on plan when the release lands.

## Decisions locked in

- **Transition: hard cut.** The rename ships as a single transition — from one release the BFFI emit carries `bffi:*` only, with no parallel `bf:*` emit window. Rationale: NLF guidance is explicit ("NO bf entities should be used at all"), and a parallel-emit transition would defeat the namespace-discipline goal. Downstream consumers reading the BFFI graph migrate to BFFI-side predicates / classes; the BIBFRAME view stays recoverable by OWL inference through the re-anchor pattern.

## Open questions for NLF

Confirmation and per-context defaults — not term additions (except the music branch, where the term additions land with BFFI 1.1.0 independently of this plan).

1. **`bf:Series` axis default** (Phase 3) — when MARC 490/830 produces a series link, should BFFI default to `bffi:SeriesWork` or `bffi:SeriesExpression`? The mapping doc recommends `bffi:SeriesExpression` for the common Helmet case (a localised Expression-in-series). NLF confirmation would lock the default.

2. **Per-routing confirmation** (Phase 4) — each row in the Phase 4 table represents a discriminator-routed canonical shape (Hub by marcKey, Identifier-scheme by LoC URI, Title-variant by marcKey tag, Series-link via structured relation, Audio by marc2bibframe2 emit convention). NLF review can flag any routing that diverges from BFFI's design intent; if a routing must change, the alternative would still need to use existing BFFI vocabulary.

3. **BFFI 1.1.0 timing** (Phase 5) — when is BFFI 1.1.0 expected to ship with the BIBFRAME 3.0 PMO classes? This determines whether the interim literal-collapse for music ships indefinitely against BFFI 1.0.0 or has a near-term replacement. Either is fine for this plan; the question is for follow-on planning.

## Verification

**rdflib-based mapping audit** — the script below regenerates the mapping doc's Classes / Predicates tables and the re-anchor cluster trees from `docs/lkd.rdf`. Re-run whenever the ontology changes; treat any new **GAP** row as an unresolved-routing alert.

```python
# scripts/scan_bffi_bf_mappings.py (proposed; not yet committed)
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDFS

BFFI = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")
BF = Namespace("http://id.loc.gov/ontologies/bibframe/")

# IMPORTANT: bffi-meta:* uses a colon separator, NOT a hash. Constructing
# the namespace with Namespace("…/bffi-meta#") silently mis-aliases every
# *Match URI and the scan reports false gaps for the axis-split classes.
BFFI_META_PREFIX = "http://urn.fi/URN:NBN:fi:schema:bffi-meta:"

g = Graph()
g.parse("docs/lkd.rdf", format="xml")

# For each bf:* term referenced in sparql/*.rq CONSTRUCTs, look up every
# bffi:* relation in lkd.rdf, walking BOTH directions for:
#   owl:equivalentClass / equivalentProperty
#   rdfs:subClassOf / subPropertyOf (the re-anchor and predicate-tighten
#                                    patterns)
#   bffi-meta:broadMatch / closeMatch / exactMatch / narrowMatch
#                                    (use BFFI_META_PREFIX above)
#   owl:sameAs
# Print one row per (bf_term, link_kind, bffi_term) triple. Any bf:*
# token with zero rows is a routing gap.
```

**Closed-namespace regression test** — extend `tests/unit/test_bffi_namespace_discipline.py` (today: scans `bffi:*` references in `vocab.py` and `sparql/*.rq`, asserts each exists in `lkd.rdf`) to also assert that **no `bf:*` URI** appears in any `sparql/*.rq` CONSTRUCT clause or in `src/bffi_pipeline/provenance/vocab.py`'s emit set. This is the regression gate that prevents `bf:*` re-introduction after the rename.

**Round-trip diff** — the `marckey-bypass` diff status from P-49 Phase A flags rows reconstructed via `bflc:marcKey`. After Phase 4 the converter's routing reads BFFI-side predicates instead of `bf:*` typing; the `marckey-bypass` counts should not increase. After Phase 5 (music interim) the MARC 382 / 384 rows stay `marckey-bypass` until BFFI 1.1.0 lands.

Future revisions of the mapping doc and this proposal MUST start from a fresh rdflib parse, not from cached tables. Mapping data drifts whenever `lkd.rdf` is updated; in particular, BFFI 1.1.0 absorbing the BIBFRAME 3.0 PMO classes will change the Phase 5 routing decisions.

## Risks

- **Round-trip fidelity** — Phase 4 affects how `bf:Hub`'s replacement renders in MARC 240/730/740 round-trip. The current converter pass reads `bf:Hub` typing as a routing key; the replacement reads `bffi:marcKey` first-3-chars instead.
- **Skosmos rendering** — many M10 display passes walk specific `bf:X` types (the genid-leak fixes from P-55 work). Each pass needs to walk the new BFFI type (or only).
- **External consumers** — if anyone downstream is reading the BFFI emit and expecting `bf:Hub`-shaped data (NLF's own tooling? cataloguer scripts?), the rename breaks them. The transition-window question is for NLF.
- **Test fixtures** — ~50+ unit tests assert specific `bf:*` triples on M3 output. Each needs the rename.

## Rollback

Pure rename. `git revert` restores `bf:*` emit at every phase. No data migration needed — the canonical graph is per-run, rebuilt from MARCXML each invocation.

## Suggested next step

Three items in parallel:

1. **NLF conversation** — take the open questions to NLF (axis default, transition window, per-routing confirmations, BFFI 1.1.0 timing). Ideal channel: the same conversation that triggered this proposal.
2. **Phase 1 prototype** (no NLF dependency) — land the clean-equivalence renames on a feature branch (or direct-to-main if confident) and confirm tests pass + the 500-record canonical still loads into Skosmos correctly. Validates the mechanical approach.
3. **Closed-namespace test extension** — extend `tests/unit/test_bffi_namespace_discipline.py` to forbid `bf:*` in `sparql/*.rq` CONSTRUCT clauses and `provenance/vocab.py` emit. Ship before Phase 1 so the regression gate is in place.

Implementation order after NLF responds: Phase 1 (clean rename) → Phase 4 (discriminator routings) → Phase 3 (axis-pick defaults) → Phase 2 (broadMatch per-case) → Phase 5 (music interim). BFFI 1.1.0 follow-on (structured PMO replacement) is a separate plan when the release lands.
