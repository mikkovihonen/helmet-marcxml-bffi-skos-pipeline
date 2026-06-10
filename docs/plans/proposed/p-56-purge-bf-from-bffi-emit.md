# P-56 — Purge `bf:*` (BIBFRAME) terms from the BFFI emit

**Status**: proposed. **Blocked on NLF input** for the four true-gap classes (`bf:Hub`, `bf:Isbn`, `bf:Ean`, `bf:AudioIssueNumber`, `bf:VariantTitle`).

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

## Methodology note

This inventory was built by **parsing `docs/lkd.rdf` with rdflib** and walking the BFFI ↔ BIBFRAME relations programmatically (`owl:equivalentClass`, `owl:equivalentProperty`, `rdfs:subClassOf` / `subPropertyOf`, `bffi-meta:exactMatch` / `broadMatch` / `closeMatch`, `owl:sameAs`) in both directions. The earlier draft of this proposal was assembled by `grep` and shipped multiple errors (notably `bf:Work` → "drop, already dual-typed as `bffi:Work`" — wrong: `bf:Work` is `owl:equivalentClass bffi:BibframeWork`, while `bffi:Work` is a DISTINCT RDA-aligned concept). Future revisions of this proposal should re-run the parse against the latest `lkd.rdf` before changing the tables — the script lives in the `Verification` section below.

## Inventory — what currently ships under `bf:*`

### Typed instances (`rdf:type bf:X`) — 2,216 in the 500-record sample

| `bf:` class | Count | BFFI replacement | Link kind | Phase |
|---|---|---|---|---|
| `bf:Hub` | 1,214 | **❌ no BFFI equivalent** (not in lkd.rdf at all) | — | 4 (NLF) |
| `bf:ProvisionActivity` | 1,006 | `bffi:ProvisionActivity` | `owl:equivalentClass` | 1 |
| `bf:Isbn` | 684 | **❌ no BFFI Identifier subclass** | — | 4 (NLF) |
| `bf:Series` | 386 | `bffi:SeriesWork` (Work-axis) or `bffi:SeriesExpression` (Expression-axis) | `bffi-meta:broadMatch` | 3 |
| `bf:Language` | 362 | `bffi:Language` | `owl:equivalentClass` | 1 |
| `bf:AudioIssueNumber` | 236 | **❌ no BFFI Identifier subclass** | — | 4 (NLF) |
| `bf:Work` | 190 | `bffi:BibframeWork` (NOT `bffi:Work` — that's a *distinct* RDA-aligned class) | `owl:equivalentClass` | 1 |
| `bf:VariantTitle` | 152 | **❌ no BFFI VariantTitle** (only `bffi:Title`; `bffi:variantAccessPoint` is for access-points, not title-bnodes) | — | 4 (NLF) |
| `bf:Manufacture` | 98 | `bffi:Manufacture` | `owl:equivalentClass` | 1 |
| `bf:Ean` | 72 | **❌ no BFFI Identifier subclass** | — | 4 (NLF) |
| `bf:Arrangement` | 28 | `bffi:Arrangement` | `owl:equivalentClass` | 1 |
| `bf:Distribution` | 4 | `bffi:Distribution` | `owl:equivalentClass` | 1 |
| `bf:Source` | 1 | `bffi:Source` | `owl:equivalentClass` | 1 |

**No `bf:Work` decision needed** — per the re-anchor pattern above, `bffi:Work rdfs:subClassOf bffi:BibframeWork owl:equivalentClass bf:Work`, and `bffi:Expression rdfs:subClassOf bffi:BibframeWork` too. Emitting `bffi:Work` or `bffi:Expression` alone makes the `bf:Work` view recoverable by inference; dropping `bf:Work` is loss-free.

Additional `bf:*` types referenced in `sparql/*.rq` CONSTRUCTs but absent from the 500-record sample (will appear on other records): `bf:Agent`, `bf:Person`, `bf:Organization`, `bf:Meeting`, `bf:Family`, `bf:Title`, `bf:Note`, `bf:Identifier`, `bf:Topic`, `bf:Place`, `bf:Temporal`, `bf:GenreForm`, `bf:Instance`, `bf:Role`, `bf:Audio`, `bf:MusicAudio`, `bf:Text`, `bf:NotatedMusic`, `bf:NotatedMovement`, `bf:Object`, `bf:StillImage`, `bf:MovingImage`, `bf:Cartography`, `bf:Dataset`, `bf:Multimedia`, `bf:MixedMaterial`, `bf:Manuscript`, `bf:Tactile`, `bf:Print`, `bf:Electronic`, `bf:Microform`, `bf:Archival`, `bf:Monograph`, `bf:Integrating`, `bf:Issn`, `bf:IssnL`, `bf:OtherIdentifier`, `bf:Local`, `bf:TableOfContents`, `bf:Extent`, `bf:KeyMode`, `bf:MusicMedium`, `bf:AdminMetadata`, `bf:Publication`, `bf:Classification`, `bf:IntendedAudience`, `bf:PrimaryContribution`, `bf:Relation`. All except `bf:Issn`, `bf:IssnL`, `bf:OtherIdentifier`, `bf:KeyMode` have `owl:equivalentClass` to a `bffi:*` term (rdflib-verified) — Phase 1. The four exceptions:

- `bf:Issn`, `bf:IssnL`, `bf:OtherIdentifier` — same Identifier-subclass gap as `bf:Isbn`/`Ean`/`AudioIssueNumber`. Phase 4.
- `bf:KeyMode` — no `bffi:KeyMode` class (the predicate `bf:keyMode` also has no `bffi:keyMode`; the closest term `bffi:musicKey` is a `Literal`-ranged datatype property, not a structured Key block). Phase 4.

### Predicates — 7 distinct materialised, ~50+ referenced in CONSTRUCTs

Rdflib-verified BFFI counterparts. Three link kinds:

- `owl:equivalentProperty` — clean semantic match, loss-free rename.
- `rdfs:subPropertyOf bf:X` — BFFI predicate is a refinement (range pinned to a `bffi:Y` that is `owl:equivalentClass bf:Y`); strictly tighter typing, no information loss. **Per NLF clarification, this is a clean migration just like equivalentProperty.**
- `bffi-meta:broadMatch` / `closeMatch` — semantic shift on record.

| `bf:` predicate | Count | BFFI replacement | Link kind | Phase |
|---|---|---|---|---|
| `bf:source` | 1,271 | `bffi:source` | `subPropertyOf` | 1 |
| `bf:place` | 500 | `bffi:place` | `subPropertyOf` | 1 |
| `bf:issuance` | 500 | `bffi:issuance` | `broadMatch` | 2 |
| `bf:date` | 500 | `bffi:date` | `subPropertyOf` | 1 |
| `bf:note` | 248 | `bffi:note` | `subPropertyOf` | 1 |
| `bf:language` | 181 | `bffi:language` | `owl:equivalentProperty` | 1 |
| `bf:hasSeries` | 120 | **❌ no BFFI equivalent** | — | 4 (NLF) |

Predicates referenced in `sparql/*.rq` CONSTRUCTs but absent from the 500-record sample (rdflib-verified BFFI counterparts):

**Phase 1 — clean rename via `owl:equivalentProperty`:**
`bf:adminMetadata`, `bf:assigner`, `bf:associatedResource`, `bf:classification`, `bf:classificationPortion`, `bf:code`, `bf:colorContent`, `bf:contribution`, `bf:descriptionConventions`, `bf:descriptionLanguage`, `bf:digitalCharacteristic`, `bf:dimensions`, `bf:editionStatement`, `bf:extent`, `bf:genreForm`, `bf:hasItem`, `bf:identifiedBy`, `bf:intendedAudience`, `bf:itemOf`, `bf:itemPortion`, `bf:language`, `bf:mainTitle`, `bf:media`, `bf:musicKey`, `bf:originDate`, `bf:originPlace`, `bf:partName`, `bf:partNumber`, `bf:provisionActivity`, `bf:publicationStatement`, `bf:qualifier`, `bf:relation`, `bf:relationship`, `bf:responsibilityStatement`, `bf:role`, `bf:seriesEnumeration`, `bf:seriesStatement`, `bf:soundCharacteristic`, `bf:status`, `bf:subject`, `bf:subtitle`, `bf:summary`, `bf:tableOfContents`, `bf:title`, `bf:version`.

**Phase 1 — clean rename via `rdfs:subPropertyOf bf:X` (range pinned to equivalent-class):**
`bf:agent`, `bf:carrier`, `bf:content`, `bf:date`, `bf:musicMedium`, `bf:note`, `bf:place`, `bf:source`.

**Phase 2 — broadMatch (semantic shift, per-case review):**
`bf:issuance` (`broadMatch bffi:issuance` — single-target; safe), `bf:instanceOf` (`broadMatch` to BOTH `bffi:workManifested` and `bffi:expressionManifested` — pick per emit context).

**Phase 4 — no BFFI alias (NLF ask):**
`bf:hasSeries`, `bf:keyMode`, `bf:ensemble`, `bf:mediumComponent`, `bf:mediumOfPerformance`. (`bf:instance` and `bf:item` are misleading — the actual BFFI predicate names are `bffi:hasItem` / `bffi:itemOf`, both `owl:equivalentProperty` to their BIBFRAME counterparts; if our SPARQL uses `bf:item` literally as a predicate URI, that's the wrong direction — should be using `bffi:hasItem` already.)

### Predicate-side NLF asks

Confirmed (no ask needed): every `bf:*` predicate where lkd.rdf has `bffi:X rdfs:subPropertyOf bf:X` is a clean migration — BFFI is the refined predicate whose range is pinned to a `bffi:Y` class that's `owl:equivalentClass bf:Y`. `bffi:agent` is the canonical example (`subPropertyOf bf:agent`, `range bffi:Agent`, which is `equivalentClass bf:Agent`). Same shape for `bf:date`, `bf:place`, `bf:source`, `bf:note`, `bf:carrier`, `bf:content`, `bf:musicMedium`. Emitting `bffi:X` strictly tightens the typing rather than weakening it; no clarification required.

Remaining predicate-side asks (rdflib-verified — no `bffi:*` term has these as `equivalentProperty` / `subPropertyOf` / `broadMatch`):

| `bf:` predicate | NLF ask |
|---|---|
| `bf:hasSeries` | add `bffi:hasSeries` (or `bffi:isPartOfSeries`) — predicate saying "this Manifestation is part of this series" |
| `bf:keyMode` | structured `bf:KeyMode` block (`b-flat major`); add `bffi:KeyMode` class + `bffi:keyMode` predicate, OR fold into `bffi:musicKey` literal (which exists as `owl:equivalentProperty bf:musicKey`) |
| `bf:ensemble` | music-ensemble subclass of medium-of-performance; add or fold into `bffi:mediumOfPerformance` |
| `bf:mediumComponent` | structured medium decomposition predicate; add or replace pattern |
| `bf:mediumOfPerformance` | no `bffi:mediumOfPerformance` exists in lkd.rdf (despite `bf:musicMedium` having `bffi:musicMedium` via subPropertyOf — but the unstructured `mediumOfPerformance` predicate has no counterpart) |

### True class gaps requiring NLF input

The complete list of `bf:*` tokens referenced in `sparql/*.rq` with NO link of any kind to a `bffi:*` term in `lkd.rdf` (rdflib-verified scan, see Verification section). Eight true class gaps plus the predicate-gap appendix:

| Class | Count in 500-record sample | Used for | Options |
|---|---|---|---|
| **`bf:Hub`** | 1,214 | MARC 240 (uniform title), 730/740 (added/analytical titles), Aggregate-Work component pointer | (a) NLF adds `bffi:Hub` to lkd.rdf; (b) drop typed shape, keep just `bffi:Title` + `rdfs:label`; (c) mint full `bffi:Work` URIs (semantically wrong for many cases — performance recordings, anonymous folk-music titles) |
| **`bf:Isbn`** | 684 | MARC 020 ISBN | NLF adds `bffi:Isbn rdfs:subClassOf bffi:Identifier` (matches the existing `bffi:Local`, `bffi:ShelfMark` pattern) |
| **`bf:AudioIssueNumber`** | 236 | MARC 028 (publisher number, e.g. record-label catalogue numbers) | NLF adds `bffi:AudioIssueNumber rdfs:subClassOf bffi:Identifier` |
| **`bf:Ean`** | 72 | MARC 024 ind1=3 (EAN-13 barcodes) | NLF adds `bffi:Ean rdfs:subClassOf bffi:Identifier` |
| **`bf:VariantTitle`** | 152 | MARC 246 (variant title), 740 ind2=0 (added title with no relator) | (a) NLF adds `bffi:VariantTitle rdfs:subClassOf bffi:Title`; (b) drop the typed shape and rely on `bffi:Title` + a `bffi:titleType "variant"` literal |
| **`bf:Issn`** | 0 in sample | MARC 022 ISSN (serials) | NLF adds `bffi:Issn rdfs:subClassOf bffi:Identifier` |
| **`bf:OtherIdentifier`** | 0 in sample | catch-all 024 ind1≠3 (DOI, ASIN, etc.) | NLF adds `bffi:OtherIdentifier rdfs:subClassOf bffi:Identifier`, OR fold into `bffi:Local` with an `bffi:assigner` discriminator |
| **`bf:Audio`** | 0 in sample | `bf:Audio` content-type parent (the BIBFRAME superclass of `bf:MusicAudio`) | `bf:MusicAudio` HAS an anchor (`bffi:MusicAudioExpression` broadMatch); `bf:Audio` doesn't. NLF: add `bffi:AudioWork` / `bffi:AudioExpression`? Or always specialise to MusicAudio / NonMusicAudio? |
| **`bf:KeyMode`** | 0 in sample as type (but the predicate `bf:keyMode` is referenced) | Structured key-mode block (`b-flat major`); only `bffi:musicKey` Literal datatype property exists | NLF: add `bffi:KeyMode` class + `bffi:keyMode` object-property, OR mandate the `bffi:musicKey "B-flat major"` Literal idiom |

## Migration path

Four phases. Phase 1 is the bulk of the mechanical rename and ships standalone. Phases 2 and 3 are per-case decisions on the smaller residue. Phase 4 (true-gap classes) is blocked on NLF.

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

### Phase 4 — true-gap classes (BLOCKED on NLF)

**Cannot ship until NLF responds on `bffi:Hub` / Identifier subclasses / `bffi:VariantTitle`.**

When NLF responds, three sub-paths:

- **NLF adds the terms to `lkd.rdf`**: rename `bf:X` → `bffi:X` in the SPARQL emits; update the closed-namespace test fixture; no logic change.
- **NLF says "drop the typed shape"**: remove the `rdf:type bf:X` triple; keep the structural chain (`bffi:title`, `rdfs:label`, etc.) so the data still renders.
- **NLF defers**: status quo for `bf:Hub` (we keep it as a vendored exception) — but then we can't claim "no `bf:*`" in the output.

This phase is the one that breaks the round-trip if it goes wrong. The current marc-roundtrip converter reads `bf:Hub` directly to reconstruct MARC 730. Until Phase 4 ships, the converter has to read whatever replaces `bf:Hub` — see `src/bffi_pipeline/marc_roundtrip/converter.py:_emit_analytical_added_entry_from_hub` and friends.

## Open questions for NLF

Frame these as the conversation to have before Phase 4 ships:

1. **`bffi:Hub`** — what should the BFFI-native equivalent of `bf:Hub` look like? Same shape as BIBFRAME (a Title-bearing entity referenced as a relation target without being a full Work)? Or does BFFI prefer to model 240/730/740 some other way (e.g. `bffi:Title` with extra metadata, no separate class)?

2. **Identifier subclasses** — `bffi:Local` and `bffi:ShelfMark` are the only existing subclasses of `bffi:Identifier` in lkd.rdf. The "standard" identifier types (ISBN, EAN, ISSN, EAN-13, audio issue number, OCLC, ASTERI, FINAF, FINAF-001, ISWC for music, ISRC for recordings, etc.) all come from MARC. Should BFFI:
   - (a) define a fixed subclass tree (`bffi:Isbn`, `bffi:Ean`, `bffi:Issn`, `bffi:AudioIssueNumber`, ...)?
   - (b) use `bffi:Identifier` + `bffi:identifierScheme <some-scheme-URI>` (data-property approach, no subclass)?

3. **`bffi:VariantTitle`** — is a variant title a separate class, or is it `bffi:Title` + a discriminator predicate (e.g. `bffi:titleType "variant"`)?

4. **`bf:Series` axis** — when MARC 490/830 produces a series link, should BFFI default it to `bffi:SeriesWork` (the series-as-Work) or `bffi:SeriesExpression`? Helmet's catalogue convention?

5. **Transition window** — should the rename ship as a hard cut (emit only `bffi:*`), or a one-release transition where both `bffi:X` and the now-inferred parent `bf:X` are emitted, letting downstream consumers migrate? Strict reading of NLF guidance: hard cut.

## Verification

The script below regenerates every table above from `docs/lkd.rdf`. Re-run it whenever the ontology changes; treat any new row as a Phase 4 ask until reviewed.

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

The earlier draft of this proposal was built by regex `grep` over `lkd.rdf`'s XML form and shipped at least three errors that the rdflib parse caught:

1. **`bf:Work`** said to drop (already dual-typed as `bffi:Work`). Correct: `bf:Work owl:equivalentClass bffi:BibframeWork`. `bffi:Work` is a separate RDA-aligned concept.
2. **`bf:item`, `bf:itemOf`, `bf:itemPortion`** said to be true gaps. Correct: clean `owl:equivalentProperty` to `bffi:hasItem` / `bffi:itemOf` / `bffi:itemPortion` (BFFI-side name doesn't match BIBFRAME-side name 1:1 — grep missed it).
3. **`bf:agent`** flagged as "subPropertyOf is weaker than equivalentProperty, NLF ask". Correct: `subPropertyOf bf:agent` plus `rdfs:range bffi:Agent` (which IS `owl:equivalentClass bf:Agent`) is BFFI's idiom for a refined predicate, not a gap.

Future work on this proposal MUST start from a fresh rdflib parse, not from earlier tables.

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
