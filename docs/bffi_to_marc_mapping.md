# `bffi:*` → MARCXML mapping reference

## Background

This document describes the **reverse-direction** conversion: a BFFI canonical Turtle graph is read and a MARCXML record is reconstructed from it. For each MARC field the converter emits, the table below names the BFFI predicates that drive it, the MARC tag/indicators/subfields produced, and any known caveats.

The table is **auto-generated** from the converter source. If the table omits a MARC field, the converter does not produce it today.

### What the converter reads — and what it does not

The reverse converter operates entirely from the **published BFFI schema** — the `bffi:` namespace plus the standard vocabularies the schema builds on (`rdf:`, `rdfs:`, `owl:`, `skos:`, `dct:`). Any data outside that scope is invisible to the reconstruction by design: the round-trip verifies that the BFFI graph alone preserves enough information to reconstruct the source MARC. Pipeline-internal state (processing audit trails, intermediate-stage annotations, etc.) is not consulted.

### Reading the table

The first column is the MARC tag (or `leader` for the record-level pseudo-tag). The Ind1 / Ind2 column shows the indicators the converter writes; `#` represents the MARC blank indicator (a literal space in MARCXML); `—` indicates a control field or leader with no indicators. The BFFI source column names the predicates the converter walks; the Notes column flags caveats.

## MARC fields the converter emits

<!-- BEGIN AUTO: shipped -->

| MARC tag | Ind1 / Ind2 | Subfields | BFFI source | Notes |
|---|---|---|---|---|
| `leader` | `—` | — | static placeholder | See known limitations below. |
| `001` | `—` | — | ?m bffi:identifiedBy [a bffi:Local ; rdf:value ?bib_id] (fallback: parse from the Manifestation URI fragment) | — |
| `005` | `—` | — | ?m bffi:adminMetadata [a bffi:AdminMetadata ; bffi:changeDate ?date] | — |
| `020` | `##` | `$a` — ISBN value | ?m bffi:identifiedBy [a bffi:Identifier ; bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn> ; rdf:value ?isbn] | — |
| `022` | `##` | `$a` — ISSN value | ?m bffi:identifiedBy [a bffi:Identifier ; bffi:source <http://id.loc.gov/vocabulary/identifiers/issn> ; rdf:value ?issn] | — |
| `041` | `##` | `$a` — 3-letter language code (one per language) | ?m bffi:language <http://id.loc.gov/vocabulary/languages/{code}> — the last URI segment is the MARC code | — |
| `084` | `##` | `$a` — classification number | ?m bffi:workManifested ?work . ?work bffi:classification [a bffi:Classification ; bffi:classificationPortion ?number] | Generic-scheme classification emit. Helmet-local 09X (091/092/094/095/097) and standard 050/080/082 dispatching by source is a follow-on. |
| `100` | `##` | `$a` — personal name<br>`$4` — LoC relator code | ?m bffi:workManifested ?work . ?work bffi:contribution [a bffi:PrimaryContribution ; bffi:agent ?agent ; bffi:role ?role] . ?agent a bffi:Person ; rdfs:label ?name . $4 = local-name of ?role (the LoC relator URI) | — |
| `110` | `##` | `$a` — corporate / jurisdiction name<br>`$4` — LoC relator code | Same as 100, but with ?agent a bffi:Organization (or bffi:Jurisdiction) on a primary contribution | — |
| `111` | `##` | `$a` — meeting / conference name<br>`$4` — LoC relator code | Same as 100, but with ?agent a bffi:Meeting on a primary contribution | — |
| `245` | `00` | `$a` — main title<br>`$b` — subtitle<br>`$c` — statement of responsibility | ?m bffi:title / bffi:Title / bffi:mainTitle (mandatory) + bffi:subtitle (optional); responsibility comes from ?m bffi:responsibilityStatement | First bffi:title block wins. See known limitations below. |
| `260` | `##` | `$a` — publication / distribution statement | ?m bffi:publicationStatement ?text — the transcribed pre-RDA statement | BFFI carries the publication statement as a single transcribed literal — the round-trip emit puts the whole string in $a. |
| `300` | `##` | `$a` — extent<br>`$c` — dimensions | ?m bffi:extent / bffi:Extent / rdfs:label (for $a) and ?m bffi:dimensions literal (for $c) | First-extent-wins for multi-extent records (rare). |
| `336` | `##` | `$a` — RDA content type code | ?m bffi:workManifested ?work . ?work bffi:content <http://id.loc.gov/vocabulary/contentTypes/{code}> | — |
| `337` | `##` | `$a` — RDA media type code | ?m bffi:media <http://id.loc.gov/vocabulary/mediaTypes/{code}> | — |
| `338` | `##` | `$a` — RDA carrier type code | ?m bffi:carrier <http://id.loc.gov/vocabulary/carriers/{code}> | — |
| `500` | `##` | `$a` — general note text | ?m bffi:note [a bffi:Note ; rdfs:label ?text] | All bffi:Note blocks emit as 500 today. Per-note-type dispatch (504 bibliography / 505 contents / 520 summary / 521 audience / etc.) is a follow-on — needs to read the additional `rdf:type` on the note bnode (e.g. <http://id.loc.gov/vocabulary/mnotetype/physical>). |
| `600` | `##` | `$a` — personal name subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Person` | — |
| `610` | `##` | `$a` — corporate name subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Organization` | — |
| `611` | `##` | `$a` — meeting / conference subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Meeting` | — |
| `630` | `##` | `$a` — uniform title subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Title` | — |
| `648` | `##` | `$a` — chronological term subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Temporal` | — |
| `650` | `##` | `$a` — topical term subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Topic` | — |
| `651` | `##` | `$a` — geographic name subject heading | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:Place` | — |
| `655` | `##` | `$a` — genre / form term | ?m bffi:workManifested ?work . ?work bffi:subject ?subject . ?subject rdfs:label ?label — `?subject` is typed `bffi:GenreForm` | — |
| `700` | `##` | `$a` — personal name<br>`$4` — LoC relator code | Same chain as 100, but the contribution is NOT typed bffi:PrimaryContribution — added-entry contributors land in 7XX | — |
| `710` | `##` | `$a` — corporate / jurisdiction name<br>`$4` — LoC relator code | Same as 700, but with ?agent a bffi:Organization or bffi:Jurisdiction | — |
| `711` | `##` | `$a` — meeting / conference name<br>`$4` — LoC relator code | Same as 700, but with ?agent a bffi:Meeting | — |

_28 MARC tags currently emitted._

<!-- END AUTO: shipped -->

## Known limitations

Some MARC fields cannot be reconstructed byte-identical from BFFI alone:

- **`bffi:readMarc382`** is a *synthesised* literal, not the verbatim source MARC 382 string. The forward conversion decomposes MARC 382 into a structured `bf:ensemble` → `bf:Ensemble` tree without preserving the source field verbatim. The reverse converter reconstructs a best-effort summary from the decomposed labels; the round-trip 382 is not byte-identical to the source.

- **The leader** is currently a 24-character placeholder (`"00000nam a2200000 a 4500"`). Per-position population from BFFI state (record-type, bibliographic-level, encoding-level, character-coding, descriptive-cataloguing-form) is deferred. The placeholder is valid MARC; downstream tooling that depends on specific leader positions sees the same value for every record.

- **Primary vs variant title discrimination** for MARC 245 currently picks the first `bffi:title` block on the Manifestation. The proper discrimination is to inspect the `bffi:marcKey` first 3 chars (e.g. blocks with marcKey starting `246` / `740` are variants, not the primary title).

- **First-extent-wins** for MARC 300 in multi-extent records. Rare in the corpus; multiple 300 datafields aren't yet emitted in those cases.

Each limitation has a tracking entry in the BFFI ontology-limitations registry when it's a true ontology shortfall vs an implementation-deferred case.
