# `bf:*` → `bffi:*` mapping reference

Generated programmatically by parsing `docs/lkd.rdf` with rdflib and walking every relation between the BFFI and BIBFRAME namespaces (`owl:equivalentClass`, `owl:equivalentProperty`, `rdfs:subPropertyOf`, the re-anchor `subClassOf` pattern, and the `bffi-meta:` match relations). Scope: every `bf:*` token referenced in `sparql/*.rq` (the CONSTRUCT clauses producing canonical BFFI). Methodology + companion proposal live in `docs/plans/proposed/p-56-purge-bf-from-bffi-emit.md`.

**Re-anchor pattern**: when a `bffi:*` class has subclasses (`bffi:Sub rdfs:subClassOf bffi:Anchor`) and the anchor is `owl:equivalentClass bf:X`, emitting any subclass alone makes the BIBFRAME class recoverable by OWL inference. No need to dual-type with `bf:*`.

**Status legend**: **clean** = direct rename or re-anchor (Phase 1 of p-56) · *semantic-shift* = `broadMatch` / `closeMatch` only (Phase 2/3) · **routed** = no single BFFI replacement; the per-instance data determines which existing `bffi:*` class applies (see callout sections below the relevant table) · **GAP** = no link of any kind, requires NLF input (Phase 4).

**Also satisfies column** (per-row inheritance closure): emitting the row's BFFI replacement makes the listed BIBFRAME classes recoverable by OWL inference (walked from `bffi:Y` upward through `rdfs:subClassOf` and counting every BFFI ancestor with `owl:equivalentClass bf:*`). Empty when there's no inheritance chain beyond the direct equivalence.

## Classes (sorted alphabetically)

| `bf:` class | Status | `bffi:*` replacement(s) | Link kind | Also satisfies (via inference) | Used in |
|---|---|---|---|---|---|
| `bf:Archival` | **clean** | `bffi:Archival` | owl:equivalentClass | `bf:Instance` | manifestation |
| `bf:Arrangement` | **clean** | `bffi:Arrangement` | owl:equivalentClass | `bf:Work` | expression, manifestation |
| `bf:Audio` | **GAP** | — | — | — | expression, work |
| `bf:AudioIssueNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/audioIssueNumber>` (see [Identifier-scheme routing](#identifier-scheme-routing-bfisbn--bfissn--bfean--bfaudioissuenumber--bfotheridentifier--bffiidentifier--bffisource) below) | n/a — no direct `bffi:AudioIssueNumber`; route by `bffi:source` URI | `bf:Identifier` (via `bffi:Identifier ≡ bf:Identifier`) | manifestation |
| `bf:Cartography` | *semantic-shift* | `bffi:CartographyExpression, bffi:CartographyWork` | bffi-meta:broadMatch | `bf:Work` | expression, work |
| `bf:Dataset` | **clean** | `bffi:Dataset` | owl:equivalentClass | `bf:Work` | expression, work |
| `bf:Ean` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/ean>` (see [Identifier-scheme routing](#identifier-scheme-routing-bfisbn--bfissn--bfean--bfaudioissuenumber--bfotheridentifier--bffiidentifier--bffisource) below) | n/a — no direct `bffi:Ean`; route by `bffi:source` URI | `bf:Identifier` | manifestation |
| `bf:Electronic` | **clean** | `bffi:Electronic` | owl:equivalentClass | `bf:Instance` | manifestation |
| `bf:Hub` | **routed** | `bffi:Work` *or* `bffi:Expression` (per-instance — see [Hub routing](#hub-routing-bfhub--bffiexpression-vs-bffiwork) below) | n/a — no direct `bffi:Hub`; route by FRBR axis signal | `bf:Work` (via `bffi:Work ⊑ bffi:BibframeWork ≡ bf:Work` either way) | aggregation, expression, manifestation, work |
| `bf:Instance` | **clean** | `bffi:Manifestation`<br>`bffi:Archival, bffi:CollectionManifestation, bffi:Electronic, bffi:Microform, bffi:Print, bffi:Tactile`<br>`bffi:CollectionManifestation` | bffi-meta:broadMatch<br>owl:equivalentClass<br>re-anchor (subClassOf Manifestation) | — | manifestation |
| `bf:Integrating` | **clean** | `bffi:Integrating` | owl:equivalentClass | `bf:Work` | work |
| `bf:Isbn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/isbn>` (see [Identifier-scheme routing](#identifier-scheme-routing-bfisbn--bfissn--bfean--bfaudioissuenumber--bfotheridentifier--bffiidentifier--bffisource) below) | n/a — no direct `bffi:Isbn`; route by `bffi:source` URI | `bf:Identifier` | manifestation |
| `bf:Issn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/issn>` (see [Identifier-scheme routing](#identifier-scheme-routing-bfisbn--bfissn--bfean--bfaudioissuenumber--bfotheridentifier--bffiidentifier--bffisource) below) | n/a — no direct `bffi:Issn`; route by `bffi:source` URI | `bf:Identifier` | manifestation |
| `bf:KeyMode` | **GAP** | — | — | — | expression, manifestation |
| `bf:Language` | **clean** | `bffi:Language` | owl:equivalentClass | — | expression, manifestation |
| `bf:Local` | **clean** | `bffi:Local` | owl:equivalentClass | `bf:Identifier` | manifestation |
| `bf:Manuscript` | **clean** | `bffi:Manuscript` | owl:equivalentClass | `bf:Work` | work |
| `bf:Meeting` | **clean** | `bffi:Meeting` | owl:equivalentClass | `bf:Agent` | work |
| `bf:Microform` | **clean** | `bffi:Microform` | owl:equivalentClass | `bf:Instance` | manifestation |
| `bf:MixedMaterial` | **clean** | `bffi:MixedMaterial`<br>`bffi:Kit` | owl:equivalentClass<br>re-anchor (subClassOf MixedMaterial) | `bf:Work` | expression, work |
| `bf:MovingImage` | *semantic-shift* | `bffi:MovingImageExpression, bffi:MovingImageWork` | bffi-meta:broadMatch | `bf:Work` | expression, work |
| `bf:Multimedia` | **clean** | `bffi:Multimedia` | owl:equivalentClass | `bf:Work` | expression, work |
| `bf:MusicAudio` | *semantic-shift* | `bffi:MusicAudioExpression`<br>`bffi:MusicWork` | bffi-meta:broadMatch<br>bffi-meta:closeMatch | `bf:Work` | expression, work |
| `bf:MusicMedium` | **clean** | `bffi:MusicMedium`<br>`bffi:ChoreographicMedium` | bffi-meta:closeMatch<br>owl:equivalentClass | — | expression, manifestation |
| `bf:NotatedMovement` | **clean** | `bffi:NotatedMovement` | owl:equivalentClass | `bf:Work` | expression, work |
| `bf:NotatedMusic` | **clean** | `bffi:NotatedMusic`<br>`bffi:MusicWork` | bffi-meta:closeMatch<br>owl:equivalentClass | `bf:Work` | expression, work |
| `bf:Note` | **clean** | `bffi:Note`<br>`bffi:TitleNote`<br>`bffi:TitleNote` | bffi-meta:broadMatch<br>owl:equivalentClass<br>re-anchor (subClassOf Note) | — | expression, manifestation |
| `bf:Object` | **clean** | `bffi:Object` | owl:equivalentClass | `bf:Work` | expression, work |
| `bf:Organization` | **clean** | `bffi:Organization` | owl:equivalentClass | `bf:Agent` | work |
| `bf:OtherIdentifier` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/<scheme>>` (see [Identifier-scheme routing](#identifier-scheme-routing-bfisbn--bfissn--bfean--bfaudioissuenumber--bfotheridentifier--bffiidentifier--bffisource) below) | n/a — catch-all for non-standard 024 ind1≠3 identifiers | `bf:Identifier` | manifestation |
| `bf:Person` | **clean** | `bffi:Person` | owl:equivalentClass | `bf:Agent` | work |
| `bf:Place` | **clean** | `bffi:Place` | owl:equivalentClass | — | work |
| `bf:PrimaryContribution` | **clean** | `bffi:PrimaryContribution` | owl:equivalentClass | `bf:Contribution` | expression, work |
| `bf:Print` | **clean** | `bffi:Print` | owl:equivalentClass | `bf:Instance` | manifestation |
| `bf:Series` | *semantic-shift* | `bffi:SeriesExpression, bffi:SeriesWork` | bffi-meta:broadMatch | `bf:Work` | manifestation |
| `bf:StillImage` | **clean** | `bffi:StillImage` | owl:equivalentClass | `bf:Work` | expression, work |
| `bf:Tactile` | **clean** | `bffi:Tactile` | owl:equivalentClass | `bf:Instance` | manifestation, work |
| `bf:Temporal` | **clean** | `bffi:Temporal` | owl:equivalentClass | — | work |
| `bf:Text` | **clean** | `bffi:Text` | owl:equivalentClass | `bf:Work` | expression, work |
| `bf:Title` | **clean** | `bffi:Title` | owl:equivalentClass | — | expression, manifestation |
| `bf:Topic` | **clean** | `bffi:Topic` | owl:equivalentClass | — | work |
| `bf:VariantTitle` | **GAP** | — | — | — | expression |
| `bf:Work` | **clean** | `bffi:BibframeWork`<br>`bffi:Expression, bffi:Work`<br>`bffi:AggregatingExpression, bffi:AggregatingWork, bffi:Expression, bffi:Work` | bffi-meta:broadMatch<br>owl:equivalentClass<br>re-anchor (subClassOf BibframeWork) | — | aggregation, expression, manifestation, work |

### Hub routing — `bf:Hub` → `bffi:Expression` vs `bffi:Work`

`lkd.rdf` has no `bffi:Hub`. But `bf:Hub` isn't a single semantic — it's BIBFRAME's flat aggregation of two FRBR/RDA-distinct things: a Work-axis reference (e.g. "Beethoven · Symphony no. 5") vs an Expression-axis reference (e.g. "English translation of *À la recherche*"). BFFI separates them. So `bf:Hub` routes to a `bffi:Work`-side or `bffi:Expression`-side class **based on what facets the source MARC entry carries**.

Routing table (read top-to-bottom; first match wins):

| Source MARC signal on the Hub | Routed `bffi:*` type | Why |
|---|---|---|
| MARC `$o` arrangement statement, OR `bf:Arrangement` in `rdf:type` set | `bffi:Arrangement` | `bffi:Arrangement` is `owl:equivalentClass bf:Arrangement` AND `rdfs:subClassOf bffi:Expression` — perfect direct match; arrangements are Expression-level by definition |
| MARC `$m` medium of performance + content-type signal "audio recording" | `bffi:MusicAudioExpression` | Expression-axis content-type-specific subclass of `bffi:Expression` |
| MARC `$m` medium-of-performance + content-type signal "notated music" | `bffi:NotatedMusic` (`owl:equivalentClass bf:NotatedMusic`) | Expression-axis subclass of `bffi:Expression` |
| MARC `$l` language qualifier (the dominant Expression signal) | `bffi:Expression` (+ `bffi:languageOfExpression` triple carrying the language URI) | `$l` names the language OF an Expression; the Hub IS that Expression |
| MARC `$r` key (`bf:keyMode`) | `bffi:Expression` (+ `bffi:musicKey` literal) | Key is an Expression-level attribute |
| MARC `$s` version (`bffi:version`) | `bffi:Expression` (+ `bffi:version`) | Version is an Expression-level attribute |
| Source field is MARC 100/700 with `$t` (author-attributed uniform title) | `bffi:Work` | The cataloguer named a Work via the author entry, not an Expression; treat as Work-level |
| Source field is MARC 130/830 (series uniform title) | `bffi:SeriesWork` *or* `bffi:SeriesExpression` (axis-pick per p-56 Phase 3) | Series entry; axis follows the per-record context |
| Source field is MARC 730/740 with `$a` title only (no facets above) | `bffi:Work` | Plain transcribed title with no Expression signal — cataloguer named a Work |
| Otherwise (fallback) | `bffi:Work` | Absent any Expression-level signal, default to the Work axis |

Distribution in the 500-record sample (621 total `bf:Hub` instances):

| Routing outcome | Count | % |
|---|---|---|
| Routed to `bffi:Expression` (language / arrangement / key / medium / version) | 191 | 30.8% |
| Routed to `bffi:Work` (no Expression signal) | 430 | 69.2% |

What survives the migration without NLF input:

- **Zero new BFFI terms required.** Every routed type already exists in `lkd.rdf` (`bffi:Work`, `bffi:Expression`, `bffi:Arrangement`, `bffi:MusicAudioExpression`, `bffi:NotatedMusic`, `bffi:SeriesWork`, `bffi:SeriesExpression`, `bffi:languageOfExpression`, `bffi:musicKey`, `bffi:version`).
- **`bf:Work` recovery via inference**: both routes pass through the `bffi:BibframeWork ≡ bf:Work` anchor — `bffi:Work ⊑ bffi:BibframeWork ≡ bf:Work` AND `bffi:Expression ⊑ bffi:BibframeWork ≡ bf:Work`. BIBFRAME consumers reading the canonical see a `bf:Work`-shaped target either way (which is correct — BIBFRAME's `bf:Hub` is itself a `bf:Work`-shaped grouping).
- **Round-trip integrity**: the Expression-level facets that triggered the routing (`bffi:languageOfExpression`, `bffi:musicKey`, `bffi:version`, `bffi:Arrangement` type) are themselves recoverable via existing BFFI vocabulary — the MARC `$l` / `$o` / `$r` / `$s` subfields can be reconstructed without `bf:Hub` typing.
- **What's lost**: the BIBFRAME-specific "this is a less-rigorously-described entity" signal. BFFI's view is that sparse-description isn't a separate class — it's just a Work or Expression with minimal metadata. The information loss is ontological, not data.

### Identifier-scheme routing — `bf:Isbn` / `bf:Issn` / `bf:Ean` / `bf:AudioIssueNumber` / `bf:OtherIdentifier` → `bffi:Identifier` + `bffi:source`

`lkd.rdf` declares `bffi:Identifier ≡ bf:Identifier` but only two subclasses below it — `bffi:Local` and `bffi:ShelfMark`. The standard MARC identifier types (ISBN, ISSN, EAN, AudioIssueNumber, the catch-all OtherIdentifier) have no BFFI subclass. **Their semantic content lives at the predicate level instead**, via the existing `bffi:source` + `bffi:Source` + `bffi:code` triple structure that BFFI already declares.

The pipeline already uses this pattern for Helmet local identifiers:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Local ;
    rdf:value "b21152068" ;
    bf:source <http://urn.fi/URN:NBN:fi:bib:source/helmet>
] .
```

The standard-scheme replacement keeps the exact same shape with `bffi:Identifier` (the anchor class) at the top and a LoC-vocabulary URI as the scheme:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Identifier ;
    rdf:value "9780123456789" ;
    bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn>
] .
```

Routing table (one row per BIBFRAME identifier subclass we emit; the BIBFRAME type collapses into the `bffi:source` URI):

| BIBFRAME type | MARC source | BFFI emit shape |
|---|---|---|
| `bf:Isbn` | 020 (ISBN-13 / ISBN-10) | `bffi:Identifier` + `bffi:source <…/identifiers/isbn>` |
| `bf:Issn` | 022 (ISSN — serials) | `bffi:Identifier` + `bffi:source <…/identifiers/issn>` |
| `bf:Ean` | 024 ind1=3 (EAN-13 barcode) | `bffi:Identifier` + `bffi:source <…/identifiers/ean>` |
| `bf:AudioIssueNumber` | 028 (publisher number / record-label catalogue number) | `bffi:Identifier` + `bffi:source <…/identifiers/audioIssueNumber>` |
| `bf:OtherIdentifier` | 024 ind1≠3 (DOI / ASIN / Sigel / etc.) | `bffi:Identifier` + `bffi:source <…/identifiers/<scheme>>` (`<scheme>` carried from the `$2` subfield) |

LoC publishes the canonical scheme URIs under `http://id.loc.gov/vocabulary/identifiers/` (it's how BIBFRAME itself encodes its subclass tree via `bf:source` on the data side). Using these URIs as the scheme indicator means a BIBFRAME consumer that previously matched on `?ident a bf:Isbn` can re-target to `?ident bffi:source <…/identifiers/isbn>` without changing the URI vocabulary.

Optional qualifier carryover when MARC `$q` is present:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Identifier ;
    rdf:value "9780123456789" ;
    bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn> ;
    bffi:qualifier "(paperback)"
] .
```

What survives the migration without NLF input:

- **Zero new BFFI terms required.** Every emit term (`bffi:Identifier`, `bffi:identifiedBy`, `bffi:source`, `bffi:qualifier`) is already in `lkd.rdf`.
- **`bf:Identifier` recovery via inference** — `bffi:Identifier ≡ bf:Identifier` is the direct equivalence anchor. BIBFRAME consumers querying "all `bf:Identifier` instances" find them all.
- **Round-trip integrity** — the `bffi:source` URI deterministically maps back to a MARC field + indicators ($2 scheme code, indicator values), so the round-trip converter can reconstruct `020` / `022` / `024` / `028` without `bf:*` class typing.
- **Skosmos rendering** — the `bffi:source` URIs need Finnish / Swedish / English labels in `config/skosmos-config.ttl` (e.g. `"ISBN"@en, "ISBN"@fi, "ISBN"@sv` — already used elsewhere); same pattern as the existing `…/source/helmet` source URI.

What's lost:

- **Type-based SPARQL queries** — `?ident a bf:Isbn` no longer works. Consumers query `?ident bffi:source <…/identifiers/isbn>` instead. Same selectivity, different idiom.
- **BIBFRAME-side `rdfs:subClassOf bf:Identifier` inference** — a BIBFRAME consumer expecting `bf:Isbn ⊑ bf:Identifier` finds `bffi:Identifier ≡ bf:Identifier` directly, but the `bf:Isbn` subtype doesn't materialise. Acceptable trade-off — the scheme code carries the same information at a different axis.

## Predicates (sorted alphabetically)

| `bf:` predicate | Status | `bffi:*` replacement(s) | Link kind | Used in |
|---|---|---|---|---|
| `bf:adminMetadata` | **clean** | `bffi:adminMetadata` | owl:equivalentProperty | manifestation |
| `bf:agent` | **clean** | `bffi:agent` | rdfs:subPropertyOf | expression, manifestation, work |
| `bf:assigner` | **clean** | `bffi:assigner` | owl:equivalentProperty | manifestation |
| `bf:associatedResource` | **clean** | `bffi:associatedResource` | owl:equivalentProperty | aggregation, expression, manifestation, work |
| `bf:carrier` | **clean** | `bffi:carrier` | rdfs:subPropertyOf | manifestation |
| `bf:classification` | **clean** | `bffi:classification` | owl:equivalentProperty | work |
| `bf:classificationPortion` | **clean** | `bffi:classificationPortion` | owl:equivalentProperty | work |
| `bf:code` | **clean** | `bffi:code` | owl:equivalentProperty | expression, manifestation, work |
| `bf:colorContent` | **clean** | `bffi:colorContent` | owl:equivalentProperty | manifestation |
| `bf:content` | **clean** | `bffi:content, bffi:contentOfRepresentativeExpression` | rdfs:subPropertyOf | expression |
| `bf:contribution` | **clean** | `bffi:contribution` | owl:equivalentProperty | expression, work |
| `bf:date` | **clean** | `bffi:date, bffi:dateOfRepresentativeExpression` | rdfs:subPropertyOf | manifestation |
| `bf:descriptionConventions` | **clean** | `bffi:descriptionConventions` | owl:equivalentProperty | manifestation |
| `bf:descriptionLanguage` | **clean** | `bffi:descriptionLanguage` | owl:equivalentProperty | manifestation |
| `bf:digitalCharacteristic` | **clean** | `bffi:digitalCharacteristic` | owl:equivalentProperty | manifestation |
| `bf:dimensions` | **clean** | `bffi:dimensions` | owl:equivalentProperty | manifestation |
| `bf:editionStatement` | **clean** | `bffi:editionStatement` | owl:equivalentProperty | manifestation |
| `bf:ensemble` | **GAP** | — | — | work |
| `bf:expressionOf` | **clean** | `bffi:expressionOf`<br>`bffi:representativeExpressionOf` | bffi-meta:broadMatch<br>owl:equivalentProperty | expression |
| `bf:extent` | **clean** | `bffi:extent`<br>`bffi:extentOfRepresentativeExpression` | bffi-meta:closeMatch<br>owl:equivalentProperty | manifestation |
| `bf:genreForm` | **clean** | `bffi:genreForm` | owl:equivalentProperty | work |
| `bf:hasSeries` | **GAP** | — | — | manifestation |
| `bf:identifiedBy` | **clean** | `bffi:identifiedBy` | owl:equivalentProperty | expression, manifestation, work |
| `bf:instanceOf` | *semantic-shift* | `bffi:expressionManifested, bffi:workManifested` | bffi-meta:broadMatch | expression, manifestation, work |
| `bf:intendedAudience` | **clean** | `bffi:intendedAudience`<br>`bffi:intendedAudienceOfRepresentativeExpression` | bffi-meta:closeMatch<br>owl:equivalentProperty | work |
| `bf:issuance` | *semantic-shift* | `bffi:extensionPlan, bffi:issuance` | bffi-meta:broadMatch | expression, manifestation, work |
| `bf:keyMode` | **GAP** | — | — | expression, manifestation |
| `bf:language` | **clean** | `bffi:language`<br>`bffi:languageOfExpression`<br>`bffi:languageOfRepresentativeExpression` | bffi-meta:broadMatch<br>bffi-meta:closeMatch<br>owl:equivalentProperty | expression, manifestation |
| `bf:mainTitle` | **clean** | `bffi:mainTitle` | owl:equivalentProperty | expression, manifestation, work |
| `bf:media` | **clean** | `bffi:media` | owl:equivalentProperty | manifestation |
| `bf:mediumComponent` | **GAP** | — | — | work |
| `bf:mediumOfPerformance` | **GAP** | — | — | work |
| `bf:musicMedium` | **clean** | `bffi:musicMedium, bffi:musicMediumOfRepresentativeExpression`<br>`bffi:mediumOfChoreographicContent, bffi:mediumOfChoreographicContentOfRepresentativeExpression` | bffi-meta:closeMatch<br>rdfs:subPropertyOf | expression, manifestation |
| `bf:note` | **clean** | `bffi:note` | rdfs:subPropertyOf | expression, manifestation |
| `bf:originDate` | **clean** | `bffi:originDate`<br>`bffi:timePeriodOfCreation` | bffi-meta:closeMatch<br>owl:equivalentProperty | work |
| `bf:originPlace` | **clean** | `bffi:originPlace` | owl:equivalentProperty | work |
| `bf:partName` | **clean** | `bffi:partName` | owl:equivalentProperty | expression, manifestation, work |
| `bf:partNumber` | **clean** | `bffi:partNumber` | owl:equivalentProperty | expression, manifestation, work |
| `bf:place` | **clean** | `bffi:place`<br>`bffi:locationOfCollection` | bffi-meta:broadMatch<br>rdfs:subPropertyOf | manifestation |
| `bf:provisionActivity` | **clean** | `bffi:provisionActivity` | owl:equivalentProperty | manifestation |
| `bf:publicationStatement` | **clean** | `bffi:publicationStatement` | owl:equivalentProperty | manifestation |
| `bf:qualifier` | **clean** | `bffi:qualifier` | owl:equivalentProperty | manifestation |
| `bf:relation` | **clean** | `bffi:relation` | owl:equivalentProperty | aggregation, expression, manifestation, work |
| `bf:relationship` | **clean** | `bffi:relationship` | owl:equivalentProperty | manifestation |
| `bf:responsibilityStatement` | **clean** | `bffi:responsibilityStatement` | owl:equivalentProperty | manifestation |
| `bf:role` | **clean** | `bffi:role` | owl:equivalentProperty | expression, work |
| `bf:soundCharacteristic` | **clean** | `bffi:soundCharacteristic` | owl:equivalentProperty | manifestation |
| `bf:source` | **clean** | `bffi:source`<br>`bffi:sourceConsulted` | bffi-meta:broadMatch<br>rdfs:subPropertyOf | expression, manifestation, work |
| `bf:status` | **clean** | `bffi:status` | owl:equivalentProperty | manifestation |
| `bf:subject` | **clean** | `bffi:subject` | owl:equivalentProperty | work |
| `bf:subtitle` | **clean** | `bffi:subtitle` | owl:equivalentProperty | manifestation |
| `bf:summary` | **clean** | `bffi:summary` | owl:equivalentProperty | expression |
| `bf:tableOfContents` | **clean** | `bffi:tableOfContents` | owl:equivalentProperty | manifestation |
| `bf:title` | **clean** | `bffi:title` | owl:equivalentProperty | expression, manifestation, work |
| `bf:version` | **clean** | `bffi:version` | owl:equivalentProperty | expression, manifestation |

## Re-anchor clusters (tree view)

One tree per `bffi:Anchor owl:equivalentClass bf:X`, showing every `bffi:Sub rdfs:subClassOf` descendant (transitive closure) with its own `bf:*` equivalent when available. Emitting any node in a tree makes every BIBFRAME ancestor satisfied by inference.

Legend: ✅ = `bffi:Sub owl:equivalentClass bf:*` is declared · 🆕 = BFFI-native, no `bf:*` counterpart in lkd.rdf · ⤴ = `bffi-meta:broadMatch`/`closeMatch` only (no clean alias).

### `bffi:AccessPolicy` ≡ `bf:AccessPolicy`

  - `bffi:AgeLimit` ⤴ bf:AccessPolicy (broadMatch)

### `bffi:Agent` ≡ `bf:Agent`

  - `bffi:Family` ✅ ≡ `bf:Family`
  - `bffi:Jurisdiction` ✅ ≡ `bf:Jurisdiction`
  - `bffi:Meeting` ✅ ≡ `bf:Meeting`
  - `bffi:MetadataLicensor` 🆕 *BFFI-native*
  - `bffi:Organization` ✅ ≡ `bf:Organization`
  - `bffi:Person` ✅ ≡ `bf:Person`

### `bffi:BibframeWork` ≡ `bf:Work`

  - `bffi:Expression` ⤴ bf:Work (broadMatch)
    - `bffi:AggregatingExpression` ⤴ bf:Work (broadMatch)
    - `bffi:Arrangement` ✅ ≡ `bf:Arrangement`
    - `bffi:CartographyExpression` ⤴ bf:Cartography (broadMatch)
    - `bffi:CollectionExpression` ⤴ bf:Collection (broadMatch)
    - `bffi:Dataset` ✅ ≡ `bf:Dataset`
    - `bffi:MixedMaterial` ✅ ≡ `bf:MixedMaterial`
      - `bffi:Kit` ✅ ≡ `bf:Kit`
    - `bffi:MonographExpression` ⤴ bf:Monograph (broadMatch)
    - `bffi:MovingImageExpression` ⤴ bf:MovingImage (broadMatch)
    - `bffi:Multimedia` ✅ ≡ `bf:Multimedia`
    - `bffi:MusicAudioExpression` ⤴ bf:MusicAudio (broadMatch)
    - `bffi:NonMusicAudioExpression` ⤴ bf:NonMusicAudio (broadMatch)
    - `bffi:NotatedMovement` ✅ ≡ `bf:NotatedMovement`
    - `bffi:NotatedMusic` ✅ ≡ `bf:NotatedMusic`
    - `bffi:Object` ✅ ≡ `bf:Object`
    - `bffi:SerialExpression` ⤴ bf:Serial (broadMatch)
    - `bffi:SeriesExpression` ⤴ bf:Series (broadMatch)
    - `bffi:StillImage` ✅ ≡ `bf:StillImage`
    - `bffi:Text` ✅ ≡ `bf:Text`
  - `bffi:Work` ⤴ bf:Work (broadMatch)
    - `bffi:AggregatingWork` ⤴ bf:Work (broadMatch)
    - `bffi:CartographyWork` ⤴ bf:Cartography (broadMatch)
    - `bffi:CollectionWork` ⤴ bf:Collection (broadMatch)
    - `bffi:Integrating` ✅ ≡ `bf:Integrating`
    - `bffi:Manuscript` ✅ ≡ `bf:Manuscript`
    - `bffi:MonographWork` ⤴ bf:Monograph (broadMatch)
    - `bffi:MovingImageWork` ⤴ bf:MovingImage (broadMatch)
    - `bffi:MusicWork` ⤴ bf:NotatedMusic (closeMatch) / bf:MusicAudio (closeMatch)
    - `bffi:NonMusicAudioWork` ⤴ bf:NonMusicAudio (broadMatch)
    - `bffi:SerialWork` ⤴ bf:Serial (broadMatch)
    - `bffi:SeriesWork` ⤴ bf:Series (broadMatch)

### `bffi:Classification` ≡ `bf:Classification`

  - `bffi:ClassificationDdc` ✅ ≡ `bf:ClassificationDdc`
  - `bffi:ClassificationLcc` ✅ ≡ `bf:ClassificationLcc`
  - `bffi:ClassificationNal` ✅ ≡ `bf:ClassificationNal`
  - `bffi:ClassificationNlm` ✅ ≡ `bf:ClassificationNlm`
  - `bffi:ClassificationUdc` ✅ ≡ `bf:ClassificationUdc`

### `bffi:Contribution` ≡ `bf:Contribution`

  - `bffi:PrimaryContribution` ✅ ≡ `bf:PrimaryContribution`

### `bffi:DigitalCharacteristic` ≡ `bf:DigitalCharacteristic`

  - `bffi:CartographicDataType` ✅ ≡ `bf:CartographicDataType`
  - `bffi:CartographicObjectType` ✅ ≡ `bf:CartographicObjectType`
  - `bffi:EncodedBitrate` ✅ ≡ `bf:EncodedBitrate`
  - `bffi:EncodingFormat` ✅ ≡ `bf:EncodingFormat`
  - `bffi:FileSize` ✅ ≡ `bf:FileSize`
  - `bffi:FileType` ✅ ≡ `bf:FileType`
  - `bffi:ObjectCount` ✅ ≡ `bf:ObjectCount`
  - `bffi:RegionalEncoding` ✅ ≡ `bf:RegionalEncoding`
  - `bffi:Resolution` ✅ ≡ `bf:Resolution`

### `bffi:EnumerationAndChronology` ≡ `bf:EnumerationAndChronology`

  - `bffi:Chronology` ✅ ≡ `bf:Chronology`
  - `bffi:Enumeration` ✅ ≡ `bf:Enumeration`

### `bffi:Identifier` ≡ `bf:Identifier`

  - `bffi:Local` ✅ ≡ `bf:Local`
  - `bffi:ShelfMark` ✅ ≡ `bf:ShelfMark`

### `bffi:Item` ≡ `bf:Item`

  - `bffi:CollectionItem` ⤴ bf:Item (broadMatch)

### `bffi:Manifestation` ≡ `bf:Instance`

  - `bffi:Archival` ✅ ≡ `bf:Archival`
  - `bffi:CollectionManifestation` ⤴ bf:Instance (broadMatch)
  - `bffi:Electronic` ✅ ≡ `bf:Electronic`
  - `bffi:Microform` ✅ ≡ `bf:Microform`
  - `bffi:Print` ✅ ≡ `bf:Print`
  - `bffi:Tactile` ✅ ≡ `bf:Tactile`

### `bffi:Material` ≡ `bf:Material`

  - `bffi:AppliedMaterial` ✅ ≡ `bf:AppliedMaterial`
  - `bffi:BaseMaterial` ✅ ≡ `bf:BaseMaterial`

### `bffi:MixedMaterial` ≡ `bf:MixedMaterial`

  - `bffi:Kit` ✅ ≡ `bf:Kit`

### `bffi:Notation` ≡ `bf:Notation`

  - `bffi:MovementNotation` ✅ ≡ `bf:MovementNotation`
  - `bffi:MusicNotation` ✅ ≡ `bf:MusicNotation`
  - `bffi:Script` ✅ ≡ `bf:Script`
  - `bffi:TactileNotation` ✅ ≡ `bf:TactileNotation`

### `bffi:Note` ≡ `bf:Note`

  - `bffi:TitleNote` ⤴ bf:Note (broadMatch)

### `bffi:ProjectionCharacteristic` ≡ `bf:ProjectionCharacteristic`

  - `bffi:PresentationFormat` ✅ ≡ `bf:PresentationFormat`
  - `bffi:ProjectionSpeed` ✅ ≡ `bf:ProjectionSpeed`

### `bffi:ProvisionActivity` ≡ `bf:ProvisionActivity`

  - `bffi:Distribution` ✅ ≡ `bf:Distribution`
  - `bffi:Manufacture` ✅ ≡ `bf:Manufacture`
  - `bffi:Modification` ✅ ≡ `bf:Modification`
  - `bffi:Production` ✅ ≡ `bf:Production`
  - `bffi:Publication` ✅ ≡ `bf:Publication`

### `bffi:Scale` ≡ `bf:Scale`

  - `bffi:ScaleDesignation` ⤴ bf:Scale (broadMatch)

### `bffi:SoundCharacteristic` ≡ `bf:SoundCharacteristic`

  - `bffi:CaptureStorage` ✅ ≡ `bf:CaptureStorage`
  - `bffi:GrooveCharacteristic` 🆕 *BFFI-native*
  - `bffi:GrooveCutting` 🆕 *BFFI-native*
  - `bffi:PlaybackChannels` ✅ ≡ `bf:PlaybackChannels`
  - `bffi:PlaybackCharacteristic` ✅ ≡ `bf:PlaybackCharacteristic`
  - `bffi:PlayingSpeed` ✅ ≡ `bf:PlayingSpeed`
  - `bffi:RecordingMedium` ✅ ≡ `bf:RecordingMedium`
  - `bffi:RecordingMethod` ✅ ≡ `bf:RecordingMethod`
  - `bffi:TapeConfig` ✅ ≡ `bf:TapeConfig`
  - `bffi:TrackConfig` ✅ ≡ `bf:TrackConfig`

### `bffi:Source` ≡ `bf:Source`

  - `bffi:RecordingSource` ⤴ bf:Source (broadMatch)
    - `bffi:TitleSource` ⤴ bf:Source (broadMatch)

### `bffi:SystemRequirement` ≡ `bf:SystemRequirement`

  - `bffi:MachineModel` 🆕 *BFFI-native*
  - `bffi:OperatingSystem` 🆕 *BFFI-native*
  - `bffi:ProgrammingLanguage` 🆕 *BFFI-native*

### `bffi:UsageAndAccessPolicy` ≡ `bf:UsageAndAccessPolicy`

  - `bffi:AccessPolicy` ✅ ≡ `bf:AccessPolicy`
    - `bffi:AgeLimit` ⤴ bf:AccessPolicy (broadMatch)
  - `bffi:RetentionPolicy` ✅ ≡ `bf:RetentionPolicy`
  - `bffi:UsePolicy` ✅ ≡ `bf:UsePolicy`

### `bffi:VideoCharacteristic` ≡ `bf:VideoCharacteristic`

  - `bffi:BroadcastStandard` ✅ ≡ `bf:BroadcastStandard`
  - `bffi:VideoFormat` ✅ ≡ `bf:VideoFormat`

