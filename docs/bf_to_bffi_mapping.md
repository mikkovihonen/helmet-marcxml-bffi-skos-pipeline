# `bf:*` → `bffi:*` mapping reference

## Background

This document maps every `bf:*` (BIBFRAME) term encountered when converting MARCXML records to BFFI, to its BFFI-namespace counterpart. The conversion path is two-stage: **MARCXML → BIBFRAME** via the LC marc2bibframe2 XSLT, then **BIBFRAME → BFFI** via SPARQL CONSTRUCT (or equivalent RDF processing).

BFFI's namespace is closed — emit-side BFFI graphs should not carry `bf:*` terms — so every `bf:*` token in the BIBFRAME intermediate needs either a direct BFFI replacement, a routing rule when no direct counterpart exists, or NLF input when no working substitute is yet defined. The mapping below enumerates each case.

It is generated programmatically by parsing BFFI's `lkd.rdf` ontology source (`owl:versionInfo` 1.0.0, based on BIBFRAME 2.4.0) with rdflib, walking BFFI ↔ BIBFRAME relations (`owl:equivalentClass`, `owl:equivalentProperty`, `rdfs:subPropertyOf`, the re-anchor `subClassOf` pattern, and the `bffi-meta:*Match` links) in both directions.

### Status legend

| Status | Meaning |
|---|---|
| **clean** | Direct rename or re-anchor (`owl:equivalentClass` / `owl:equivalentProperty` / `rdfs:subPropertyOf`). |
| ***semantic-shift*** | `bffi-meta:broadMatch` / `closeMatch` only. |
| **routed** | No single BFFI replacement; the per-instance data determines which existing `bffi:*` class applies (see the routing callouts below the relevant table). |
| **GAP** | No link of any kind; requires NLF input. |

### Document conventions

| Element | Meaning |
|---|---|
| **Re-anchor pattern** | A `bffi:*` class has subclasses (`bffi:Sub rdfs:subClassOf bffi:Anchor`) and the anchor is `owl:equivalentClass bf:X`. Emitting any subclass alone makes the BIBFRAME class recoverable by OWL inference — no need to dual-type with `bf:*`. |
| **"Also satisfies" column** | Per-row inheritance closure: emitting the row's BFFI replacement makes the listed BIBFRAME classes recoverable by OWL inference, walked from `bffi:Y` upward through `rdfs:subClassOf` and counting every BFFI ancestor with `owl:equivalentClass bf:*`. Empty when there's no inheritance chain beyond the direct equivalence. |

## Classes (sorted alphabetically)

| `bf:` class | Status | `bffi:*` replacement(s) | Link kind | Also satisfies (via inference) | Used in |
|---|---|---|---|---|---|
| `bf:Archival` | **clean** | `bffi:Archival` | owl:equivalentClass | `bf:Instance` | manifestation |
| `bf:Arrangement` | **clean** | `bffi:Arrangement` | owl:equivalentClass | `bf:Work` | expression, manifestation |
| `bf:Audio` | **routed** | `bffi:NonMusicAudioWork` (Work-axis) / `bffi:NonMusicAudioExpression` (Expression-axis) — see note below | Emit-time convention: marc2bibframe2 emits `bf:Audio` only for non-music audio (music gets `bf:MusicAudio` directly); the BFFI emit routes `bf:Audio` → `bffi:NonMusicAudio*` per that convention | `bf:Audio` (via BIBFRAME `bf:NonMusicAudio rdfs:subClassOf bf:Audio` chain + `bffi:NonMusicAudio* bffi-meta:broadMatch bf:NonMusicAudio`) | expression, work |
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
| `bf:KeyMode` | **routed** | `bffi:musicKey` (Literal datatype property; carries the key as a string like `"B-flat major"`) — BFFI collapses the structured Key block into a literal (see [Music-medium and music-key routing](#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) below) | n/a — no class for structured Key in BFFI; collapse to literal | `bf:KeyMode` is alive in BIBFRAME 2.5 but BFFI chose the literal-only shape | expression, manifestation |
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
| `bf:VariantTitle` | **routed** | `bffi:Title` (the anchor — covers transcribed-title shapes per its `skos:definition`) + `bffi:marcKey` discriminator (see [Title-variant routing](#title-variant-routing-bfvarianttitle--bffititle--bffimarckey) below) | n/a — no direct `bffi:VariantTitle`; route by the MARC tag in `bffi:marcKey` | `bf:Title` (via `bffi:Title ≡ bf:Title`) | expression |
| `bf:Work` | **clean** | `bffi:BibframeWork`<br>`bffi:Expression, bffi:Work`<br>`bffi:AggregatingExpression, bffi:AggregatingWork, bffi:Expression, bffi:Work` | bffi-meta:broadMatch<br>owl:equivalentClass<br>re-anchor (subClassOf BibframeWork) | — | aggregation, expression, manifestation, work |

### Hub routing — `bf:Hub` → `bffi:Expression` vs `bffi:Work`

`lkd.rdf` has no `bffi:Hub`. But `bf:Hub` isn't a single semantic — it's BIBFRAME's flat aggregation of two FRBR/RDA-distinct things: a Work-axis reference (e.g. "Beethoven · Symphony no. 5") vs an Expression-axis reference (e.g. "English translation of *À la recherche*"). BFFI separates them. So `bf:Hub` routes to a `bffi:Work`-side or `bffi:Expression`-side class **based on what facets the source MARC entry carries**.

The discriminator is the **marcKey value** carried on every `bf:Hub` bnode in the BIBFRAME input. The routing reads it as **`bflc:marcKey`** (what marc2bibframe2 literally emits — the BFLC-standard predicate); the BFFI-side `bffi:marcKey` doesn't exist as a triple at routing time, only by `owl:equivalentProperty` inference. The marcKey value encodes the full MARC field as `<tag><ind1><ind2> <subfields>` — for example `"73002$aSymphonie no. 5$lenglanti$omikrofilmi"`. The routing reads two things from this string:

1. **First 3 characters** — the MARC tag (`240` / `730` / `740` / `100` / `700` / `130` / `830`) — determines whether the Hub came from a uniform title, an analytical added entry, an author-attributed title, a series, etc.
2. **Subfield codes embedded in the value** (`$l`, `$o`, `$r`, `$s`, `$m`, `$t`) — present Expression-level facets that signal the Hub IS an Expression (a specific realization), not just a Work reference.

When the BIBFRAME emit also decomposes specific subfields into structured predicates on the Hub bnode (e.g., `bf:language` URI for `$l`, `bf:musicMedium` for `$m`, `bf:keyMode` for `$r`, `bf:Arrangement` rdf:type for `$o`), the routing can read those directly — same signal, just decomposed. The marcKey-substring check is the universal fallback that always works.

Routing table (read top-to-bottom; first match wins):

| Signal | Routed `bffi:*` type | Why |
|---|---|---|
| `bflc:marcKey` contains `$o` arrangement subfield, OR `bf:Arrangement` in the Hub's `rdf:type` set | `bffi:Arrangement` | `bffi:Arrangement` is `owl:equivalentClass bf:Arrangement` AND `rdfs:subClassOf bffi:Expression` — perfect direct match; arrangements are Expression-level by definition |
| `bflc:marcKey` contains `$m` medium-of-performance subfield AND record content-type is audio recording | `bffi:MusicAudioExpression` | Expression-axis content-type-specific subclass of `bffi:Expression` |
| `bflc:marcKey` contains `$m` AND record content-type is notated music | `bffi:NotatedMusic` (`owl:equivalentClass bf:NotatedMusic`) | Expression-axis subclass of `bffi:Expression` |
| `bflc:marcKey` contains `$l` language qualifier (the dominant Expression signal) | `bffi:Expression` (+ `bffi:languageOfExpression` triple carrying the language URI) | `$l` names the language OF an Expression; the Hub IS that Expression |
| `bflc:marcKey` contains `$r` key subfield | `bffi:Expression` (+ `bffi:musicKey` literal) | Key is an Expression-level attribute |
| `bflc:marcKey` contains `$s` version subfield | `bffi:Expression` (+ `bffi:version`) | Version is an Expression-level attribute |
| First 3 chars of `bflc:marcKey` are `100` or `700` AND `$t` subfield present (author-attributed uniform title) | `bffi:Work` | The cataloguer named a Work via the author entry, not an Expression; treat as Work-level |
| First 3 chars of `bflc:marcKey` are `130` or `830` (series uniform title) | `bffi:SeriesWork` *or* `bffi:SeriesExpression` (axis-pick per record context) | Series entry; axis follows the per-record context |
| First 3 chars of `bflc:marcKey` are `730` or `740` AND no Expression-level subfields present | `bffi:Work` | Plain transcribed title with no Expression signal — cataloguer named a Work |
| Otherwise (fallback) | `bffi:Work` | Absent any Expression-level signal, default to the Work axis |

**BFFI emit side**: the routed `bffi:Work` / `bffi:Expression` carries `bffi:marcKey` (BFFI-namespace; `owl:equivalentProperty bflc:marcKey`) with the same marcKey value forwarded from the `bf:Hub` bnode. Downstream BFFI consumers querying the canonical use `bffi:marcKey` (closed-namespace); BIBFRAME-aware consumers can reach `bflc:marcKey` by `owl:equivalentProperty` inference. The routing-time read is `bflc:marcKey` (since that's the literal predicate in the BIBFRAME input from marc2bibframe2); the emit-time write is `bffi:marcKey`.

What survives the migration without NLF input:

- **Zero new BFFI terms required.** Every routed type already exists in `lkd.rdf` (`bffi:Work`, `bffi:Expression`, `bffi:Arrangement`, `bffi:MusicAudioExpression`, `bffi:NotatedMusic`, `bffi:SeriesWork`, `bffi:SeriesExpression`, `bffi:languageOfExpression`, `bffi:musicKey`, `bffi:version`, `bffi:marcKey`).
- **`bf:Work` recovery via inference**: both routes pass through the `bffi:BibframeWork ≡ bf:Work` anchor — `bffi:Work ⊑ bffi:BibframeWork ≡ bf:Work` AND `bffi:Expression ⊑ bffi:BibframeWork ≡ bf:Work`. BIBFRAME consumers reading the canonical see a `bf:Work`-shaped target either way (which is correct — BIBFRAME's `bf:Hub` is itself a `bf:Work`-shaped grouping).
- **Round-trip integrity**: the Expression-level facets that triggered the routing (`bffi:languageOfExpression`, `bffi:musicKey`, `bffi:version`, `bffi:Arrangement` type) are themselves recoverable via existing BFFI vocabulary — the MARC `$l` / `$o` / `$r` / `$s` subfields can be reconstructed without `bf:Hub` typing.
- **What's lost**: the BIBFRAME-specific "this is a less-rigorously-described entity" signal. BFFI's view is that sparse-description isn't a separate class — it's just a Work or Expression with minimal metadata. The information loss is ontological, not data.

### Identifier-scheme routing — `bf:Isbn` / `bf:Issn` / `bf:Ean` / `bf:AudioIssueNumber` / `bf:OtherIdentifier` → `bffi:Identifier` + `bffi:source`

`lkd.rdf` declares `bffi:Identifier ≡ bf:Identifier` but only two subclasses below it — `bffi:Local` and `bffi:ShelfMark`. The standard MARC identifier types (ISBN, ISSN, EAN, AudioIssueNumber, the catch-all OtherIdentifier) have no BFFI subclass. **Their semantic content lives at the predicate level instead**, via the existing `bffi:source` + `bffi:Source` + `bffi:code` triple structure that BFFI already declares.

The same pattern applies to local-library identifiers — a BFFI emit typically writes them as:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Local ;
    rdf:value "b21152068" ;
    bf:source <http://example.org/bib:source/local-library>
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
- **Round-trip integrity** — the `bffi:source` URI deterministically maps back to a MARC field + indicators ($2 scheme code, indicator values), so a BFFI-to-MARC reconstruction can recover `020` / `022` / `024` / `028` without `bf:*` class typing.
- **Display-layer rendering** — the `bffi:source` URIs benefit from multilingual labels in whichever display config the consumer uses (Skosmos, a custom UI, etc.) — e.g. `"ISBN"@en, "ISBN"@fi, "ISBN"@sv`. Same pattern as labels for any other URI-typed value.

What's lost:

- **Type-based SPARQL queries** — `?ident a bf:Isbn` no longer works. Consumers query `?ident bffi:source <…/identifiers/isbn>` instead. Same selectivity, different idiom.
- **BIBFRAME-side `rdfs:subClassOf bf:Identifier` inference** — a BIBFRAME consumer expecting `bf:Isbn ⊑ bf:Identifier` finds `bffi:Identifier ≡ bf:Identifier` directly, but the `bf:Isbn` subtype doesn't materialise. Acceptable trade-off — the scheme code carries the same information at a different axis.

### Title-variant routing — `bf:VariantTitle` → `bffi:Title` + `bffi:marcKey`

`bffi:Title`'s own `skos:definition` is explicit: *"Title information relating to a resource: work title, preferred title, instance title, **transcribed title**…"* — variant titles are subsumed under the single `bffi:Title` class by design. BFFI deliberately collapses the BIBFRAME `Title` / `VariantTitle` / `ParallelTitle` / `KeyTitle` subclass tree into one class with marcKey-discriminated instances.

The discriminator is the **marcKey value** carried on the title bnode. At routing time (reading the BIBFRAME `bf:VariantTitle` / `bf:Title` bnodes that marc2bibframe2 emits), the literal predicate is **`bflc:marcKey`** — `bffi:marcKey` only exists by `owl:equivalentProperty` inference at that point. The BFFI emit then writes the marcKey value forward onto the resulting `bffi:Title` bnode using **`bffi:marcKey`** (closed-namespace; `owl:equivalentProperty bflc:marcKey` keeps the BFLC-side reachable by reasoning).

The marcKey value encodes the full MARC field as `<tag><ind1><ind2> <subfields>` — for example `"24610$aOsallisuus ja yhteisöllisyys…"`. The first three characters are the MARC tag, which uniquely identifies the title kind:

| MARC tag (first 3 chars of the marcKey value) | Title kind |
|---|---|
| `245` | main title (the primary title of the resource) |
| `246` | variant title (alternative form transcribed from the resource) |
| `240` | uniform title (the conventional title for a Work) |
| `730` | added entry — analytical / related title |
| `740` | uncontrolled added entry |
| `247` | former title (predecessor title in serials) |
| `222` | key title (ISSN-registered series title) |

Emit shape (parallel `bffi:title` chains on the same parent, each bnode typed `bffi:Title`, with `bffi:marcKey` carrying the MARC field encoding):

```turtle
<expression> bffi:title <main-title-bnode> ;
             bffi:title <variant-title-bnode> .

<main-title-bnode> a bffi:Title ;
                   bffi:mainTitle "Osallisuus ja yhteisöllisyys" ;
                   bffi:marcKey "24500$aOsallisuus ja yhteisöllisyys" .

<variant-title-bnode> a bffi:Title ;
                      bffi:mainTitle "Osallisuus ja yhteisöllisyys lastenkotien retkitoiminnassa" ;
                      bffi:marcKey "24631$aOsallisuus ja yhteisöllisyys lastenkotien retkitoiminnassa" .
```

Consumers determine the title kind by reading the first three characters of `bffi:marcKey`. The round-trip back to MARC uses the same value to reconstruct the field tag + indicators + subfields verbatim — the marcKey IS the original MARC field, so reconstruction is identity.

What survives without NLF input:

- **Zero new BFFI terms** — `bffi:Title`, `bffi:mainTitle`, `bffi:title`, and `bffi:marcKey` are all in the existing BFFI vocabulary set. `bffi:marcKey owl:equivalentProperty bflc:marcKey` makes the BFLC-side counterpart reachable for BIBFRAME-aware consumers.
- **`bf:Title` recovery via inference** — `bffi:Title ≡ bf:Title` is direct equivalence; BIBFRAME consumers walking the OWL chain still see a `bf:Title`-shaped target.
- **Round-trip integrity** — `bffi:marcKey` carries the full MARC field encoding, so reconstruction is direct (read the tag, indicators, and subfields from the marcKey value).
- **Standard discriminator** — `bffi:marcKey` is a published BFFI predicate (`owl:equivalentProperty bflc:marcKey`); the underlying marcKey encoding is well-known to BIBFRAME-aware consumers. No project-internal vocabulary required.

What's lost:

- **Type-based SPARQL queries** — `?vt a bf:VariantTitle` no longer works. Consumers switch to `?t a bffi:Title ; bffi:marcKey ?mk . FILTER(STRSTARTS(?mk, "246"))`. Same selectivity, slightly more verbose.
- **The four BIBFRAME Title subclasses** (`bf:VariantTitle`, `bf:ParallelTitle`, `bf:KeyTitle`, `bf:CollectiveTitle`) all collapse into the same routing — discriminated by the MARC tag in `bffi:marcKey` instead of the class.

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
| `bf:ensemble` | **routed** | folds into `bffi:musicMedium` → `bffi:MusicMedium` → `bffi:readMarc382` (literal MARC 382 string) — see [Music-medium and music-key routing](#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) below | n/a — BFFI doesn't decompose ensemble structurally | work |
| `bf:expressionOf` | **clean** | `bffi:expressionOf`<br>`bffi:representativeExpressionOf` | bffi-meta:broadMatch<br>owl:equivalentProperty | expression |
| `bf:extent` | **clean** | `bffi:extent`<br>`bffi:extentOfRepresentativeExpression` | bffi-meta:closeMatch<br>owl:equivalentProperty | manifestation |
| `bf:genreForm` | **clean** | `bffi:genreForm` | owl:equivalentProperty | work |
| `bf:hasSeries` | **routed** | `bffi:relation` → `bffi:Relation` bnode with `bffi:relationship <…/relationship/series>` + `bffi:associatedResource <series>` (Series target typed `bffi:SeriesWork` / `bffi:SeriesExpression`) (see [Series-link routing](#series-link-routing-bfhasseries--bffirelation--bffiserieswork--bffiseriesexpression) below) | n/a — no direct `bffi:hasSeries`; use BFFI's structured-relation pattern | manifestation |
| `bf:identifiedBy` | **clean** | `bffi:identifiedBy` | owl:equivalentProperty | expression, manifestation, work |
| `bf:instanceOf` | *semantic-shift* | `bffi:expressionManifested, bffi:workManifested` | bffi-meta:broadMatch | expression, manifestation, work |
| `bf:intendedAudience` | **clean** | `bffi:intendedAudience`<br>`bffi:intendedAudienceOfRepresentativeExpression` | bffi-meta:closeMatch<br>owl:equivalentProperty | work |
| `bf:issuance` | *semantic-shift* | `bffi:extensionPlan, bffi:issuance` | bffi-meta:broadMatch | expression, manifestation, work |
| `bf:keyMode` | **routed** | `bffi:musicKey` (Literal datatype, `owl:equivalentProperty bf:musicKey`) — collapses the predicate-to-Key-block chain into a literal — see [Music-medium and music-key routing](#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) below | n/a — BFFI has no structured-Key predicate | expression, manifestation |
| `bf:language` | **clean** | `bffi:language`<br>`bffi:languageOfExpression`<br>`bffi:languageOfRepresentativeExpression` | bffi-meta:broadMatch<br>bffi-meta:closeMatch<br>owl:equivalentProperty | expression, manifestation |
| `bf:mainTitle` | **clean** | `bffi:mainTitle` | owl:equivalentProperty | expression, manifestation, work |
| `bf:media` | **clean** | `bffi:media` | owl:equivalentProperty | manifestation |
| `bf:mediumComponent` | **routed** | folds into `bffi:musicMedium` → `bffi:MusicMedium` → `bffi:readMarc382` (literal MARC 382 string) — see [Music-medium and music-key routing](#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) below | n/a — BFFI doesn't decompose medium structurally | work |
| `bf:mediumOfPerformance` | **routed** | `bffi:musicMedium` (`rdfs:subPropertyOf bf:musicMedium`, range `bffi:MusicMedium`; the English label is literally *"music medium of performance"*) — see [Music-medium and music-key routing](#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) below | predicate-name shift (BFFI side renames to `bffi:musicMedium`) | work |
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

### Series-link routing — `bf:hasSeries` → `bffi:relation` + `bffi:SeriesWork` / `bffi:SeriesExpression`

BFFI declares the *literal-form* (`bffi:seriesStatement`, `bffi:seriesEnumeration` — both `owl:equivalentProperty` to their `bf:*` counterparts) and the *entity classes* (`bffi:SeriesWork`, `bffi:SeriesExpression` — both `bffi-meta:broadMatch bf:Series`), but **no `bffi:hasSeries`** linking predicate. The closest BFFI-namespace alternative is the **structured general-purpose relation chain** (`bffi:relation` → `bffi:Relation` bnode with `bffi:relationship` + `bffi:associatedResource`), pointing the relationship at LoC's `vocabulary/relationship/series` URI — the same shape `bffi:relation` already covers for related-work, hub-target, analytical-entry and contained-in cases.

Why not `dct:isPartOf`? The DC Terms section above shows `dct:isPartOf` has BFFI per-axis substitutes (`bffi:expressionOf`, `bffi:workManifested`, `bffi:expressionManifested`, `bffi:itemOf`) — but all four model **FRBR-axis identity** chains (X IS-A realization/instance/copy of Y), not aggregation/membership. None fits Series-membership semantics (a Manifestation is one of many publications *in* a Series, not an *instance of* the SeriesWork). And BFFI's aggregation predicate `bffi:aggregatedBy` has range `bffi:AggregatingExpression`, which is a sibling class to `bffi:SeriesExpression`, not a parent — using it would require dual-typing the Series. The structured-relation chain avoids both issues.

Emit shape:

```turtle
<manifestation> bffi:relation [
    a bffi:Relation ;
    bffi:relationship <http://id.loc.gov/vocabulary/relationship/series> ;
    bffi:associatedResource <series-uri>
] ;
bffi:seriesStatement "Julkaisuja (Nuorisotutkimusseura). Kenttä, no. 5" .

<series-uri> a bffi:SeriesWork ;        # or bffi:SeriesExpression — axis-pick
             skos:prefLabel "Julkaisuja (Nuorisotutkimusseura). Kenttä" ;
             bffi:seriesEnumeration "vol. 5" .  # when MARC 490$v / 830$v present
```

Axis pick on the Series target follows the same rule as the other axis-split classes — `bffi:SeriesExpression` for translation-of-a-series cases (e.g. a Finnish Expression of an English series), `bffi:SeriesWork` for Work-level series identity. Recommended default: `bffi:SeriesExpression` (matches the common case where the catalogued bib is a localised Expression-in-series).

What survives without NLF input:

- **Single-namespace emit** — every predicate and class on the chain is in `bffi:*` (no `dct:*` dependency).
- **Round-trip integrity** — the LoC `vocabulary/relationship/series` URI is the same one BIBFRAME's `bf:Relation` rows already use, and `bffi:relation owl:equivalentProperty bf:relation`. MARC 490 / 800 / 810 / 830 reconstruction matches the relationship URI to the field tag.
- **Series-entity identity** — typing the target as `bffi:SeriesWork` / `bffi:SeriesExpression` preserves the FRBR axis info that BIBFRAME's flat `bf:Series` lost. BIBFRAME consumers still see a `bf:Work`-shaped target via the re-anchor chain (`bffi:SeriesWork ⊑ bffi:Work ⊑ bffi:BibframeWork ≡ bf:Work`).

What's lost:

- **The `bf:hasSeries` predicate URI itself** — consumers walking `?m bf:hasSeries ?s` need to switch to `?m bffi:relation/bffi:associatedResource ?s . ?m bffi:relation/bffi:relationship <…/relationship/series>`. Same selectivity (the relationship URI distinguishes series-membership from other `bffi:relation` uses).
- **Direct-link convenience** — Series membership now lives on a Relation bnode, not as a flat predicate on the Manifestation. Skosmos rendering needs to walk one extra hop (the Hub/Relation routing patterns already do this; same display infrastructure).

### Music-medium and music-key routing — `bf:mediumOfPerformance` / `bf:mediumComponent` / `bf:ensemble` / `bf:KeyMode` → interim collapse to literal, pending BFFI absorption of BIBFRAME 3.0 PMO

**Version context.** BFFI 1.0.0 (`lkd.rdf`'s `owl:versionInfo`) is based on **BIBFRAME 2.4.0** (per its own `dct:description`). The Library of Congress released **BIBFRAME 3.0 in December 2025**, whose headline change is that NDMSO **absorbed the Performed Music Ontology (PMO) into core BIBFRAME**, adding or refining: `bf:MediumOfPerformance`, `bf:MediumComponent`, `bf:Ensemble`, `bf:EnsembleSize`, `bf:KeyMode`, `bf:Mode`, `bf:Tempo`, `bf:DramaticRole`, `bf:MediumComponentQualifier`, `bf:OpusNumber`, `bf:SerialNumber`, `bf:ThematicCatalogNumber` (each carrying `dct:modified 2025-12-01` with ticket `GH134`). BFFI predates this PMO absorption by ~1.5 years and **does not yet have BFFI-native equivalents** for the new PMO-imported classes.

A BFFI emit can therefore route these terms in two ways:

**1. Interim (works today against BFFI 1.0.0): collapse to `bffi:readMarc382` literal.**

For medium-of-performance terms (`bf:mediumOfPerformance`, `bf:mediumComponent`, `bf:ensemble` and the corresponding BIBFRAME 3.0 classes), route to the existing BFFI chain:

```turtle
<work>
    bffi:musicMedium [               # subPropertyOf bf:musicMedium, range bffi:MusicMedium
        a bffi:MusicMedium ;         # owl:equivalentClass bf:MusicMedium
        bffi:readMarc382 "1\\b vn$nvc$na 015\\$2 marcmusperf"
    ] .
```

`bffi:readMarc382` (English label: *"read-only 382 field"*) is the only property `lkd.rdf` declares with `bffi:MusicMedium` as its domain. It holds the verbatim MARC 382 string on the MusicMedium block, with the decomposition (individual instrument / voice / ensemble / part-count) encoded inside the literal as MARC text rather than separate RDF triples. `bffi:musicMedium`'s English label is literally **"music medium of performance"** — semantically the same role as `bf:mediumOfPerformance`.

For music key (`bf:KeyMode` class, `bf:keyMode` predicate, and the new BIBFRAME 3.0 `bf:Mode` / `bf:Tempo`), route to the existing literal:

```turtle
<work> a bffi:MusicWork ;
       bffi:musicKey "B-flat major" .
```

`bffi:musicKey` has `rdfs:domain bffi:MusicWork`, is a DatatypeProperty (range Literal), and `owl:equivalentProperty bf:musicKey` — corresponds to BIBFRAME's flat-literal `bf:musicKey`, not to the structured `bf:KeyMode` block.

**2. Target (after BFFI absorbs BIBFRAME 3.0 PMO): structured emit matching BFFI's anchor pattern.**

NLF could mirror BIBFRAME's PMO absorption by adding BFFI-namespace equivalents — `bffi:MediumOfPerformance`, `bffi:MediumComponent`, `bffi:Ensemble`, `bffi:EnsembleSize`, `bffi:KeyMode`, `bffi:Mode`, `bffi:Tempo`, `bffi:DramaticRole`, `bffi:MediumComponentQualifier` — each `owl:equivalentClass` to its BIBFRAME counterpart, following the existing re-anchor pattern (cf. `bffi:MusicMedium ≡ bf:MusicMedium`). A BFFI emit could then shift from the `bffi:readMarc382` literal to a structured PMO-shaped chain.

NLF asks (surfaced by BIBFRAME 3.0):

1. Will BFFI 1.1 absorb the BIBFRAME 3.0 PMO model? (Classes: `MediumOfPerformance`, `MediumComponent`, `Ensemble`, `EnsembleSize`, `KeyMode`, `Mode`, `Tempo`, `DramaticRole`, `MediumComponentQualifier`. Properties: corresponding predicate lowercased forms.)
2. If yes, will they follow the re-anchor pattern (`bffi:X owl:equivalentClass bf:X`, BFFI subclasses below)?
3. If no, will `bffi:readMarc382` remain the canonical shape (and should it gain a non-"read-only" sibling for write-side use)?

What survives in the interim (current BFFI 1.0.0):

- **Zero new BFFI terms required for the interim emit.** Every term in the collapse chain (`bffi:musicMedium`, `bffi:MusicMedium`, `bffi:readMarc382`, `bffi:musicKey`) is already in `lkd.rdf`.
- **`bf:MusicMedium` recovery via inference** — `bffi:MusicMedium owl:equivalentClass bf:MusicMedium`; the BIBFRAME 2.x class is directly reachable. (BIBFRAME 3.0's `bf:MusicMedium` is unchanged by the PMO absorption.)
- **Round-trip integrity** — a BFFI-to-MARC reconstruction re-emits MARC 382 verbatim from `bffi:readMarc382` (the literal IS the original MARC 382). Music-key reconstruction reads the `bffi:musicKey` literal into MARC 384.

What's lost in the interim (until BFFI absorbs PMO):

- **Structured-decomposition queries** — `?w bf:musicMedium/bf:mediumComponent ?c` (and the new BIBFRAME 3.0 `bf:Ensemble` / `bf:MediumComponent` chains) don't materialise; consumers needing the components must parse the `bffi:readMarc382` literal or query BIBFRAME directly.
- **Key/Mode/Tempo separation** — `?w bf:keyMode/bf:mode`, `?w bf:tempo`, the combined `bffi:musicKey "B-flat major"` literal carries the joined key+mode form but doesn't separate them.
- **Direct PMO-class equivalence** — entities the BIBFRAME 3.0 emit types as `bf:Ensemble` (new in 3.0) have no BFFI class to land on; they collapse into the MusicMedium block.

Sources for the BIBFRAME 3.0 release information:

- [BIBFRAME 3.0: Now with Improved Music Data — Music Library Association Cataloging and Metadata Committee](https://cmc.wp.musiclibraryassoc.org/2026/02/03/bibframe-3-0-now-with-improved-music-data/)
- [BIBFRAME — Library of Congress](https://www.loc.gov/bibframe/)
- [LC semi-annual BIBFRAME update, Feb 2026](https://www.loc.gov/bibframe/news/source/LOC%20semi-annual%20BIBFRAME%20update%20-%20Feb%202026.pdf)
- [`lcnetdev/bibframe-ontology` repository](https://github.com/lcnetdev/bibframe-ontology)

This is the same pattern as the Identifier-scheme and Title-variant collapses elsewhere in this doc: BFFI consistently chooses **one canonical class + one literal-carrier property** over BIBFRAME's structured subclass tree.

## DC Terms → BFFI alternatives

Definitive mapping from DC Terms predicates to BFFI counterparts. BFFI's `lkd.rdf` does not declare formal `owl:equivalentProperty` / `rdfs:subPropertyOf` / `bffi-meta:*Match` links to the `dct:*` namespace (verified by rdflib scan of every BFFI predicate's link set). The mapping below is therefore by **semantic correspondence** — each BFFI term carries an `owl:equivalentProperty` to the BIBFRAME predicate of comparable meaning, and the BIBFRAME predicate has the same role as the DC Terms predicate.

| `dct:*` term | BFFI alternative | Shape | BFFI ↔ BIBFRAME link |
|---|---|---|---|
| `dct:date` | `bffi:date` (parent) · `bffi:copyrightDate` · `bffi:changeDate` · `bffi:provisionActivityDate` (children) | Literal datatype | `bffi:*Date rdfs:subPropertyOf bffi:date`; each child has `owl:equivalentProperty bf:*Date` |
| `dct:identifier` | `bffi:identifiedBy` → `bffi:Identifier` (range) → `rdf:value` (literal payload) | ObjectProperty + structured bnode | `bffi:identifiedBy owl:equivalentProperty bf:identifiedBy` |
| `dct:isPartOf` | **per-axis** — `bffi:expressionOf` (Expression→Work) · `bffi:workManifested` (Manifestation→Work) · `bffi:expressionManifested` (Manifestation→Expression) · `bffi:itemOf` (Item→Manifestation) | ObjectProperty | each `owl:equivalentProperty bf:expressionOf` / `bf:workManifested` / `bf:expressionManifested` / `bf:itemOf`; multiple RDA exactMatches |
| `dct:modified` | `bffi:changeDate` (domain `bffi:AdminMetadata`) | Literal datatype on AdminMetadata bnode | `bffi:changeDate owl:equivalentProperty bf:changeDate`; `rdfs:subPropertyOf bffi:date` |
| `dct:publisher` | `bffi:publicationStatement` (literal) · `bffi:Publication` (class, `rdfs:subClassOf bffi:ProvisionActivity`) | Literal datatype · or class for structured bnode | `bffi:publicationStatement owl:equivalentProperty bf:publicationStatement`; `bffi:Publication owl:equivalentClass bf:Publication` |
| `dct:relation` | `bffi:relation` → `bffi:Relation` (range) | ObjectProperty + structured bnode (with `bffi:relationship` + `bffi:associatedResource`) | `bffi:relation owl:equivalentProperty bf:relation` |
| `dct:spatial` | `bffi:place` (parent) · `bffi:originPlace` (Work creation) · `bffi:geographicCoverage` (Work subject area) · `bffi:locationOfCollection` (CollectionManifestation) | ObjectProperty → `bffi:Place` | parent `bffi:place rdfs:subPropertyOf bf:place`; children with `owl:equivalentProperty bf:*` + RDA exactMatch |
| `dct:subject` | `bffi:subject` | ObjectProperty | `bffi:subject owl:equivalentProperty bf:subject`; RDA `w/P10256` exactMatch |
| `dct:temporal` | `bffi:temporalCoverage` | ObjectProperty → `bffi:Temporal` | `bffi:temporalCoverage owl:equivalentProperty bf:temporalCoverage`; RDA `w/P10322` exactMatch |
| `dct:title` | `bffi:title` (ObjectProperty → `bffi:Title`) · `bffi:mainTitle` (Literal datatype on `bffi:Title`) | ObjectProperty · or Literal datatype | `bffi:title owl:equivalentProperty bf:title`; `bffi:mainTitle owl:equivalentProperty bf:mainTitle` |

## Re-anchor clusters (tree view)

One tree per `bffi:Anchor owl:equivalentClass bf:X` that has at least one BFFI subclass. Each tree shows every `bffi:Sub rdfs:subClassOf` descendant (transitive closure) with its own `bf:*` equivalent when available. Emitting any node in a tree makes every BIBFRAME ancestor satisfied by inference.

The trees enumerate the complete BFFI re-anchor structure as declared in `lkd.rdf` — including classes a typical MARC-to-BFFI conversion does not encounter — so an NLF reviewer can verify the structure independently of conversion-scoped tables above.

Marker legend:

| Marker | Meaning |
|---|---|
| ✅ | `bffi:Sub owl:equivalentClass bf:*` is declared |
| 🆕 | BFFI-native — no `bf:*` counterpart in `lkd.rdf` |
| ⤴ | `bffi-meta:broadMatch` / `closeMatch` only (no clean alias) |

### Standalone anchors (no BFFI subclasses, no BFFI parents)

Fifty-six BFFI classes are `owl:equivalentClass bf:X` directly with no further BFFI hierarchy above or below them. Listed here alphabetically for completeness:

- `bffi:AcquisitionSource` ✅ ≡ `bf:AcquisitionSource`
- `bffi:AdminMetadata` ✅ ≡ `bf:AdminMetadata`
- `bffi:AspectRatio` ✅ ≡ `bf:AspectRatio`
- `bffi:Binding` ✅ ≡ `bf:Binding`
- `bffi:BookFormat` ✅ ≡ `bf:BookFormat`
- `bffi:Capture` ✅ ≡ `bf:Capture`
- `bffi:Carrier` ✅ ≡ `bf:Carrier`
- `bffi:Cartographic` ✅ ≡ `bf:Cartographic`
- `bffi:CollectionArrangement` ✅ ≡ `bf:CollectionArrangement`
- `bffi:ColorContent` ✅ ≡ `bf:ColorContent`
- `bffi:Content` ✅ ≡ `bf:Content`
- `bffi:ContentAccessibility` ✅ ≡ `bf:ContentAccessibility`
- `bffi:CopyrightRegistration` ✅ ≡ `bf:CopyrightRegistration`
- `bffi:CoverArt` ✅ ≡ `bf:CoverArt`
- `bffi:DescriptionAuthentication` ✅ ≡ `bf:DescriptionAuthentication`
- `bffi:DescriptionConventions` ✅ ≡ `bf:DescriptionConventions`
- `bffi:DescriptionLevel` ✅ ≡ `bf:DescriptionLevel`
- `bffi:Dissertation` ✅ ≡ `bf:Dissertation`
- `bffi:Emulsion` ✅ ≡ `bf:Emulsion`
- `bffi:Event` ✅ ≡ `bf:Event`
- `bffi:Extent` ✅ ≡ `bf:Extent`
- `bffi:FontSize` ✅ ≡ `bf:FontSize`
- `bffi:Frequency` ✅ ≡ `bf:Frequency`
- `bffi:Generation` ✅ ≡ `bf:Generation`
- `bffi:GenerationProcess` ✅ ≡ `bf:GenerationProcess`
- `bffi:GenreForm` ✅ ≡ `bf:GenreForm`
- `bffi:GeographicCoverage` ✅ ≡ `bf:GeographicCoverage`
- `bffi:Illustration` ✅ ≡ `bf:Illustration`
- `bffi:ImmediateAcquisition` ✅ ≡ `bf:ImmediateAcquisition`
- `bffi:IntendedAudience` ✅ ≡ `bf:IntendedAudience`
- `bffi:Language` ✅ ≡ `bf:Language`
- `bffi:Layout` ✅ ≡ `bf:Layout`
- `bffi:Media` ✅ ≡ `bf:Media`
- `bffi:Mount` ✅ ≡ `bf:Mount`
- `bffi:MusicFormat` ✅ ≡ `bf:MusicFormat`
- `bffi:MusicMedium` ✅ ≡ `bf:MusicMedium`
- `bffi:Place` ✅ ≡ `bf:Place`
- `bffi:Polarity` ✅ ≡ `bf:Polarity`
- `bffi:ProductionMethod` ✅ ≡ `bf:ProductionMethod`
- `bffi:Projection` ✅ ≡ `bf:Projection`
- `bffi:PubFrequency` ✅ ≡ `bf:PubFrequency`
- `bffi:ReductionRatio` ✅ ≡ `bf:ReductionRatio`
- `bffi:Relation` ✅ ≡ `bf:Relation`
- `bffi:Relationship` ✅ ≡ `bf:Relationship`
- `bffi:Relief` ✅ ≡ `bf:Relief`
- `bffi:Role` ✅ ≡ `bf:Role`
- `bffi:SoundContent` ✅ ≡ `bf:SoundContent`
- `bffi:Status` ✅ ≡ `bf:Status`
- `bffi:Sublocation` ✅ ≡ `bf:Sublocation`
- `bffi:Summary` ✅ ≡ `bf:Summary`
- `bffi:SupplementaryContent` ✅ ≡ `bf:SupplementaryContent`
- `bffi:TableOfContents` ✅ ≡ `bf:TableOfContents`
- `bffi:Temporal` ✅ ≡ `bf:Temporal`
- `bffi:Title` ✅ ≡ `bf:Title` (`bffi:TitleNote` exists but is `rdfs:subClassOf bffi:Note`, not `bffi:Title`; `bffi:TitleSource` is `rdfs:subClassOf bffi:RecordingSource`)
- `bffi:Topic` ✅ ≡ `bf:Topic`
- `bffi:Unit` ✅ ≡ `bf:Unit`

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

