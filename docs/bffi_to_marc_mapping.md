# `bffi:*` → MARCXML mapping reference

## Background

This document describes the **reverse-direction** conversion: BFFI canonical Turtle → reconstructed MARCXML. It is the round-trip pair of the forward-direction (BIBFRAME → BFFI) mapping reference. For each MARC field the reverse converter emits, this doc names the source BFFI predicates the converter walks, the MARC tag/indicators/subfields produced, and any known caveats.

The Shipped and Pending tables below are **auto-generated** by the `bffi-pipeline regenerate-marc-mapping` command from registered emit metadata declared in the reverse converter. Adding a new MARC field family means: write an extract helper, decorate it with an emit-metadata annotation, wire the result into the record builder. The doc updates on the next regeneration.

### Cardinal rule — `bffi-prov:` is off limits

The reverse converter **must not consult `bffi-prov:` predicates** when deciding what content to emit. `bffi-prov:` is the pipeline-internal provenance namespace (Activity records, decision-audit tags, etc.); reading it during MARC reconstruction would silently couple the round-trip to information the BFFI namespace alone wouldn't carry. An automated check fails the build if any `bffi-prov:` reference creeps into the reverse-converter source.

`bffi-prov:` data is fair game for orthogonal concerns — UI panels, diff-pairing lineage tokens — but never for emit content.

### Status legend

| Status | Meaning |
|---|---|
| **shipped** | The reverse converter emits this MARC tag from the BFFI predicates documented in the row. Listed in the Shipped table below. |
| **pending** | The MARC tag appears in the 20 k bench's source MARC distribution but the reverse converter doesn't yet emit it — a candidate for a follow-on commit. The `20 k bench lost count` column shows the per-tag impact. |

## Shipped — MARC tags the reverse converter currently emits

The first column is the MARC tag (or `leader` for the record-level pseudo-tag). The Ind1 / Ind2 column shows the indicators the converter writes; `#` represents the MARC blank indicator (a literal space in MARCXML); `—` indicates a control field or leader with no indicators. The BFFI source column names the predicates the extract function walks; the Notes column flags caveats.

<!-- BEGIN AUTO: shipped -->

| MARC tag | Ind1 / Ind2 | Subfields | BFFI source | Notes |
|---|---|---|---|---|
| `leader` | `—` | — | static placeholder | 24-character placeholder ('00000nam a2200000 a 4500'). Per-position population from BFFI state (record-type, bibliographic-level, encoding-level, etc.) is a follow-on. |
| `001` | `—` | — | ?m bffi:identifiedBy [a bffi:Local ; rdf:value ?bib_id] (fallback: parse from the Manifestation URI fragment) | — |
| `020` | `##` | `$a` — ISBN value | ?m bffi:identifiedBy [a bffi:Identifier ; bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn> ; rdf:value ?isbn] | — |
| `022` | `##` | `$a` — ISSN value | ?m bffi:identifiedBy [a bffi:Identifier ; bffi:source <http://id.loc.gov/vocabulary/identifiers/issn> ; rdf:value ?issn] | — |
| `041` | `##` | `$a` — 3-letter language code (one per language) | ?m bffi:language <http://id.loc.gov/vocabulary/languages/{code}> (local-name extraction; sorted for determinism) | — |
| `245` | `00` | `$a` — main title<br>`$b` — subtitle<br>`$c` — statement of responsibility | ?m bffi:title / bffi:Title / bffi:mainTitle (mandatory) + bffi:subtitle (optional); responsibility comes from ?m bffi:responsibilityStatement | First-title-block wins. Primary-vs-variant discrimination (by bffi:marcKey first 3 chars) is a follow-on. The $c responsibility statement is contributed by a separate _extract_responsibility_statement helper. |
| `300` | `##` | `$a` — extent<br>`$c` — dimensions | ?m bffi:extent / bffi:Extent / rdfs:label (for $a) and ?m bffi:dimensions literal (for $c) | First-extent-wins for multi-extent records (rare). |
| `600/610/611/630/648/650/651/655` | `##` | `$a` — subject heading label | ?m bffi:workManifested ?work . ?work bffi:subject ?subj_node . ?subj_node rdfs:label ?label . MARC tag dispatched from the subject node's URI fragment (e.g. #Topic650-N → 650, #Place651-N → 651). | Only the 6XX subject tag set is emitted by this routing; URI fragments outside the set are skipped (handled by other field-family routings). |

_8 MARC tags currently emitted._

<!-- END AUTO: shipped -->

## Pending — MARC tags observed in the corpus but not yet emitted

Prioritised by the 20 k bench's eval-harness `lost` distribution. Each entry is a candidate for a follow-on commit (decorate a new extract helper with the emit metadata and wire it into the record builder).

<!-- BEGIN AUTO: pending -->

| MARC tag | 20 k bench `lost` count | Notes |
|---|---|---|
| `005` | 14,000 | Record modification date (control field). Reads bffi:adminMetadata / bffi:changeDate. |
| `008` | 19,000 | Control field — fixed-position record metadata. Reads adminMetadata (changeDate, descriptionLanguage) + language + publicationStatement to populate the 40 character positions. |
| `084` | 21,000 | Other classification number (non-Helmet-local classification). |
| `091/092/094/095/097` | 75,000 | Helmet-local classifications (combined count). bf:Classification blocks with Finnish source codes; reverse converter needs to pick the right MARC 09X tag by classification source. |
| `260` | 19,000 | Publication/distribution statement (pre-RDA). marc2bibframe2 normalises 260 and 264 records into a single bf:ProvisionActivity; the reverse converter chooses 260 vs 264 based on the activity shape. See L-01 in the limitations registry for the round-trip convention. |
| `336/337/338` | 29,000 | RDA content/media/carrier types (combined count). Maps from bffi:content / bffi:media / bffi:carrier predicates with LoC RDA vocabulary URIs. |
| `500` | 7,000 | General notes. Walks bffi:note / bffi:Note / rdfs:label; marcKey first 3 chars discriminate 500 / 504 / 505 / 520 / etc. |
| `700` | 86,000 | Added entries — personal-name contributors. Walks bffi:contribution / bffi:Contribution / bffi:agent + bffi:role on the Work; needs to discriminate primary (MARC 100) from added entries (MARC 700) and emit relator codes ($4 / $e). |
| `710` | 17,000 | Corporate-name contributors (parallel to 700). |
| `730` | 62,000 | Added uniform titles. Walks bffi:title blocks whose bffi:marcKey first 3 chars are 730 (variant titles discriminator). |
| `740` | 15,000 | Added analytical titles (parallel to 730). |
| `852` | 21,000 | Holdings location (bffi:Item / bffi:heldBy). On the rewrite branch the Item class is intentionally deferred until a concrete consumer asks for it (see project notes). |

_12 MARC tags pending — 385,000 `lost` records in the 20 k bench combined._

<!-- END AUTO: pending -->

## Adding a new MARC family

The pattern for adding a MARC field family to the reverse converter is the "one MARC family per commit" discipline documented in the rewrite branch's plan:

1. **Survey** the BFFI source. Pick a real corpus record that uses the target MARC field; trace which BFFI predicates carry the equivalent content. The forward-direction mapping reference gives the `bf:* → bffi:*` trace; the reverse direction reads the same `bffi:*` predicates.

2. **Implement an extract helper** that walks the BFFI predicates and returns the field-relevant data (typically as a small frozen dataclass).

3. **Decorate** with the emit-metadata annotation declaring the MARC tag, indicators, subfield codes + descriptions, and a brief description of the BFFI walk. One decorator can declare multiple MARC tags if the extractor contributes to several (e.g. an identifier extractor handling both ISBN and ISSN).

4. **Wire into the record builder.** Add the extract call to the orchestrator and the emit logic to the record builder (building MARC `datafield` / `controlfield` elements).

5. **Test.** Use the minimal-BFFI fixture builder, add the field-specific triples, assert the MARCXML output. Patterns from existing tests cover happy paths, optional subfields, and unsupported-source skips.

6. **Regenerate** the doc: `bffi-pipeline regenerate-marc-mapping`. The auto-table picks up the new entry.

The pre-commit hook fails on lint + tests, so each follow-on commit ships a passing build.

## Known limitations

Some MARC fields cannot be reconstructed byte-identical from BFFI alone:

- **`bffi:readMarc382`** is a *synthesised* literal, not the verbatim source MARC 382 string. The forward conversion's XSLT decomposes 382 into a structured `bf:ensemble` → `bf:Ensemble` tree without preserving the source field as a `bflc:marcKey` on the ensemble bnode (unlike 6XX subject blocks or X30 uniform titles). The reverse converter reconstructs a best-effort summary from the decomposed labels; the round-trip 382 is not byte-identical to the source.

- **The leader** is currently a 24-character placeholder (`"00000nam a2200000 a 4500"`). Per-position population from BFFI state (record-type, bibliographic-level, encoding-level, character-coding, descriptive-cataloguing-form) is a follow-on commit. The placeholder is valid MARC; downstream tooling that depends on specific leader positions sees the same value for every record.

- **Primary vs variant title discrimination** for MARC 245 currently picks the first `bffi:title` block on the Manifestation. The proper discrimination is to check `bffi:marcKey` first 3 chars (e.g. blocks with marcKey starting `246` / `740` are variants, not the primary title). A follow-on commit lands this.

- **First-extent-wins** for MARC 300 in multi-extent records. Rare in the corpus; a follow-on commit can emit multiple 300 datafields when appropriate.

Each limitation has a tracking entry in the BFFI ontology-limitations registry when it's a true ontology shortfall vs an implementation-deferred case.
