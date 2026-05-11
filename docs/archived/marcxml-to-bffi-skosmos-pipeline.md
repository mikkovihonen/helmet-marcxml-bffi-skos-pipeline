# MARCXML → BFFI → Skosmos Pipeline

> **Archived.** This document captured the original end-to-end technical
> specification for the BFFI pipeline. It is retained for historical context
> and section-level back-references from older commits, plans, and source
> comments. **Do not treat it as the source of truth for ongoing work.**
> Live references have moved as follows:
>
> - Committed identifiers, URI namespaces, `bffi-prov` enums and Activity
>   classes → [`CLAUDE.md`](../../CLAUDE.md).
> - Toolchain (rdflib, Fuseki, mlx-lm, embeddings, FAISS, LangChain) →
>   [`../tech-stack.md`](../tech-stack.md).
> - Local inference setup → [`../local-inference.md`](../local-inference.md).
> - Validation boundaries → [`../validation-strategy.md`](../validation-strategy.md).
> - Forward-looking design changes → [`../proposals/`](../proposals/).
> - Sequenced execution detail → [`../plans/`](../plans/).
>
> The historical milestone-ordered build plan lives at
> [`BUILD_PLAN.md`](BUILD_PLAN.md) (also archived).

---

A practical guide to converting MARCXML records into BFFI (the Finnish BIBFRAME profile) and publishing Work and Expression authority records via Skosmos, including LLM-assisted deduplication, provenance logging, and evaluation.

> **Note on document scope.** This is the **technical specification** — patterns, code samples, and design rationale for each component. For the build plan (milestones, repository structure, definitions of done), see `CLAUDE.md` and `docs/archived/BUILD_PLAN.md`. For validation boundaries, local-inference setup, and external dependencies, see the corresponding files in `docs/`.
>
> **Post-draft updates.** This document was originally drafted while the project was still considering Anthropic's API for the LLM judge. Subsequent decisions committed the project to local inference on Apple Silicon (no paid API services) and assigned URN namespaces under `http://urn.fi/URN:NBN:fi:bib:`. The code samples below have been updated; the patterns and reasoning are unchanged.

---

## 0. Operating constraints and committed identifiers

### Operating context

- **Pro bono project** for the National Library of Finland.
- **Source corpus:** the Helmet consortium catalog (Helsinki / Espoo / Vantaa / Kauniainen public libraries) — target ~800,000 bibliographic records.
- **Target hardware:** MacBook Pro M5 Max, 128 GB unified memory.
- **No paid LLM APIs.** All LLM inference runs locally on Apple Silicon.
- Open-source tooling only.
- **Dual licensing:** code Apache-2.0 (matching NLF tools); published RDF data CC0 (matching Finto vocabularies).
- No telemetry, no error reporting.

### Committed identifiers

| Concern | Value |
|---|---|
| Work URI namespace | `http://urn.fi/URN:NBN:fi:bib:work:` |
| Expression URI namespace | `http://urn.fi/URN:NBN:fi:bib:expression:` |
| Helmet source URI (in `bf:identifiedBy`) | `http://urn.fi/URN:NBN:fi:bib:source:helmet` |
| Named-graph base for Fuseki | `http://urn.fi/URN:NBN:fi:bib:graph:` |
| `bffi-prov` namespace (provenance vocabulary) | `http://urn.fi/URN:NBN:fi:schema:bffi-prov#` |
| `bffi:adminMetadata` linking property | `http://urn.fi/URN:NBN:fi:schema:bffi:adminMetadata` (links Works/Expressions to a `bffi:AdminMetadata` summary block; layered on top of the PROV-O graph, see § 8) |
| Authority priority — persons / corporate bodies | KANTO first, VIAF as fallback for non-Finnish creators. MARC 6XX subject-as-name fields (`#Agent600/610/611-N` URI fragments from marc2bibframe2) route to KANTO too — a biography's subject is a person, not a topic. |
| Authority priority — subjects | YSO (general; loaded together with YSO-Paikat for places + YSO-Aika for time periods, all sharing the YSO concept namespace), then LCSH for English `$2 lcsh` literals. KAUNO + SLM (fiction genre/form), with LCGFT for cataloguer-cited English genre/form `$0` URIs. MUSO (music). YSO is also a fallback graph for `genre_form` lookups when cataloguers tag heterogeneous content with the legacy `$2 kaunokki`. |
| Display language priority for `skos:prefLabel` | `fi`, `sv`, `en` |
| Documentation language | English |

These are not up for renegotiation without surfacing the decision. See `CLAUDE.md` for the full operating-constraints rationale and `docs/archived/BUILD_PLAN.md` for the milestone-by-milestone build plan.

---

## 1. Background: what BFFI changes vs vanilla BIBFRAME

The Library of Congress's BIBFRAME 2.0 has only `bf:Work` and `bf:Instance`. BFFI (the National Library of Finland's profile, namespace `http://urn.fi/URN:NBN:fi:schema:bffi:`) splits `bf:Work` into disjoint classes `bffi:Work` and `bffi:Expression` corresponding to RDA. A useful framing from the LKD project: BIBFRAME works are essentially expressions; for every expression there exists a `bf:Work`. To add in RDA Works we just isolate some of the `bf:Work` properties and subclasses for `bffi:Work` and leave some for the Expression.

Conversion therefore must do two things that LoC's tooling doesn't:

1. Split each `bf:Work` into a `bffi:Work` (language/expression-independent) and one or more `bffi:Expression` (language, content type, etc.).
2. Re-route every property to the correct one of the two.

Current model is **BFFI 1.0.0**, published 2025-01-02.

**Canonical schema reference.** The full BFFI 1.0.0 ontology is vendored locally at [`docs/lkd.rdf`](docs/lkd.rdf) (RDF/XML, ~4600 lines). It is the **single source of truth** for all `bffi:*` class names, predicate names, and `rdfs:domain` / `rdfs:range` declarations. Whenever this spec or the build plan introduces a new BFFI term, the term must be looked up in `docs/lkd.rdf` first. The published URL `https://schema.finto.fi/bffi/` is currently 403-protected outside the Finto network, which is why the local copy is the working reference.

The schema also carries explicit cross-vocabulary alignment via `bffi-meta:exactMatch`, `bffi-meta:closeMatch`, `bffi-meta:broadMatch`, and `bffi-meta:relatedValueVocabulary` predicates pointing at BIBFRAME, BFLC, and RDA. This pipeline doesn't emit those predicates (they're schema-level metadata, not record-level), but downstream tools querying for "the BIBFRAME equivalent of `bffi:Work`" can follow them.

---

## 2. Pipeline overview

| Stage | Tool | Purpose |
|-------|------|---------|
| 1. Preprocess MARCXML | Catmandu / LoC preprocessor | Split multi-instance records, normalize quirks |
| 2. MARCXML → BIBFRAME 2.0 | LoC `marc2bibframe2` XSLT | Standard step, produces `bf:Work` + `bf:Instance` |
| 3. BIBFRAME → BFFI | SPARQL CONSTRUCT (custom) | Split `bf:Work` into `bffi:Work` + `bffi:Expression` |
| 4. Work-key calculation & dedup | Embeddings + LLM judge | Merge Works across records into authority Works |
| 5. Reconciliation | LLM-assisted matching | Link agents/subjects to KANTO, YSO, VIAF |
| 6. SKOS overlay + Skosify | Skosify with `--infer` | Dual-type as `skos:Concept` via RDFS inference |
| 7. Load to triple store | Jena Fuseki + jena-text | Backend for Skosmos |
| 8. Publish | Skosmos `config.ttl` | Browse & search via Finto-style UI |

`NatLibFi/bib-rdf-pipeline` is the closest reference implementation but was archived June 2025; treat it as inspiration for pipeline shape rather than a drop-in tool.

---

## 3. SPARQL CONSTRUCT pattern

Two CONSTRUCTs — one per output class — keyed off a deterministic SHA-1 of the source `bf:Work` URI so they round-trip and stay linked.

```sparql
PREFIX bf:    <http://id.loc.gov/ontologies/bibframe/>
PREFIX bflc:  <http://id.loc.gov/ontologies/bflc/>
PREFIX bffi:  <http://urn.fi/URN:NBN:fi:schema:bffi:>
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
PREFIX arq:   <http://jena.apache.org/ARQ/function#>

# --- Pass 1: the bffi:Work (language-independent core) ---
CONSTRUCT {
  ?workURI a bffi:Work ;
           bffi:hasExpression  ?exprURI ;
           bffi:contribution   [ a bffi:PrimaryContribution ;
                                 bffi:agent ?primaryAgent ] ;
           bffi:subject        ?subject ;
           bffi:classification ?class ;
           bffi:originDate     ?originDate ;
           bffi:genreForm      ?workGenre ;
           bffi:marcKey        ?marcKey ;
           skos:prefLabel      ?workLabel .
}
WHERE {
  ?bfWork a bf:Work .

  BIND( IRI(CONCAT("http://urn.fi/URN:NBN:fi:bib:work:",
            arq:sha1(STR(?bfWork)))) AS ?workURI )
  BIND( IRI(CONCAT("http://urn.fi/URN:NBN:fi:bib:expression:",
            arq:sha1(STR(?bfWork)))) AS ?exprURI )

  # Primary creator only (translators, editors → Expression)
  OPTIONAL {
    ?bfWork bf:contribution ?c .
    ?c a bflc:PrimaryContribution ;
       bf:agent ?primaryAgent .
  }
  OPTIONAL { ?bfWork bf:subject        ?subject }
  OPTIONAL { ?bfWork bf:classification ?class }
  OPTIONAL { ?bfWork bf:originDate     ?originDate }
  OPTIONAL { ?bfWork bf:genreForm      ?workGenre }
  OPTIONAL { ?bfWork bflc:marcKey      ?marcKey }
  OPTIONAL {
    ?bfWork bf:title ?t . ?t bf:mainTitle ?workLabel .
  }
}

# --- Pass 2: the bffi:Expression (language-dependent surface) ---
CONSTRUCT {
  ?exprURI a bffi:Expression ;
           bffi:expressionOf    ?workURI ;
           bffi:language        ?language ;
           bffi:content         ?contentType ;
           bffi:title           ?title ;
           bffi:contribution    [ a bffi:Contribution ;
                                  bffi:agent ?otherAgent ] ;
           bffi:summary         ?summary ;
           bffi:note            ?note ;
           skos:prefLabel       ?exprLabel .
}
WHERE {
  ?bfWork a bf:Work .
  BIND( IRI(CONCAT("http://urn.fi/URN:NBN:fi:bib:work:",
            arq:sha1(STR(?bfWork)))) AS ?workURI )
  BIND( IRI(CONCAT("http://urn.fi/URN:NBN:fi:bib:expression:",
            arq:sha1(STR(?bfWork)))) AS ?exprURI )

  OPTIONAL { ?bfWork bf:language        ?language }
  OPTIONAL { ?bfWork bf:content         ?contentType }
  OPTIONAL { ?bfWork bf:summary         ?summary }
  OPTIONAL { ?bfWork bf:note            ?note }
  # bf:tableOfContents is intentionally NOT routed here: BFFI's
  # bffi:tableOfContents has rdfs:domain bffi:Manifestation (per docs/lkd.rdf
  # line 3995), not Expression. The pipeline currently doesn't mint
  # Manifestation blocks; preserve that scope by dropping the predicate.

  # Non-primary contributions go here (translator, illustrator, editor)
  OPTIONAL {
    ?bfWork bf:contribution ?c .
    FILTER NOT EXISTS { ?c a bflc:PrimaryContribution }
    ?c bf:agent ?otherAgent .
  }

  OPTIONAL {
    ?bfWork bf:title ?t . ?t bf:mainTitle ?exprLabel .
    BIND( ?t AS ?title )
  }
}
```

**Notes:**

- Property allocation below has been verified against `docs/lkd.rdf` (the vendored BFFI 1.0.0 ontology — see § 1). Re-verify whenever BFFI publishes a minor revision; the schema is the canonical source for class names, predicate names, and `rdfs:domain` / `rdfs:range`.
- URI minting goes through `src/bffi_pipeline/uris.py` — never concatenate URI strings elsewhere. The CONSTRUCT shown here is the **raw** pass: it hashes the source `bf:Work` URI so the same input produces the same output on re-run. Canonical Works minted by the M8 merge stage hash a different canonical input (creator URI + original-language preferred title), so identical works across records collapse naturally. See `docs/archived/BUILD_PLAN.md` M1/M3 for both URI-minting rules.
- **Property routing is intentionally conservative.** BFFI's schema gives `bffi:title`, `bffi:note`, and `bffi:language` broader domains than Expression alone (the union covers Work too, or `rdfs:Resource` universally). The CONSTRUCT above pins them to Expression on purpose — a clean Work/Expression split is more useful for downstream RDA-FRBR consumers than mirroring the schema's broadest permissible domains. The Boundary-3 SHACL shape (§ 10) enforces this pipeline-level allocation, not the schema-level one.
- **Subclass typing is out-of-scope for v0.1.0.** BFFI distinguishes ~28 specific Work/Expression subclasses (`bffi:MusicWork`, `bffi:NotatedMusic`, `bffi:CartographyWork`, `bffi:CartographyExpression`, `bffi:SerialWork`, `bffi:SerialExpression`, `bffi:MovingImageWork`, `bffi:MovingImageExpression`, `bffi:MonographWork`, `bffi:MonographExpression`, `bffi:NonMusicAudioWork`, `bffi:NonMusicAudioExpression`, etc.). The CONSTRUCT above emits the bare `bffi:Work` / `bffi:Expression` types regardless of material type. Subclass-specific typing would require mapping each BIBFRAME subclass that `marc2bibframe2` emits (`bf:Text`, `bf:NotatedMusic`, `bf:Cartography`, `bf:MovingImage`, `bf:NonMusicAudio`, etc.) to its BFFI counterpart in Pass 1 and Pass 2 — a meaningful upgrade, but its own milestone.
- **Helmet identifier preservation.** Both passes must copy `bf:identifiedBy` from the source `bf:Work` onto the new `bffi:Work` *and* `bffi:Expression`, with `bf:source` = `<http://urn.fi/URN:NBN:fi:bib:source:helmet>`. Each raw Work and Expression carries the Helmet bib ID of the record it came from; M8 unions the identifier sets when raw Works merge into a canonical Work, so a canonical Work that absorbed N Helmet records carries N `bf:identifiedBy` triples. (See `docs/archived/BUILD_PLAN.md` M2/M3/M8.)
- **MARCXML inputs are UTF-8 only.** One record per file, named `<helmet_bib_id>.xml` (filename pattern `^\d+\.xml$`). Encoding discrepancies are hard errors at the Boundary-1 ingest check (§10) — the offending filename is surfaced and the record is skipped to `_errors.jsonl`. No silent transcoding from MARC-8 / Latin-1. See `docs/archived/BUILD_PLAN.md` M2 and `docs/validation-strategy.md` Boundary 1.
- Note: the `, skos:Concept` is intentionally absent from the `a` lines; Skosify will infer it from the RDFS overlay (see §5).

---

## 4. Skosmos `config.ttl`

Skosmos doesn't infer types — every resource needs both `bffi:Work`/`bffi:Expression` *and* `skos:Concept`. Either the CONSTRUCT emits both, or Skosify's `--infer` adds the SKOS typing during preprocessing.

```turtle
@prefix skosmos: <http://purl.org/net/skosmos#> .
@prefix void:    <http://rdfs.org/ns/void#> .
@prefix dc:      <http://purl.org/dc/terms/> .
@prefix skos:    <http://www.w3.org/2004/02/skos/core#> .
@prefix bffi:    <http://urn.fi/URN:NBN:fi:schema:bffi:> .
@prefix isothes: <http://purl.org/iso25964/skos-thes#> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix :        <#> .

# --- Type labels (Skosmos requires rdfs:label on every custom type) ---
bffi:Work       rdfs:label "Work"@en, "Teos"@fi, "Verk"@sv ;
                rdfs:subClassOf skos:Concept .
bffi:Expression rdfs:label "Expression"@en, "Ekspressio"@fi, "Uttryck"@sv ;
                rdfs:subClassOf skos:Concept .

# --- Fuseki backend ---
:bibframeEndpoint a void:Dataset ;
    void:sparqlEndpoint <http://localhost:3030/bffi/sparql> .

# --- The vocabulary entry ---
:bffiWorks a skosmos:Vocabulary, void:Dataset ;
    dc:title         "Finnish Authority Works and Expressions"@en,
                     "Suomalaiset auktoriteettiteokset ja -ekspressiot"@fi ;
    skosmos:shortName "bffi-works"@en ;
    dc:subject       :catBibliographic ;
    void:uriSpace    "http://urn.fi/URN:NBN:fi:bib:work:" ;
    skosmos:language "fi", "sv", "en" ;
    skosmos:defaultLanguage "fi" ;
    skosmos:sparqlGraph     <http://urn.fi/URN:NBN:fi:bib:graph:bffi-works> ;
    skosmos:sparqlEndpoint  <http://localhost:3030/bffi/sparql> ;
    skosmos:sparqlDialect   "JenaText" ;

    # The two custom types the user can filter by
    skosmos:indexShowClass  bffi:Work ;
    skosmos:indexShowClass  bffi:Expression ;

    # Group view — useful if you cluster Works with their Expressions
    skosmos:groupClass      isothes:ConceptGroup ;

    skosmos:showTopConcepts true ;
    skosmos:fullAlphabeticalIndex true .
```

**Common gotchas:**

- `void:uriSpace` must match the minted Work URI prefix `http://urn.fi/URN:NBN:fi:bib:work:` exactly, otherwise Skosmos won't recognize internal links.
- `skosmos:sparqlDialect "JenaText"` enables the `text:query` predicate Skosmos uses for fast label search; without it, search becomes painfully slow at scale. Verify `jena-text` is enabled in the Fuseki image.
- `skosmos:language` order doubles as the display-language priority. `"fi", "sv", "en"` matches Finland's official languages plus an international fallback; `skosmos:defaultLanguage "fi"` is the committed default.
- To display Works with their Expressions hierarchically, model the link with `skos:narrower`/`skos:broader` *in addition* to `bffi:hasExpression`/`bffi:expressionOf`. This is handled automatically by the Skosify overlay below.
- Both Fuseki (`stain/jena-fuseki:5.0.0`) and Skosmos (`ghcr.io/natlibfi/skosmos:3.2`) are pinned in `docker-compose.yml`; see `docs/archived/BUILD_PLAN.md` M10/M11 for the rationale.

---

## 5. Skosify: the overlay-plus-inference approach

The Skosify `[types]` section is **destructive** — it replaces source classes with SKOS classes. Instead, declare the BFFI/SKOS subclass relationships in a small overlay file and let Skosify's RDFS inference add `skos:Concept` while keeping the BFFI types intact. This is the more robust path because new BFFI subclasses only require an overlay update, not a pipeline change.

### Overlay file

```turtle
# bffi-skos-overlay.ttl  (load alongside your converted data)
@prefix bf:   <http://id.loc.gov/ontologies/bibframe/> .
@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

bffi:Work       rdfs:subClassOf skos:Concept ;
                rdfs:label "Work"@en, "Teos"@fi, "Verk"@sv .
bffi:Expression rdfs:subClassOf skos:Concept ;
                rdfs:label "Expression"@en, "Ekspressio"@fi, "Uttryck"@sv .

bffi:hasExpression rdfs:subPropertyOf skos:narrower ;
                   rdfs:label "has expression"@en, "ekspressio"@fi .
bffi:expressionOf  rdfs:subPropertyOf skos:broader ;
                   rdfs:label "expression of"@en, "ekspressio teoksesta"@fi .

# Declare the Helmet source URI as a real entity so bf:identifiedBy triples on
# Works and Expressions resolve to a labeled resource in Skosmos rather than a
# dangling URI. Helmet is the joint catalog of the public libraries of Helsinki,
# Espoo, Vantaa, and Kauniainen; the underlying ILS is Sierra, but that is
# implementation detail and is not preserved in the data — provenance attributes
# records to Helmet, not Sierra.
<http://urn.fi/URN:NBN:fi:bib:source:helmet>
    a bf:Source, bffi:Source ;     # bffi:Source is owl:equivalentClass of bf:Source per docs/lkd.rdf line 609 — dual-type for BFFI-native consumers.
    rdfs:label   "Helmet"@en, "Helmet"@fi, "Helmet"@sv ;
    bf:code      "helmet-bib" ;
    rdfs:comment "Joint catalog of the public libraries of Helsinki, Espoo, Vantaa, and Kauniainen."@en .
```

### Skosify config

```ini
# bffi.cfg

[namespaces]
skos  = http://www.w3.org/2004/02/skos/core#
skosx = http://purl.org/iso25964/skos-thes#
bf    = http://id.loc.gov/ontologies/bibframe/
bflc  = http://id.loc.gov/ontologies/bflc/
bffi  = http://urn.fi/URN:NBN:fi:schema:bffi:
dct   = http://purl.org/dc/terms/

[options]
# RDFS subclass + subproperty inference. This dual-types every
# bffi:Work / bffi:Expression as skos:Concept (and lifts
# bffi:hasExpression to skos:narrower) WITHOUT destroying the BFFI types.
infer = on

namespace = http://urn.fi/URN:NBN:fi:bib:work:
label = Finnish Authority Works and Expressions
default_language = fi
preflabel_policy = shortest

mark_top_concepts = true
narrower = true
keep_related = true
break_cycles = true

cleanup_classes = false
cleanup_properties = false
cleanup_unreachable = false

[types]
# Empty — relying on inference instead.

[literals]
bf:mainTitle    = skos:prefLabel
bf:variantTitle = skos:altLabel

[relations]
# Empty — bffi:hasExpression / bffi:expressionOf are lifted via inference.
```

### Run

```bash
skosify --config bffi.cfg \
        --output bffi-works-skosified.ttl \
        bffi-works.ttl bffi-skos-overlay.ttl
```

**Why overlay-plus-inference wins:** when BFFI publishes a new version with additional Work/Expression subclasses, you only extend the overlay with new `rdfs:subClassOf skos:Concept` declarations and re-run. The CONSTRUCT pipeline stays untouched. Destructive `[types]` mappings would force conversion updates every time the model evolves.

---

## 6. LLM-assisted work-key calculation and deduplication

The classic algorithm — normalized author + uniform title → key — breaks on transliteration, abbreviated forms, translations, and cataloguing variation. A three-stage funnel keeps LLMs constrained and auditable:

### Stage 1 — Deterministic blocking

Compute a cheap rule-based key from MARC 100/240/245 of the source Helmet record (normalized creator surname + first significant title word + content type code). Only consider pairs within the same block. This eliminates >99% of comparisons before any model runs. Implementation lives in `src/bffi_pipeline/stages/workkey.py`.

### Stage 2 — Embedding similarity

Within each block, embed a structured string per record (fixed field order: `"creator: <X> | title: <Y> | language: <Z> | year: <Y> | type: <T>"`). The committed embedding model is **BGE-M3** (1024-dim, multilingual; subject to a benchmark against `intfloat/multilingual-e5-large` and `jinaai/jina-embeddings-v3` against the gold set per `docs/archived/BUILD_PLAN.md` M5 before locking in). Index with **FAISS `IndexHNSWFlat`** (cosine via L2-normalized inner product, `M=32`, `efConstruction=200`). Persist the index to disk so M6 reloads rather than rebuilds.

Thresholds are tightened from "frontier-API" defaults (≥ 0.92 / ≤ 0.75) because local LLM inference is throughput-bound: a wider gray zone produces more pairs the judge has to handle, and at 800k records that becomes a multi-week run rather than multi-night. The narrower gray zone roughly halves judge workload at modest cost in accuracy (see §11 for the throughput numbers):

- **≥ 0.90 → auto-merge**
- **≤ 0.78 → reject**
- **0.78–0.90 → escalate to LLM judge**

Validate the chosen values against the gold set before treating them as final. Embeddings genuinely understand that "Tolstoi" and "Толстой" are the same person and that "Sota ja rauha" and "War and Peace" share a referent.

### Stage 3 — LLM judge for the gray zone

Send only ambiguous pairs to a model with a structured-output schema. Force it to quote field values rather than free-associate. Temperature 0, seeded. The committed setup is a **two-stage Qwen3 cascade**: Qwen3 32B Instruct (4-bit MLX) as the primary judge; pairs returning `uncertain` or `same_work` with confidence < 0.85 re-run on Qwen3 72B Instruct as a second opinion. Both decisions are logged to provenance with distinct `bffi-prov:stage` values (`"llm-judge-primary"` and `"llm-judge-second-opinion"`). See §11 for the memory budget, throughput numbers, and the cascade rationale.

### Operational principles

- **Provenance:** store the model name, version, prompt hash, and rationale alongside every merge decision — including negative ones. Ground every judgment in field-level evidence. See §8.
- **Asymmetric thresholds:** false merges cost much more than false splits. Bias toward keeping things separate when uncertain.
- **Boundary-4 semantic validators** sit between the model output and the cache: `decision="uncertain"` requires `confidence ≤ 0.7`; `decision="same_work"` requires non-empty `matching_fields`; rationale must be ≥ 20 chars and free of stub phrases ("I don't know", "n/a", "unable to determine", "not sure"). Validation failures share the retry path with JSON parse failures (max 2 retries → log `uncertain` with the validation error). **Validation-failed responses must not be cached** — caching cements bad outputs across re-runs.
- **Two distinct confidence cutoffs — don't conflate them.** The cascade re-run threshold (§7, §11) is `confidence < 0.85`: any `same_work` decision below this gets a Qwen3 72B second opinion. The reconciliation fallback threshold (M9, see `docs/archived/BUILD_PLAN.md`) is `confidence < 0.80`: below this, the M9 LLM-pick result is discarded in favour of the highest-lexical candidate, and the canonical Work's AdminMetadata is tagged `bffi:descriptionAuthentication = <bib:auth/needs-review>` (see § 8 "AdminMetadata view"). Cascade applies to Work-merge decisions; reconciliation-fallback applies to authority-URI selection.
- **Use LLMs for normalization too:** giving the model a creator string and a list of KANTO/VIAF candidates and asking it to pick the right URI is a much better-bounded task than open-ended dedup.

### Finnish-context shortcut

Authority priority is committed:

- **Persons and corporate bodies:** KANTO first; VIAF as fallback for non-Finnish creators not found in KANTO. Don't query VIAF for Finnish authors who are in KANTO.
- **General subjects:** YSO.
- **Fiction genre/form:** KAUNO.
- **Music subjects/forms:** MUSO.

Embed MARC 100/700 strings, retrieve top-k candidates from the relevant authority, let the LLM pick from the candidate list (only when lexical similarity is ambiguous; a single high-similarity hit goes through deterministically). The best work-key after all this is *not* a string — it's the URI of the canonical creator plus the URI of the original-language preferred title. That key is stable under language, transliteration, and edition variation. See `docs/archived/BUILD_PLAN.md` M9 for the five-tier reconciliation decision logic (tier-0 local-prefLabel-match / lexical / LLM-pick / fallback-with-review-flag / unreconciled). Tier-0 takes the locally-loaded M11 option 3b authority graphs (YSO + YSO-Paikat + YSO-Aika sharing one Fuseki named graph; KAUNO; SLM; MUSO; LCGFT; LCSH) as the first port of call: an exact `skos:prefLabel` match against the locally-loaded graph binds deterministically with no `api.finto.fi` round-trip — the dominant path for YSA-tagged subject literals because YSO inherited the YSA prefLabels unchanged in the 2014-2018 vocabulary merge. Per-kind graph routing in tier-0: `subject` queries YSO (with sub-vocabs) then LCSH; `genre_form` queries KAUNO + SLM + LCGFT then falls through to YSO so cataloguer-tagged `$2 kaunokki` (the legacy KAUNO name) doesn't strand temporal / place literals that live in YSO-Aika / YSO-Paikat; `music_form` queries MUSO. MARC 6XX subject-as-name fields (`#Agent6(00|10|11)-N` URI fragments minted by marc2bibframe2) route to KANTO so a biography of Pekurinen carries `bffi:creator → biographer-kanto-uri` AND `bffi:subject → pekurinen-kanto-uri` distinctly — `_apply_canonical_link` dispatches on `predicate_uri` rather than `kind`, so a person-kind request from the *subject* walker never accidentally writes `bffi:creator`.

---

## 7. The stage-3 LLM judge

The judge runs against a **local OpenAI-compatible server** (Ollama on `:11434` for development; mlx_lm on `:8000` for production batches). Both expose the same chat-completions API, so the application code talks to either through `langchain-openai` pointed at `LLM_BASE_URL`. The committed primary model is **Qwen3 32B Instruct, MLX 4-bit quantization**; see §11 for the cascade and the throughput rationale.

The prompt is a versioned file (`prompts/judge_v1.txt`) hashed at startup so the hash can be logged with every provenance record. The inline `SYSTEM` and `EXAMPLES` blocks below are reproduced for narrative purposes; production code reads them from the file.

```python
"""
Stage-3 work-merge judge for ambiguous candidate pairs.
Run only on pairs with embedding similarity in the gray zone (0.78–0.90).
"""

import os
import re
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.globals import set_llm_cache
from langchain_community.cache import SQLiteCache

# Cache writes happen only after Boundary-4 validation passes — see below.
set_llm_cache(SQLiteCache(database_path=".workmerge_cache.db"))


# --- Schemas ---------------------------------------------------------------

class WorkRecord(BaseModel):
    """One side of a candidate pair. Fields are taken straight from MARC/BFFI."""
    record_id: str
    creator: Optional[str] = None
    creator_uri: Optional[str] = None
    preferred_title: Optional[str] = None
    variant_titles: list[str] = []
    original_language: Optional[str] = None
    expression_language: Optional[str] = None
    content_type: Optional[str] = None
    date_of_origin: Optional[str] = None
    publication_year: Optional[str] = None
    notes: list[str] = []


_STUB_PHRASES = ("i don't know", "unable to determine", "n/a", "not sure")


class WorkMatchDecision(BaseModel):
    """Structured judgment. The model fills this — nothing else is allowed."""
    decision: Literal["same_work", "different_work", "uncertain"]
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0.0–1.0. Use <0.7 when uncertain; reserve >0.9 for clear cases."
    )
    rationale: str = Field(
        min_length=20,
        description=(
            "2–4 sentences citing specific field values from BOTH records. "
            "Do not introduce facts not present in the inputs."
        )
    )
    matching_fields: list[str] = Field(default_factory=list)
    diverging_fields: list[str] = Field(default_factory=list)

    # --- Boundary 4 semantic validators (see §10) -------------------------
    @model_validator(mode="after")
    def _coherent_uncertain(self) -> "WorkMatchDecision":
        if self.decision == "uncertain" and self.confidence > 0.7:
            raise ValueError(
                "decision='uncertain' is incoherent with confidence > 0.7"
            )
        return self

    @model_validator(mode="after")
    def _same_work_needs_evidence(self) -> "WorkMatchDecision":
        if self.decision == "same_work" and not self.matching_fields:
            raise ValueError(
                "decision='same_work' requires at least one matching_field"
            )
        return self

    @model_validator(mode="after")
    def _rationale_is_substantive(self) -> "WorkMatchDecision":
        text = self.rationale.strip()
        if len(text) < 20:
            raise ValueError("rationale shorter than 20 characters")
        lowered = text.lower()
        if any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in _STUB_PHRASES):
            raise ValueError("rationale contains stub phrase")
        return self


# --- Prompt ----------------------------------------------------------------
# Production code reads these from prompts/judge_v1.txt and hashes the file.

SYSTEM = """You are a cataloguing assistant deciding whether two bibliographic
records describe the SAME RDA Work.

A Work is the abstract intellectual or artistic creation, independent of any
particular language, edition, format, or performance. Apply these rules:

- Translations of one creation are the SAME Work (different Expressions).
- Reprints, new editions, paperbacks, audiobooks of one creation are the
  SAME Work.
- Adaptations across content types (novel → screenplay, prose → graphic
  novel, text → musical setting) are DIFFERENT Works.
- Abridgements and substantially revised editions are DIFFERENT Works.
- Compilations, anthologies and selections are DIFFERENT Works from their
  constituent parts.
- Two creations sharing only a generic title ("Poems", "Selected Letters",
  "Sonatas") with the same author are NOT automatically the same Work — look
  for corroborating evidence (date, content scope, opus number).
- Different creators almost always means different Works. Do not merge across
  creators unless one record clearly attributes the same creator under a
  variant name and the titles match.

Cite only fields shown in the input. If a field is missing, say so rather
than guessing. When in doubt, return "uncertain"."""

EXAMPLES = """Examples of the reasoning style expected:

Example 1 — translation (same Work):
  A: creator="Tolstoy, Leo", preferred_title="Война и мир", original_language="ru"
  B: creator="Tolstoi, L. N.", preferred_title="Sota ja rauha",
     original_language="ru", expression_language="fi"
  → same_work, confidence 0.95.
    Rationale: same creator under transliteration variants, both have
    original_language='ru', B's expression_language='fi' indicates a Finnish
    Expression of the Russian original.

Example 2 — adaptation (different Work):
  A: creator="Tolkien, J. R. R.", preferred_title="The Lord of the Rings",
     content_type="text"
  B: creator="Jackson, Peter", preferred_title="The Lord of the Rings",
     content_type="two-dimensional moving image"
  → different_work, confidence 0.97.
    Rationale: different creators and content_type shifts from text to moving
    image, indicating an adaptation rather than the same Work."""

USER = """Decide whether these two records describe the same RDA Work.

Record A:
{record_a}

Record B:
{record_b}

Embedding similarity (for context only, not authoritative): {sim:.3f}"""

prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM + "\n\n" + EXAMPLES),
    ("user", USER),
])


# --- Chain -----------------------------------------------------------------
# Local OpenAI-compatible server. Ollama by default; mlx_lm for production.

llm_primary = ChatOpenAI(
    base_url=os.environ["LLM_BASE_URL"],          # http://localhost:11434/v1
    api_key=os.environ.get("LLM_API_KEY", "ollama"),
    model=os.environ["LLM_MODEL_PRIMARY"],         # qwen3:32b-q4_K_M
    temperature=0,
    seed=42,
)

llm_fallback = ChatOpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ.get("LLM_API_KEY", "ollama"),
    model=os.environ["LLM_MODEL_FALLBACK"],        # qwen2.5:72b-instruct-q4_K_M
    temperature=0,
    seed=42,
)

judge_primary = prompt | llm_primary.with_structured_output(
    WorkMatchDecision, method="json_schema"
)
judge_fallback = prompt | llm_fallback.with_structured_output(
    WorkMatchDecision, method="json_schema"
)


def judge_pair(a: WorkRecord, b: WorkRecord, sim: float) -> WorkMatchDecision:
    """Single call against the primary model. Caller handles retries."""
    return judge_primary.invoke({
        "record_a": a.model_dump_json(indent=2, exclude_none=True),
        "record_b": b.model_dump_json(indent=2, exclude_none=True),
        "sim": sim,
    })
```

### Cascade

`cascade_judge(a, b, sim)` runs `judge_pair` on the primary model first; if the result is `uncertain` or `same_work` with `confidence < 0.85`, it re-runs on the fallback model. Both decisions are logged to provenance with distinct `bffi-prov:stage` values (`"llm-judge-primary"`, `"llm-judge-second-opinion"`). The 72B fallback is expected to handle ~10–20 % of gray-zone pairs — the ones where the 32B was wobbly.

### Retry, cache, checkpoint

Three layered failure-recovery mechanisms (see `docs/archived/BUILD_PLAN.md` M6):

- **SQLite cache** keyed on `(model, prompt_hash, record_a_canonical, record_b_canonical)`. Cache writes happen only after Boundary-4 validation passes — never cement a malformed or stub-phrase response.
- **Checkpoint file** at `<output_path>.checkpoint`, written every 100 pairs, recording last-completed index + cache-hit counts. On restart the run resumes with an honest ETA rather than re-iterating silently.
- **Exponential-backoff retry** (5 s → 30 s → 120 s) inside `judge_pair` for connection errors and timeouts. After 3 retries exhaust, log `decision="uncertain"` with the error in the rationale and continue. Never crash a multi-day run on a single bad pair.

---

## 8. Provenance logging

Every merge decision becomes a discrete `prov:Activity` in a separate named graph, with PROV-O for the standard skeleton plus a small custom extension for LLM-specific fields.

### The shape

```turtle
# Stored in graph <http://urn.fi/URN:NBN:fi:bib:graph:provenance>
@prefix prov:      <http://www.w3.org/ns/prov#> .
@prefix bffi:      <http://urn.fi/URN:NBN:fi:schema:bffi:> .
@prefix bffi-prov: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#> .
@prefix bf:        <http://id.loc.gov/ontologies/bibframe/> .
@prefix skos:      <http://www.w3.org/2004/02/skos/core#> .
@prefix dct:       <http://purl.org/dc/terms/> .
@prefix xsd:       <http://www.w3.org/2001/XMLSchema#> .
@prefix rdfs:      <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf:       <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix bib:       <http://urn.fi/URN:NBN:fi:bib:> .

# --- The decision itself ---
bib:merge/01HXYZ... a prov:Activity, bffi-prov:WorkMergeDecision ;
    prov:startedAtTime    "2026-05-08T10:23:14Z"^^xsd:dateTime ;
    prov:endedAtTime      "2026-05-08T10:23:18Z"^^xsd:dateTime ;
    prov:wasAssociatedWith bib:agent/qwen3-32b-q4_K_M ;
    prov:used             bib:rawwork/12345678 ,
                          bib:rawwork/12345679 ;
    bffi-prov:stage       "llm-judge-primary" ;
    bffi-prov:decision    "same_work" ;
    bffi-prov:confidence  "0.91"^^xsd:decimal ;
    bffi-prov:embeddingSimilarity "0.84"^^xsd:decimal ;
    bffi-prov:rationale   "Same creator under transliteration variants, both records have original_language='ru'; B's expression_language='fi' indicates a Finnish Expression." ;
    bffi-prov:matchingField  "creator", "original_language", "date_of_origin" ;
    bffi-prov:divergingField "expression_language" ;
    bffi-prov:promptHash  "sha256:9a1f7c3e..." ;
    bffi-prov:rawResponse "<<full JSON the model returned>>" .

# --- The canonical Work points back at its provenance ---
# Note the bf:identifiedBy triples carrying the source Helmet bib IDs;
# union of source identifiers happens at the M8 merge step, so a canonical
# Work that absorbed N raw Works carries N bf:identifiedBy triples.
bib:work/c0ffee...  a bffi:Work, skos:Concept ;
    skos:prefLabel        "Sota ja rauha"@fi ;
    bf:identifiedBy [ a bf:Local ;
                      rdf:value "12345678" ;
                      bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ,
                    [ a bf:Local ;
                      rdf:value "12345679" ;
                      bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] ;
    prov:wasGeneratedBy   bib:merge/01HXYZ... ;
    prov:wasDerivedFrom   bib:rawwork/12345678 ,
                          bib:rawwork/12345679 .

# --- Agents ---
# URIs are minted at runtime as `bib:agent/<model_id with ":" → "-">`
# (see `bffi_pipeline.provenance.logger.model_agent_uri`); the
# quantisation suffix is preserved so different quantisations remain
# distinguishable. Cascade tiers are declared so primary, fallback, and
# the M3 contributor-extraction default attribute to distinct
# prov:SoftwareAgent records.
bib:agent/qwen3-32b-q4_K_M a prov:SoftwareAgent ;
    rdfs:label            "Qwen3 32B (4-bit, instruct weights — M6/M9 cascade primary; M3 title cascade)" ;
    bffi-prov:provider    "Alibaba (Qwen team) — open weights, served locally via Ollama" ;
    bffi-prov:modelId     "qwen3:32b-q4_K_M" ;
    bffi-prov:temperature "0.0"^^xsd:decimal ;
    bffi-prov:seed        "42"^^xsd:integer .

bib:agent/qwen3-8b-q4_K_M a prov:SoftwareAgent ;
    rdfs:label            "Qwen3 8B (4-bit — M3 contributor-extraction cascade default)" ;
    bffi-prov:provider    "Alibaba (Qwen team) — open weights, served locally via Ollama" ;
    bffi-prov:modelId     "qwen3:8b-q4_K_M" ;
    bffi-prov:temperature "0.0"^^xsd:decimal ;
    bffi-prov:seed        "42"^^xsd:integer .

bib:agent/qwen2.5-72b-instruct-q4_K_M a prov:SoftwareAgent ;
    rdfs:label            "Qwen2.5 72B Instruct (4-bit, cascade fallback — Qwen3 has no 72B size)" ;
    bffi-prov:provider    "Alibaba (Qwen team) — open weights, served locally via Ollama" ;
    bffi-prov:modelId     "qwen2.5:72b-instruct-q4_K_M" ;
    bffi-prov:temperature "0.0"^^xsd:decimal ;
    bffi-prov:seed        "42"^^xsd:integer .

# --- Human review chains onto the original Activity ---
bib:review/01HX2A... a prov:Activity, bffi-prov:HumanReview ;
    prov:wasInformedBy    bib:merge/01HXYZ... ;
    prov:wasAssociatedWith bib:agent/cataloguer/jdoe ;
    prov:atTime           "2026-05-09T14:00:00Z"^^xsd:dateTime ;
    bffi-prov:decision    "confirmed" ;
    bffi-prov:reviewNote  "Verified against KANTO authority record (KANTO00012345)." .
```

### Activity classes, properties, and stages

The `bffi-prov` namespace (`http://urn.fi/URN:NBN:fi:schema:bffi-prov#`) is the project's small custom extension to PROV-O. Three Activity subclasses cover the lifecycle:

| Class | Where it's emitted | What it records |
|---|---|---|
| `bffi-prov:MarcConversion` | M2, on every successful MARCXML → BIBFRAME conversion | `prov:used` = source filename (`file://...`), `bffi-prov:helmetBibId` = bare numeric ID, `bffi-prov:converterVersion` = `marc2bibframe2` commit hash. Minted Work and Instance get `prov:wasGeneratedBy` pointing at this Activity. |
| `bffi-prov:WorkMergeDecision` | M6, on every cascade judge call (primary and second-opinion) | The shape shown above: stage, decision, confidence, embedding similarity, rationale, matching/diverging fields, prompt hash, raw response. |
| `bffi-prov:HumanReview` | Whenever a cataloguer confirms or overrides an earlier decision | Chained onto the original Activity via `prov:wasInformedBy`; carries `bffi-prov:decision` ∈ {`"confirmed"`, `"overridden"`} plus `bffi-prov:reviewNote`. |

**Reconciliation status on canonical Works.** When M9 takes the deterministic-fallback path (LLM `uncertain` or LLM confidence < 0.80), the canonical Work's AdminMetadata block records the state in BFFI-native form:

- `bffi:descriptionAuthentication = <bib:auth/needs-review>` (set by M9 fallback) → `<bib:auth/verified>` (set by a human review confirming the reconciliation), or absent / `<bib:auth/auto-merged>` when there is no reconciliation issue. Cataloguers query for `?w bffi:adminMetadata/bffi:descriptionAuthentication <bib:auth/needs-review>` to drive the review queue. See "AdminMetadata view" above. (An earlier draft used a separate `bffi-prov:reconciliationStatus` predicate; that has been folded into AdminMetadata's `descriptionAuthentication` so the state lives in exactly one place.)

**Compaction and the meta-graph.** `bffi-pipeline provenance compact --older-than 90d` removes `bffi-prov:rawResponse` literals from Activities older than the threshold. The date of the last compaction run is recorded in a small auxiliary graph `<http://urn.fi/URN:NBN:fi:bib:graph:provenance-meta>` as a single triple:

```turtle
<http://urn.fi/URN:NBN:fi:bib:graph:provenance>
    bffi-prov:lastCompactedAt "2026-05-08T20:14:00Z"^^xsd:dateTime .
```

Every `bffi-pipeline ...` invocation reads this triple at startup; if the value is older than 90 days (or absent), the CLI prints a stale-provenance warning to stderr. See `docs/archived/BUILD_PLAN.md` M7.

**`bffi-prov:stage` vocabulary.** Each `WorkMergeDecision` Activity carries exactly one `bffi-prov:stage` literal. The committed enum:

- `"llm-judge-primary"` — Qwen3 32B first-pass.
- `"llm-judge-second-opinion"` — Qwen3 72B cascade re-run for `uncertain` or low-confidence `same_work` (cascade trigger: confidence < 0.85, see §6 / §11).
- `"reconciliation-local"` — exact `skos:prefLabel` match against the locally-loaded Finto authority graph (M11 option 3b — YSO / KAUNO / SLM / MUSO); no `api.finto.fi` round-trip.
- `"reconciliation-lexical"` — single high-similarity authority candidate accepted deterministically.
- `"reconciliation-llm"` — LLM picked from a candidate list.
- `"reconciliation-fallback"` — LLM `uncertain` or confidence < 0.80; took highest-lexical and set the canonical Work's AdminMetadata `bffi:descriptionAuthentication = <bib:auth/needs-review>`.
- `"reconciliation-no-candidate"` — no candidate cleared the lexical floor; left unreconciled.
- `"human-only"` — category routed straight to human review without an LLM decision (see §11).
- `"watchdog-aborted"` — the M6 cascade's LLM call exceeded `LLM_CALL_TIMEOUT_SECONDS` and exhausted the 5/30/120 s retry budget on both primary and fallback. The Activity's `bffi-prov:confidence` is 0.0, `bffi-prov:decision` is `uncertain`, and the rationale carries the per-call latency for forensics. Plan: `docs/plans/in-progress/p-03-m6-stall-watchdog.md`.

`"human-review"` is **not** a `bffi-prov:stage` value; `bffi-prov:HumanReview` Activities use `prov:wasInformedBy` to chain onto an earlier decision.

### AdminMetadata view (BFFI-native administrative summary)

Every canonical `bffi:Work` and every `bffi:Expression` carries one `bffi:adminMetadata` triple pointing at a `bffi:AdminMetadata` block. AdminMetadata is `owl:equivalentClass` of `bf:AdminMetadata` per BFFI's schema (`docs/lkd.rdf`); standard BIBFRAME semantics apply. The block is a BFFI-native administrative summary: who modified it last, when, with what generation process, in what authentication state, derived from which Helmet records.

This is a **layered view** on top of the PROV-O graph above — not a replacement. The PROV-O Activities keep the decision-level history (rationale, confidence, prompt hash, cascade tier, override chains). AdminMetadata gives downstream BFFI tooling the BIBFRAME-conventional view it expects, and gives cataloguers a one-glance status check. The pipeline writes both views; cross-references keep them synchronised.

#### Predicates used

The 14 predicates the pipeline emits on each `bffi:AdminMetadata` block are listed in the table at § 0; ranges and BIBFRAME equivalents are confirmed against `docs/lkd.rdf`. Stable URIs for the value-class instances (Agents, GenerationProcess, DescriptionAuthentication, etc.) live in `config/bffi-admin-vocabulary.ttl` and are loaded into the `bffi-works` graph at `make publish` time.

#### Cross-references between AdminMetadata and PROV-O

1. **Same agent URIs.** `bffi:descriptionModifier` (AdminMetadata) and `prov:wasAssociatedWith` (PROV-O Activity) point at the **same** `bib:agent/...` URI. The agent record is dual-typed `a prov:SoftwareAgent, bffi:Agent` in the vocabulary file; provider/modelId/temperature/seed properties stay in the PROV-O graph.
2. **Same generation-process URI.** `bffi:generationProcess` references `bib:gen-process/bffi-pipeline/v<version>`; the PROV-O `bffi-prov:converterVersion` (M2) and the model-tagged software agent (M6) refer to the same release.
3. **Same source-record set.** `bffi:sourceMetadata` enumerates the same Helmet record URIs that PROV-O's `prov:wasDerivedFrom` enumerates on the canonical Work. Both lists come from `canonical-map.jsonl`.
4. **Spine link to the latest decision.** The AdminMetadata block carries `prov:wasGeneratedBy <latest WorkMergeDecision-or-HumanReview Activity URI>`. AdminMetadata is `rdfs:Resource`, so plain PROV-O on it is well-formed. This is the one-hop path from "current state" to "decision history".
5. **Authentication state ↔ M9 needs-review.** `bffi:descriptionAuthentication = bib:auth/needs-review` is the BFFI-native expression of the M9 deterministic-fallback case. The previously-introduced `bffi-prov:reconciliationStatus` predicate is dropped under this layered model — see "Operational notes" below.

#### Example

```turtle
@prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix bib:  <http://urn.fi/URN:NBN:fi:bib:> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

bib:work/c0ffee...
    bffi:adminMetadata bib:adminmeta/c0ffee... .

bib:adminmeta/c0ffee...
    a bffi:AdminMetadata ;
    bffi:adminMetadataFor          bib:work/c0ffee... ;
    bffi:descriptionCreationDate   "2026-04-12T08:31:02Z"^^xsd:dateTime ;
    bffi:descriptionChangeDate     "2026-05-09T14:00:00Z"^^xsd:dateTime ;
    bffi:dateGenerated             "2026-05-08T20:14:00Z"^^xsd:dateTime ;
    bffi:descriptionModifier       bib:agent/cataloguer/jdoe ;
    bffi:catalogerId               "jdoe" ;
    bffi:descriptionConventions    bib:desc-conv/bffi-1.0.0 ;
    bffi:descriptionLevel          bib:desc-level/full ;
    bffi:encodingLevel             bib:enc-level/human-reviewed ;
    bffi:descriptionAuthentication bib:auth/verified ;
    bffi:generationProcess         bib:gen-process/bffi-pipeline/v0.1.0 ;
    bffi:metadataLicensor          bib:metadata-licensor/cc0 ;
    bffi:recordingSource           bib:recording-source/helmet ;
    bffi:sourceMetadata            bib:helmet/12345678 , bib:helmet/12345679 ;
    prov:wasGeneratedBy            bib:review/01HX2A... .   # spine link to PROV-O
```

The `prov:wasGeneratedBy` references the latest `bffi-prov:HumanReview` Activity, which itself `prov:wasInformedBy` the original `bffi-prov:WorkMergeDecision` — the full cascade history is one or two hops away from the AdminMetadata view.

#### Joining AdminMetadata to PROV-O for the review queue

```sparql
PREFIX bffi:      <http://urn.fi/URN:NBN:fi:schema:bffi:>
PREFIX bffi-prov: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#>
PREFIX prov:      <http://www.w3.org/ns/prov#>

SELECT ?work ?confidence ?rationale WHERE {
  # AdminMetadata side: which Works are awaiting human review.
  ?work bffi:adminMetadata ?am .
  ?am   bffi:descriptionAuthentication <http://urn.fi/URN:NBN:fi:bib:auth/needs-review> ;
        prov:wasGeneratedBy ?activity .

  # PROV-O side: pull the rationale and confidence from the spine-linked Activity.
  GRAPH <http://urn.fi/URN:NBN:fi:bib:graph:provenance> {
    ?activity bffi-prov:rationale  ?rationale ;
              bffi-prov:confidence ?confidence .
  }
}
ORDER BY ?confidence
```

This query is the canonical replacement for the standalone "low-confidence merges awaiting review" query at the top of "Useful queries" — it surfaces the same Works but reaches into the PROV-O graph only when the cataloguer actually needs the rationale.

### Helper

```python
import hashlib
from datetime import datetime, timezone
from ulid import ULID
from rdflib import Graph, Namespace, Literal, URIRef, RDF, RDFS, XSD

PROV      = Namespace("http://www.w3.org/ns/prov#")
BFFI_PROV = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi-prov#")
BIB       = Namespace("http://urn.fi/URN:NBN:fi:bib:")

def log_merge_decision(
    g: Graph,
    *,
    inputs: list[str],
    canonical: str | None,
    decision: str,
    confidence: float,
    embedding_sim: float,
    rationale: str,
    matching_fields: list[str],
    diverging_fields: list[str],
    prompt_template: str,
    raw_response: str,
    model_id: str,
    stage: str = "llm-judge-primary",
    started_at: datetime | None = None,
    ended_at:   datetime | None = None,
) -> URIRef:
    activity = BIB[f"merge/{ULID()}"]
    started_at = started_at or datetime.now(timezone.utc)
    ended_at   = ended_at   or datetime.now(timezone.utc)
    prompt_hash = "sha256:" + hashlib.sha256(prompt_template.encode()).hexdigest()

    g.add((activity, RDF.type, PROV.Activity))
    g.add((activity, RDF.type, BFFI_PROV.WorkMergeDecision))
    g.add((activity, PROV.startedAtTime, Literal(started_at.isoformat(), datatype=XSD.dateTime)))
    g.add((activity, PROV.endedAtTime,   Literal(ended_at.isoformat(),   datatype=XSD.dateTime)))
    # Sanitize model_id for URI use: Ollama tags use ':' as separator
    # ("qwen3:32b-q4_K_M") but ':' is reserved in URI path segments.
    # Replace with '-' so the agent URI is well-formed.
    g.add((activity, PROV.wasAssociatedWith, BIB[f"agent/{model_id.replace(':', '-')}"]))
    for src in inputs:
        g.add((activity, PROV.used, URIRef(src)))

    g.add((activity, BFFI_PROV.stage,                Literal(stage)))
    g.add((activity, BFFI_PROV.decision,             Literal(decision)))
    g.add((activity, BFFI_PROV.confidence,           Literal(confidence,    datatype=XSD.decimal)))
    g.add((activity, BFFI_PROV.embeddingSimilarity,  Literal(embedding_sim, datatype=XSD.decimal)))
    g.add((activity, BFFI_PROV.rationale,            Literal(rationale)))
    for f in matching_fields:
        g.add((activity, BFFI_PROV.matchingField, Literal(f)))
    for f in diverging_fields:
        g.add((activity, BFFI_PROV.divergingField, Literal(f)))
    g.add((activity, BFFI_PROV.promptHash,  Literal(prompt_hash)))
    g.add((activity, BFFI_PROV.rawResponse, Literal(raw_response)))

    if decision == "same_work" and canonical:
        c = URIRef(canonical)
        g.add((c, PROV.wasGeneratedBy, activity))
        for src in inputs:
            g.add((c, PROV.wasDerivedFrom, URIRef(src)))

    return activity
```

ULIDs (or UUIDv7) beat UUIDv4 here because they're time-ordered — listing recent merges is just `ORDER BY DESC` on the URI.

### Useful queries

**Low-confidence merges awaiting review:**

```sparql
SELECT ?activity ?confidence ?rationale WHERE {
  GRAPH <http://urn.fi/URN:NBN:fi:bib:graph:provenance> {
    ?activity a bffi-prov:WorkMergeDecision ;
              bffi-prov:decision   "same_work" ;
              bffi-prov:confidence ?confidence ;
              bffi-prov:rationale  ?rationale .
    FILTER(?confidence < 0.85)
    FILTER NOT EXISTS {
      ?review prov:wasInformedBy ?activity ;
              a bffi-prov:HumanReview .
    }
  }
} ORDER BY ?confidence
```

**Which Helmet records ended up under a given canonical Work:**

```sparql
# The committed shape is bf:identifiedBy with the Helmet source on the
# canonical Work itself — each absorbed source record contributes one
# identifier after M8. This is the query cataloguers will actually run
# (it's also the body of `bffi-pipeline lookup-helmet`, see BUILD_PLAN M10).
SELECT ?helmetBibId WHERE {
  <http://urn.fi/URN:NBN:fi:bib:work:c0ffee...>
      bf:identifiedBy [ a bf:Local ;
                        rdf:value ?helmetBibId ;
                        bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] .
}
```

**LLM merges humans later overrode (gold-set training data):**

```sparql
SELECT ?activity ?confidence ?rationale ?reviewNote WHERE {
  GRAPH <http://urn.fi/URN:NBN:fi:bib:graph:provenance> {
    ?activity a bffi-prov:WorkMergeDecision ;
              bffi-prov:confidence ?confidence ;
              bffi-prov:rationale  ?rationale .
    ?review prov:wasInformedBy ?activity ;
            bffi-prov:decision   "overridden" ;
            bffi-prov:reviewNote ?reviewNote .
  }
}
```

### Operational notes

- Keep provenance in a **separate named graph** `<http://urn.fi/URN:NBN:fi:bib:graph:provenance>`; do *not* include it in Skosmos's `void:sparqlGraph`. Otherwise Skosmos will render `prov:Activity` URIs as concepts.
- **Retention policy:** keep the structured Activity record indefinitely (it's small); compact `bffi-prov:rawResponse` literals after ~90 days via `bffi-pipeline provenance compact --older-than 90d`. Structured fields (decision, confidence, rationale, prompt hash) survive compaction; only the raw model output is dropped. CLI prints a stale-provenance warning on startup if the last compaction is older than 90 days. See `docs/archived/BUILD_PLAN.md` M7.
- **Version the prompt template in git** and store the file path alongside the hash (`bffi-prov:promptSource "git://repo/prompts/judge_v1.txt"`).
- **Log negative decisions too**, not just merges — when a cataloguer asks "why didn't these merge?", you need the answer.
- **`bffi-prov:stage` vocabulary.** Each decision is tagged with the stage that produced it, so review queues, eval, and gold-set growth can filter precisely:
  - `"llm-judge-primary"` / `"llm-judge-second-opinion"` — the two-stage Qwen3 cascade (§7, §11).
  - `"reconciliation-local"` / `"reconciliation-lexical"` / `"reconciliation-llm"` / `"reconciliation-fallback"` / `"reconciliation-no-candidate"` — the five-tier reconciliation logic in M9 (tier-0 local prefLabel match against the M11 option 3b authority graphs precedes the four Finto-API tiers).
  - `"human-only"` — categories where the model's gold-set accuracy is unacceptable, routed straight to human review without an LLM decision (see §11).
  - `"human-review"` is reserved for `bffi-prov:HumanReview` Activities chained off an earlier decision via `prov:wasInformedBy`.

---

## 9. Gold-set evaluation harness

### Gold-set format

JSONL in the repo, one pair per line. Diffs cleanly in pull requests.

```jsonl
{"id": "gs-0001", "category": "translation", "expected": "same_work", "record_a": {...}, "record_b": {...}, "embedding_sim": 0.84, "added": "2026-04-12", "added_by": "jdoe", "notes": "Standard Russian-to-Finnish translation case."}
{"id": "gs-0002", "category": "adaptation", "expected": "different_work", "record_a": {...}, "record_b": {...}, "embedding_sim": 0.79}
{"id": "gs-0003", "category": "common-title-collision", "expected": "different_work", ...}
```

**Selection principles:**

- **Stratified by category, not frequency.** Translation, adaptation, abridgement, common-title collision, transliteration variant, compilation/constituent, edition revision, music recording vs notated work, same author with similar titles — each ≥ 20–30 cases regardless of corpus prevalence.
- **Drawn from real records**, not synthesized. Synthetic cases stop being predictive.
- **Hold-out portion that never appears in few-shot prompts.** Committed split: 30 %, hand-marked per case with a `"holdout": true` field (not hash-derived), revisited at ~500 total cases. Stratification matters across the hold-out as much as the training set — every category needs at least 2–3 hold-out cases.
- Every case carries `category` and ideally `notes`.
- **Eval is not in CI.** It runs manually on the M5 Max via `make eval` before any PR that touches `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/`. Output is pasted into the PR description per the project's PR template (see §13 / `docs/ci-strategy.md`).

### Scoring harness

```python
import json
import time
import hashlib
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, asdict

from work_judge import judge_pair, WorkRecord, prompt as judge_prompt


@dataclass
class CaseResult:
    id: str
    category: str
    expected: str
    predicted: str
    confidence: float
    correct: bool
    rationale: str
    latency_ms: int


def load_gold_set(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(gold_path: Path, run_label: str) -> dict:
    cases = load_gold_set(gold_path)
    results: list[CaseResult] = []

    for case in cases:
        a = WorkRecord(**case["record_a"])
        b = WorkRecord(**case["record_b"])
        t0 = time.perf_counter()
        decision = judge_pair(a, b, case["embedding_sim"])
        latency = int((time.perf_counter() - t0) * 1000)

        results.append(CaseResult(
            id=case["id"],
            category=case["category"],
            expected=case["expected"],
            predicted=decision.decision,
            confidence=decision.confidence,
            correct=(decision.decision == case["expected"]),
            rationale=decision.rationale,
            latency_ms=latency,
        ))

    return summarize(results, run_label, gold_path)


def summarize(results: list[CaseResult], run_label: str, gold_path: Path) -> dict:
    n = len(results)
    correct = sum(r.correct for r in results)
    uncertain = sum(r.predicted == "uncertain" for r in results)

    accuracy = correct / n if n else 0.0
    decided_n = n - uncertain
    decided_acc = (
        sum(r.correct for r in results if r.predicted != "uncertain") / decided_n
        if decided_n else 0.0
    )

    by_cat = defaultdict(list)
    for r in results:
        by_cat[r.category].append(r)
    per_category = {
        cat: {
            "n": len(rs),
            "accuracy": sum(r.correct for r in rs) / len(rs),
            "uncertain_rate": sum(r.predicted == "uncertain" for r in rs) / len(rs),
        }
        for cat, rs in sorted(by_cat.items())
    }

    labels = ["same_work", "different_work", "uncertain"]
    confusion = {e: {p: 0 for p in labels} for e in ["same_work", "different_work"]}
    for r in results:
        if r.expected in confusion:
            confusion[r.expected][r.predicted] += 1

    high_conf_results = [r for r in results if r.confidence >= 0.9 and r.predicted != "uncertain"]
    high_conf_acc = (
        sum(r.correct for r in high_conf_results) / len(high_conf_results)
        if high_conf_results else None
    )

    prompt_text = str(judge_prompt)
    prompt_hash = "sha256:" + hashlib.sha256(prompt_text.encode()).hexdigest()[:16]

    return {
        "run_label": run_label,
        "gold_set_path": str(gold_path),
        "gold_set_size": n,
        "prompt_hash": prompt_hash,
        "accuracy": round(accuracy, 4),
        "decided_accuracy": round(decided_acc, 4),
        "uncertain_rate": round(uncertain / n, 4) if n else 0.0,
        "high_confidence_accuracy": (
            round(high_conf_acc, 4) if high_conf_acc is not None else None
        ),
        "median_latency_ms": sorted(r.latency_ms for r in results)[n // 2] if n else 0,
        "per_category": per_category,
        "confusion_matrix": confusion,
        "failures": [asdict(r) for r in results if not r.correct],
    }


if __name__ == "__main__":
    import sys
    summary = evaluate(Path(sys.argv[1]), run_label=sys.argv[2])
    out = Path(f"eval-runs/{sys.argv[2]}.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"Accuracy:           {summary['accuracy']:.1%}  ({summary['gold_set_size']} cases)")
    print(f"Decided accuracy:   {summary['decided_accuracy']:.1%}  (excluding uncertain)")
    print(f"High-conf accuracy: {summary['high_confidence_accuracy']}")
    print(f"Uncertain rate:     {summary['uncertain_rate']:.1%}")
    print(f"Median latency:     {summary['median_latency_ms']} ms")
    print()
    print("Per category:")
    for cat, stats in summary["per_category"].items():
        print(f"  {cat:30s} {stats['accuracy']:.1%}  (n={stats['n']})")
```

### What the metrics mean

| Metric | What it tells you |
|--------|-------------------|
| **Per-category accuracy** | The only number that genuinely tells you what's happening. Aggregate accuracy hides the regressions that matter. |
| **Decided accuracy** (excluding uncertain) | Precision when the model commits. `uncertain` is fine — it routes to human review. |
| **High-confidence accuracy** | Calibration. If `confidence ≥ 0.9` is right 99% of the time, auto-commit that band. If only 85%, the signal is meaningless. |
| **Confusion matrix** | Whether errors are symmetric. False merges are much more expensive than false splits because un-merging is painful. |

### Wiring it into the loop

The committed strategy runs the eval **manually on the M5 Max**, not in CI — Linux runners can't host a 32B local model and self-hosted runners introduce operational/security overhead unjustified for a solo project (see §13 / `docs/ci-strategy.md`). The discipline is enforced socially via the PR template, not by an automated gate.

- **Run before every prompt or judge change.** Any PR touching `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/` requires `make eval` output pasted into the PR description. The PR template has a checkbox.
- **Compare against the previous main-branch run.** Per-category regression > 10 points is a blocker; aggregate change is informational. Aggregate accuracy hides the regressions that matter.
- **Feed overrides back in.** The "humans overrode the LLM" SPARQL query is your gold-set growth pipeline. Run monthly, present candidates, add confirmed ones with `category` filled in. Aim for 20–50 production-derived cases per month. New cases default to `"holdout": false`; flip the flag explicitly for cases that should join the eval set.
- **Track runs over time.** Plot per-category accuracy across runs. "We lost 8 points on `transliteration-variant` three releases ago and never recovered" is invisible in any single run. Store the JSON artifacts in `eval-runs/` (gitignored) or a dedicated bucket so the history survives a repo re-clone.

### A subtlety worth internalizing

The gold set should be representative of your **failure modes**, not your **corpus**. If 95% of real pairs are obvious same-language reprints, the gold set should still be heavily weighted toward the hard 5%. Otherwise high gold-set accuracy will be uninformative because it'll be dominated by easy cases the model gets right by reflex.

---

## 10. Validation strategy

Data crosses five distinct boundaries in the pipeline; each gets its own validation. Don't conflate them — rdflib's permissive parsing is **not** validation, and silent acceptance at one boundary becomes corruption at the next.

| # | Boundary | When | Tool | Failure mode |
|---|---|---|---|---|
| 1 | MARCXML input | M2 entry | XSD + minimum-content checks | Skip record, log to `_errors.jsonl` |
| 2 | BIBFRAME post-conversion | M2 exit | SHACL (`bibframe-conversion.shape.ttl`) | Skip record, log to `_errors.jsonl` |
| 3 | BFFI post-CONSTRUCT | M3 exit | SHACL (`bffi.shape.ttl`) | Report to `_validation.jsonl`, warn on CLI, don't block |
| 4 | Judge output | M6 | Pydantic structural + semantic validators | Retry; on persistent failure log `decision="uncertain"` |
| 5 | Post-load smoke | M10 exit | SPARQL `ASK` queries | Roll back the load |

**Boundary 1 — MARCXML input.** XSD validation against the LoC MARC21 slim schema using cached `lxml.etree.XMLSchema`, plus a minimum-content check (≥ 1 of 1XX/7XX, 245, 008, 336/337/338). Failures are typed (`marcxml-xml-syntax`, `marcxml-xsd-validation`, `marcxml-content-minimum`).

**Boundary 2 — BIBFRAME post-conversion.** A small SHACL shape verifying what the pipeline assumes from `marc2bibframe2`: every record produces ≥ 1 `bf:Work`; every Work has a `bf:title` with `bf:mainTitle`; every Work has ≥ 1 `bf:contribution`. Intentionally minimal — this validates "BIBFRAME my pipeline can handle," not "correct BIBFRAME."

**Boundary 3 — BFFI post-CONSTRUCT.** This is where validation pays for itself. Required shapes: every `bffi:Work` has ≥ 1 `bffi:hasExpression`; every `bffi:Expression` has exactly one `bffi:expressionOf` pointing at a `bffi:Work`; every Work and Expression has `bf:identifiedBy` with `bf:source = <http://urn.fi/URN:NBN:fi:bib:source:helmet>`; every Work has `skos:prefLabel` in at least one of `fi`/`sv`/`en`; class disjointness; properties allocated to Work-only don't appear on Expression and vice versa. `pyshacl` runs on every conversion batch; failures surface in `_validation.jsonl` with a CLI warning but **don't block** at production scale (0.1 % of 800k records is 800 records that need triage, not a halt). CI fails on regressions against a baseline.

**Boundary 4 — Judge output.** Pydantic structural constraint via `with_structured_output(WorkMatchDecision, method="json_schema")` plus the `@model_validator` checks shown in §7: `decision="uncertain"` requires `confidence ≤ 0.7`; `decision="same_work"` requires non-empty `matching_fields`; rationale ≥ 20 chars and free of stub phrases. Validation failures share the retry path with JSON parse failures (max 2 retries → log `uncertain` with the validation error). **Validation-failed responses are never cached.**

**Boundary 5 — Post-load smoke.** SPARQL `ASK` queries that all must return `true` after loading:

```sparql
ASK { ?w a bffi:Work, skos:Concept ; skos:prefLabel ?l }
ASK { ?e a bffi:Expression, skos:Concept ; bffi:expressionOf ?w }
ASK { ?w a bffi:Work ;
         bf:identifiedBy [ bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] }
ASK { ?w skos:narrower ?e . ?e skos:broader ?w }   # Skosify-inferred inverses
```

Failure rolls back the load (drop the loaded named graph; don't leave Fuseki half-loaded).

Full shape definitions, the `_errors.jsonl` schema, and unit-test expectations live in `docs/validation-strategy.md`. The `bffi.shape.ttl` file is the most-edited artifact in the repo — every BFFI version bump flows through it; treat it as living documentation.

---

## 11. Local inference and hardware target

The pipeline runs end-to-end on a MacBook Pro M5 Max with 128 GB unified memory. **No paid LLM APIs** — all LLM-dependent code is built around a local OpenAI-compatible server.

### Memory budget

| Component | Approx. resident size |
|---|---|
| FAISS HNSW index (800k × 1024 dim) | ~5 GB |
| BGE-M3 embedding model (loaded only during M5) | ~2.5 GB |
| Qwen3 32B 4-bit (primary judge) | ~18–20 GB |
| Qwen3 72B 4-bit (cascade fallback) | ~40 GB |
| Fuseki + Skosmos containers | ~4–6 GB |
| OS + working memory | ~10–15 GB |
| **Typical concurrent peak** | **~40–50 GB** |

Comfortable on 128 GB. Loading both judge models simultaneously (~60 GB of models alone) is possible if the cascade runs in one process; most operations only need one at a time.

### Server choice

- **Default: Ollama.** Simplest setup, OpenAI-compatible API on `:11434`, MLX backend in preview. Use for development and gold-set runs.
- **Production batch: mlx_lm.** Continuous batching gives 4–8× throughput on bulk judge runs. Use for the production pass over 50k+ pairs.

The application code talks to either through `langchain-openai` pointed at `LLM_BASE_URL`. Don't write server-specific code.

### Models

- **Primary: Qwen3 32B Instruct (MLX 4-bit).** ~3–6 s per judge call. Strong multilingual quality; uneven on hard cases (common-title collisions, abridgments).
- **Cascade fallback: Qwen3 72B Instruct (MLX 4-bit).** ~6–12 s per call. Better on ambiguous cases.
- **Escape hatch: Llama 3.3 70B (MLX 4-bit).** Comparable size to Qwen3 72B; weaker on Finnish/Russian. Use only if Qwen3 has problems on the gold set.

Benchmark all model choices against the gold set before committing.

### Throughput

A single-pass judge run on 50k–100k gray-zone pairs:

| Model | Server mode | Time |
|---|---|---|
| Qwen3 32B | Ollama (serial) | 70–170 hours |
| Qwen3 32B | mlx_lm (batched) | 10–25 hours |
| Qwen3 72B | Ollama (serial) | 140–340 hours |
| Qwen3 72B | mlx_lm (batched) | 20–50 hours |

Two consequences for the design. **Tighten the gray zone aggressively** — auto-merge ≥ 0.90 (not 0.92) and rejection ≤ 0.78 (not 0.75) once thresholds are validated against the gold set; this roughly halves the LLM workload (§6 Stage 2). **Plan for mlx_lm in production** — Ollama is fine for development, but the full corpus run uses mlx_lm.

### Cascade strategy

For production:

1. Run all gray-zone pairs through Qwen3 32B.
2. Re-run pairs where 32B returned `uncertain` OR `same_work` with confidence < 0.85 through Qwen3 72B.

The 72B handles ~10–20 % of the workload and catches cases where the 32B was wobbly. Both decisions log to provenance with distinct `bffi-prov:stage` values (`"llm-judge-primary"`, `"llm-judge-second-opinion"`).

### Quality risks — be honest

Open-source judge models will not match Claude-class quality on hard bibliographic cases. Plan for: a larger human-review queue, more gold-set growth in categories where the model struggles, longer prompt iteration. If after careful tuning the gold-set per-category accuracy isn't acceptable in some category (e.g., common-title collisions consistently below 75 %), the answer is **not** to over-trust the model. It's to send those category candidates straight to human review without an LLM decision and tag the provenance with `bffi-prov:stage = "human-only"`.

Full hardware/model rationale and benchmark protocol: `docs/local-inference.md`.

---

## 12. External dependencies — cataloguer asks

The pipeline can be built autonomously through M0–M4 on synthetic MARCXML, but **M5 onwards requires real Helmet records** because dedup quality, embedding benchmark, and gold-set development all depend on realistic field content. Four asks gate progression:

- **Ask 1 — Curated development sample (~15 records, before M5).** Hand-picked records exercising Work/Expression cases (translation, transliteration, common-title collision, adaptation, abridgement), material-type coverage (music recording, notated music, map, serial), and edge cases (corporate body, co-creators, aggregate work, deliberately problematic record). Each carries a Helmet bib ID, a one-sentence note, and an expected outcome that seeds the gold set.
- **Ask 2 — Reconciliation seed batch (~15 records, before M9).** 5–10 records with creators in KANTO under authorised forms (happy path); 3–5 with non-Finnish creators not in KANTO (VIAF fallback); 3–5 with MARC heading/KANTO authorised-form variance (LLM-pick path); a few with YSO 650 fields.
- **Ask 3 — Corpus characterisation (before the production M5 run).** Total record count (assumed 800k; confirm); distribution of material types and languages; translation-vs-original ratio; single dump or incremental updates; records flagged for exclusion; embargoes and policy restrictions.
- **Ask 4 — Policy confirmation (before the production M10 publish).** Helmet consortium confirms: bibliographic metadata is OK to republish as linked open data; the URN namespace is acceptable / coordinated with NLF; no records or categories must be excluded; license for published RDF is settled (CC0 to match Finto).

**Surfacing the gates.** Before starting M5, remind that Asks 1 and 3 are needed; before M9, remind about Ask 2; before the production publish, remind about Ask 4. If asks aren't fulfilled, proceed with synthetic data where possible and explicitly note in the PR description and runbook which milestone is "blocked on external input."

Full ask list with English phrasing for cataloguers and Finnish-language equivalent: `docs/external-dependencies.md` and `docs/cataloguer-asks-fi.md`.

---

## 13. Tech stack, milestones, and CI

### Tech stack (committed)

Python 3.11+, `uv` package manager, `rdflib` 7.x, `pymarc` for inspection plus the LoC `marc2bibframe2` XSLT (vendored as a git submodule under `third_party/`) for conversion, `lxml` for XSLT and XSD, `langchain-openai` + Pydantic v2 against a local OpenAI-compatible server (Ollama default; mlx_lm for production), MLX inference framework, **Qwen3 32B** primary judge with **Qwen3 72B** cascade, `sentence-transformers` BGE-M3 embeddings, FAISS `IndexHNSWFlat`, `python-ulid`, `typer` CLI, `pytest` + `pytest-asyncio`, Apache Jena Fuseki 5.x with `jena-text` (Docker, version pinned), Skosmos 3.x (Docker, version pinned), `ruff` lint+format, `mypy --strict`. Full table with rationale: `docs/archived/BUILD_PLAN.md` § "Tech stack".

### Milestones

The build plan is structured as M0 → M13 with explicit definitions of done. One-liners (full detail in `docs/archived/BUILD_PLAN.md`):

| ID | Milestone | One-liner |
|---|---|---|
| M0 | Skeleton | Repo layout, lint/test skeleton green, `docker-compose.yml` runs Fuseki + Skosmos. |
| M1 | URI minting + config | `src/bffi_pipeline/uris.py` deterministic minting; Pydantic Settings. |
| M2 | MARCXML → BIBFRAME | XSLT wrapper; UTF-8-only; one-record-per-file `<id>.xml`; emits Helmet `bf:identifiedBy`; sidecar `helmet-map.jsonl`; Boundaries 1 & 2. |
| M3 | BIBFRAME → BFFI | Two SPARQL CONSTRUCTs; preserves Helmet identifiers; Boundary 3; `skos:prefLabel` language tagging via Lingua, with default-on local-LLM cascade for ambiguous parallel titles; surfaces Sierra-style Helmet bib ID via `dct:identifier`; default-off LLM cascade for 245$c contributor extraction (Qwen3 8B, multilingual stop-word heuristic gates ~13% of records, transliteration-variant detection skips emission and defers to M9 binding). |
| M4 | Work-key blocking | Stage-1 deterministic blocking key. |
| M5 | Embedding candidates | BGE-M3 (post-benchmark) + FAISS HNSW; persisted index; tightened thresholds. |
| M6 | LLM judge | Qwen3 cascade; SQLite cache; checkpoint; Boundary 4 validators. |
| M7 | Provenance logging | PROV-O + `bffi-prov` namespace; compaction subcommand; staleness warning. |
| M8 | Merge application | Union-find canonical Works; identifier-set union (`bf:identifiedBy` + Sierra-style `dct:identifier` per absorbed bib_id); multi-language `skos:prefLabel` union from M3 cascade; `canonical-map.jsonl`. |
| M9 | Reconciliation | KANTO → VIAF / YSO / KAUNO / MUSO; four-tier decision logic. |
| M10 | Skosify overlay + Fuseki load | Overlay-plus-inference; Helmet source URI declared; Boundary 5; `lookup-helmet` CLI. |
| M11 | Skosmos config | Pinned Skosmos 3.x; `fi`/`sv`/`en` priority; overlay labels `dct:identifier` / `bffi:subject` / `bffi:genreForm` / `bffi:creator` / `bf:role` for concept-page rendering; cross-vocabulary linking via authority dumps (KANTO/YSO/KAUNO/MUSO/SLM + LoC relators) loaded into local Fuseki (option 3b — `bffi-pipeline load-finto` / `make refresh-finto`). |
| M12 | Gold set + eval harness | Pairwise M6-judge gold set (`gold/gold.jsonl`, 50–100 cases target, 30 % hand-marked hold-out); single-record M3-cascade contributor-extraction gold set (`gold/contrib.jsonl`, scaffolding committed, cataloguer-curated extension pending); eval not in CI. |
| M13 | Documentation + handoff | Apache-2.0 LICENSE, README, runbook, architecture diagram. |

### CI

CI runs on **GitHub-hosted Ubuntu 24.04 runners only**:

| Check | Trigger |
|---|---|
| `ruff check` + `ruff format --check` | every push, every PR |
| `mypy --strict src` | every push, every PR |
| `pytest tests/unit` | every push, every PR |
| `pytest tests/integration -m "not requires_llm"` (Fuseki via Docker service) | every push, every PR |

The LLM eval is **not** in CI. Tests requiring a running Ollama instance carry the `@pytest.mark.requires_llm` mark and are excluded from CI; they run locally via `make test-integration`. `make eval` runs manually on the M5 Max before any PR touching `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/`. Output is pasted into the PR description per `.github/pull_request_template.md`.

Full milestone breakdown: `docs/archived/BUILD_PLAN.md`. CI rationale, workflow file, and PR template: `docs/ci-strategy.md`.

---

## Appendix: key references

### External

- **`docs/lkd.rdf`** — vendored BFFI 1.0.0 ontology (RDF/XML, ~4600 lines, published 2025-01-02). The canonical reference for class and property definitions; consult before adding any `bffi:*` term to spec, code, or shapes. Vendored because `https://schema.finto.fi/bffi/` is 403-protected outside the Finto network.
- **BFFI data model:** <https://schema.finto.fi/bffi/>
- **LKD project:** <https://github.com/NatLibFi/lkd>
- **`marc2bibframe2`:** <https://github.com/lcnetdev/marc2bibframe2>
- **`bib-rdf-pipeline`** (archived June 2025, reference only): <https://github.com/NatLibFi/bib-rdf-pipeline>
- **Skosmos:** <https://github.com/NatLibFi/Skosmos>
- **Skosify:** <https://github.com/NatLibFi/Skosify>
- **PROV-O:** <https://www.w3.org/TR/prov-o/>
- **Finto (live Skosmos instance):** <https://finto.fi/>
- **KANTO / YSO / KAUNO / MUSO (authority files):** via Finto
- **Ollama (local LLM server):** <https://ollama.com/>
- **vllm-mlx (production-batch inference):** <https://github.com/wsbagnsv1/vllm-mlx>
- **MLX (Apple's Metal-optimized framework):** <https://github.com/ml-explore/mlx>
- **Qwen3 model family:** <https://github.com/QwenLM/Qwen3>
- **BGE-M3 multilingual embeddings:** <https://huggingface.co/BAAI/bge-m3>

### Companion documents in this project

- **`CLAUDE.md`** (root) — operating constraints, conventions, do-nots.
- **`docs/archived/BUILD_PLAN.md`** — milestone-ordered build plan (M0–M13) with definitions of done.
- **`docs/validation-strategy.md`** — the five validation boundaries (MARCXML XSD, BIBFRAME shape, BFFI shape, judge semantic, post-load smoke).
- **`docs/local-inference.md`** — Apple Silicon memory budget, model expectations, throughput planning, cascade strategy.
- **`docs/external-dependencies.md`** — records and confirmations to request from Helmet cataloguers.
- **`docs/ci-strategy.md`** — CI rationale (Linux runners only; LLM eval runs manually on the M5 Max).
- **`docs/cataloguer-asks-fi.md`** — Finnish-language version of the cataloguer requests, for forwarding to Helmet staff.
