# MARCXML → BFFI → Skosmos Pipeline

A practical guide to converting MARCXML records into BFFI (the Finnish BIBFRAME profile) and publishing Work and Expression authority records via Skosmos, including LLM-assisted deduplication, provenance logging, and evaluation.

---

## 1. Background: what BFFI changes vs vanilla BIBFRAME

The Library of Congress's BIBFRAME 2.0 has only `bf:Work` and `bf:Instance`. BFFI (the National Library of Finland's profile, namespace `http://urn.fi/URN:NBN:fi:schema:bffi:`) splits `bf:Work` into disjoint classes `bffi:Work` and `bffi:Expression` corresponding to RDA. A useful framing from the LKD project: BIBFRAME works are essentially expressions; for every expression there exists a `bf:Work`. To add in RDA Works we just isolate some of the `bf:Work` properties and subclasses for `bffi:Work` and leave some for the Expression.

Conversion therefore must do two things that LoC's tooling doesn't:

1. Split each `bf:Work` into a `bffi:Work` (language/expression-independent) and one or more `bffi:Expression` (language, content type, etc.).
2. Re-route every property to the correct one of the two.

Current model is **BFFI 1.0.0**, published 2025-01-02.

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
           bffi:creator        ?primaryAgent ;
           bffi:subject        ?subject ;
           bffi:classification ?class ;
           bffi:originDate     ?originDate ;
           bffi:genreForm      ?workGenre ;
           bffi:marcKey        ?marcKey ;
           skos:prefLabel      ?workLabel .
}
WHERE {
  ?bfWork a bf:Work .

  BIND( IRI(CONCAT("https://example.org/work/",
            arq:sha1(STR(?bfWork)))) AS ?workURI )
  BIND( IRI(CONCAT("https://example.org/expression/",
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
           bffi:contributor     ?otherAgent ;
           bffi:summary         ?summary ;
           bffi:tableOfContents ?toc ;
           bffi:note            ?note ;
           skos:prefLabel       ?exprLabel .
}
WHERE {
  ?bfWork a bf:Work .
  BIND( IRI(CONCAT("https://example.org/work/",
            arq:sha1(STR(?bfWork)))) AS ?workURI )
  BIND( IRI(CONCAT("https://example.org/expression/",
            arq:sha1(STR(?bfWork)))) AS ?exprURI )

  OPTIONAL { ?bfWork bf:language        ?language }
  OPTIONAL { ?bfWork bf:content         ?contentType }
  OPTIONAL { ?bfWork bf:summary         ?summary }
  OPTIONAL { ?bfWork bf:tableOfContents ?toc }
  OPTIONAL { ?bfWork bf:note            ?note }

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

- URI minting must be deterministic and reversible so re-runs produce stable identifiers. In production, hash the `bflc:workKey` rather than the `bf:Work` URI so identical works across records collapse naturally.
- Property allocation above is illustrative — check `schema.finto.fi/bffi/` for canonical assignments per BFFI subclass before shipping.
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
    void:uriSpace    "https://example.org/work/" ;
    skosmos:language "en", "fi", "sv" ;
    skosmos:defaultLanguage "fi" ;
    skosmos:sparqlGraph     <https://example.org/graphs/bffi-works> ;
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

- `void:uriSpace` must match your minted Work URI prefix exactly, otherwise Skosmos won't recognize internal links.
- `skosmos:sparqlDialect "JenaText"` enables the `text:query` predicate Skosmos uses for fast label search; without it, search becomes painfully slow at scale.
- To display Works with their Expressions hierarchically, model the link with `skos:narrower`/`skos:broader` *in addition* to `bffi:hasExpression`/`bffi:expressionOf`. This is handled automatically by the Skosify overlay below.

---

## 5. Skosify: the overlay-plus-inference approach

The Skosify `[types]` section is **destructive** — it replaces source classes with SKOS classes. Instead, declare the BFFI/SKOS subclass relationships in a small overlay file and let Skosify's RDFS inference add `skos:Concept` while keeping the BFFI types intact. This is the more robust path because new BFFI subclasses only require an overlay update, not a pipeline change.

### Overlay file

```turtle
# bffi-skos-overlay.ttl  (load alongside your converted data)
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

namespace = https://example.org/work/
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

Compute a cheap rule-based key from MARC 100/240/245 (normalized creator surname + first significant title word + content type). Only consider pairs within the same block. This eliminates >99% of comparisons before any model runs.

### Stage 2 — Embedding similarity

Within each block, embed a structured string per record using a multilingual model (BGE-M3, multilingual-e5, jina-embeddings-v3). Index in FAISS or Qdrant. Set asymmetric thresholds:

- ≥ 0.92 → auto-merge
- ≤ 0.75 → reject
- 0.75–0.92 → escalate to LLM judge

Embeddings genuinely understand that "Tolstoi" and "Толстой" are the same person and that "Sota ja rauha" and "War and Peace" share a referent.

### Stage 3 — LLM judge for the gray zone

Send only ambiguous pairs to a model with a structured-output schema. Force it to quote field values rather than free-associate. Temperature 0, seeded.

### Operational principles

- **Provenance:** store the model name, version, prompt hash, and rationale alongside every merge decision. Ground every judgment in field-level evidence.
- **Asymmetric thresholds:** false merges cost much more than false splits. Bias toward keeping things separate when uncertain.
- **Use LLMs for normalization too:** giving the model a creator string and a list of KANTO/VIAF candidates and asking it to pick the right URI is a much better-bounded task than open-ended dedup.

### Finnish-context shortcut

Use **KANTO** (Finnish authority file, published via Finto/Skosmos) as your reconciliation target. Embed your MARC 100/700 strings, retrieve KANTO candidates, let the LLM pick. Same approach works against **YSO** for subjects and **KAUNO** for fiction genre/form.

The best work-key after all this is *not* a string — it's the URI of the canonical creator (KANTO/VIAF) plus the URI of the original-language preferred title. That key is stable under language, transliteration, and edition variation.

---

## 7. The stage-3 LLM judge

```python
"""
Stage-3 work-merge judge for ambiguous candidate pairs.
Run only on pairs with embedding similarity in the gray zone (0.75–0.92).
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.globals import set_llm_cache
from langchain_community.cache import SQLiteCache

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


class WorkMatchDecision(BaseModel):
    """Structured judgment. The model fills this — nothing else is allowed."""
    decision: Literal["same_work", "different_work", "uncertain"]
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0.0–1.0. Use <0.7 when uncertain; reserve >0.9 for clear cases."
    )
    rationale: str = Field(
        description=(
            "2–4 sentences citing specific field values from BOTH records. "
            "Do not introduce facts not present in the inputs."
        )
    )
    matching_fields: list[str] = Field(default_factory=list)
    diverging_fields: list[str] = Field(default_factory=list)


# --- Prompt ----------------------------------------------------------------

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

llm = ChatAnthropic(model="claude-opus-4-5", temperature=0)
judge = prompt | llm.with_structured_output(WorkMatchDecision)


def judge_pair(a: WorkRecord, b: WorkRecord, sim: float) -> WorkMatchDecision:
    return judge.invoke({
        "record_a": a.model_dump_json(indent=2, exclude_none=True),
        "record_b": b.model_dump_json(indent=2, exclude_none=True),
        "sim": sim,
    })
```

---

## 8. Provenance logging

Every merge decision becomes a discrete `prov:Activity` in a separate named graph, with PROV-O for the standard skeleton plus a small custom extension for LLM-specific fields.

### The shape

```turtle
# Stored in graph <https://example.org/graphs/provenance>
@prefix prov:      <http://www.w3.org/ns/prov#> .
@prefix bffi:      <http://urn.fi/URN:NBN:fi:schema:bffi:> .
@prefix bffi-prov: <https://example.org/ns/bffi-prov#> .
@prefix skos:      <http://www.w3.org/2004/02/skos/core#> .
@prefix dct:       <http://purl.org/dc/terms/> .
@prefix xsd:       <http://www.w3.org/2001/XMLSchema#> .
@prefix ex:        <https://example.org/> .

# --- The decision itself ---
ex:merge/01HXYZ... a prov:Activity, bffi-prov:WorkMergeDecision ;
    prov:startedAtTime    "2026-05-08T10:23:14Z"^^xsd:dateTime ;
    prov:endedAtTime      "2026-05-08T10:23:18Z"^^xsd:dateTime ;
    prov:wasAssociatedWith ex:agent/claude-opus-4-5 ;
    prov:used             ex:rawwork/melinda-001234567 ,
                          ex:rawwork/melinda-009876543 ;
    bffi-prov:stage       "llm-judge" ;
    bffi-prov:decision    "same_work" ;
    bffi-prov:confidence  "0.91"^^xsd:decimal ;
    bffi-prov:embeddingSimilarity "0.84"^^xsd:decimal ;
    bffi-prov:rationale   "Same creator under transliteration variants, both records have original_language='ru'; B's expression_language='fi' indicates a Finnish Expression." ;
    bffi-prov:matchingField  "creator", "original_language", "date_of_origin" ;
    bffi-prov:divergingField "expression_language" ;
    bffi-prov:promptHash  "sha256:9a1f7c3e..." ;
    bffi-prov:rawResponse "<<full JSON the model returned>>" .

# --- The canonical Work points back at its provenance ---
ex:work/tolstoy-war-and-peace a bffi:Work, skos:Concept ;
    skos:prefLabel        "Sota ja rauha"@fi ;
    prov:wasGeneratedBy   ex:merge/01HXYZ... ;
    prov:wasDerivedFrom   ex:rawwork/melinda-001234567 ,
                          ex:rawwork/melinda-009876543 .

# --- Agents ---
ex:agent/claude-opus-4-5 a prov:SoftwareAgent ;
    rdfs:label            "Claude Opus 4.5" ;
    bffi-prov:provider    "Anthropic" ;
    bffi-prov:modelId     "claude-opus-4-5" ;
    bffi-prov:temperature "0.0"^^xsd:decimal .

# --- Human review chains onto the original Activity ---
ex:review/01HX2A... a prov:Activity, bffi-prov:HumanReview ;
    prov:wasInformedBy    ex:merge/01HXYZ... ;
    prov:wasAssociatedWith ex:agent/cataloguer/jdoe ;
    prov:atTime           "2026-05-09T14:00:00Z"^^xsd:dateTime ;
    bffi-prov:decision    "confirmed" ;
    bffi-prov:reviewNote  "Verified against KANTO authority record (KANTO00012345)." .
```

### Helper

```python
import hashlib
from datetime import datetime, timezone
from ulid import ULID
from rdflib import Graph, Namespace, Literal, URIRef, RDF, RDFS, XSD

PROV      = Namespace("http://www.w3.org/ns/prov#")
BFFI_PROV = Namespace("https://example.org/ns/bffi-prov#")
EX        = Namespace("https://example.org/")

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
    stage: str = "llm-judge",
    started_at: datetime | None = None,
    ended_at:   datetime | None = None,
) -> URIRef:
    activity = EX[f"merge/{ULID()}"]
    started_at = started_at or datetime.now(timezone.utc)
    ended_at   = ended_at   or datetime.now(timezone.utc)
    prompt_hash = "sha256:" + hashlib.sha256(prompt_template.encode()).hexdigest()

    g.add((activity, RDF.type, PROV.Activity))
    g.add((activity, RDF.type, BFFI_PROV.WorkMergeDecision))
    g.add((activity, PROV.startedAtTime, Literal(started_at.isoformat(), datatype=XSD.dateTime)))
    g.add((activity, PROV.endedAtTime,   Literal(ended_at.isoformat(),   datatype=XSD.dateTime)))
    g.add((activity, PROV.wasAssociatedWith, EX[f"agent/{model_id}"]))
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
  GRAPH <https://example.org/graphs/provenance> {
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

**Which raw MARC records ended up under a given canonical Work:**

```sparql
SELECT ?rawWork ?marcKey WHERE {
  <https://example.org/work/tolstoy-war-and-peace>
      prov:wasDerivedFrom ?rawWork .
  ?rawWork bffi:marcKey ?marcKey .
}
```

**LLM merges humans later overrode (gold-set training data):**

```sparql
SELECT ?activity ?confidence ?rationale ?reviewNote WHERE {
  GRAPH <https://example.org/graphs/provenance> {
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

- Keep provenance in a **separate named graph**; do *not* include it in Skosmos's `void:sparqlGraph`. Otherwise Skosmos will render `prov:Activity` URIs as concepts.
- **Retention policy:** keep the structured Activity record indefinitely (it's small); compact `bffi-prov:rawResponse` after ~90 days.
- **Version the prompt template in git** and store the file path alongside the hash (`bffi-prov:promptSource "git://repo/prompts/judge_v3.txt"`).
- **Log negative decisions too**, not just merges — when a cataloguer asks "why didn't these merge?", you need the answer.

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
- **Hold-out portion** that never appears in few-shot prompts.
- Every case carries `category` and ideally `notes`.

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

- **Run on every prompt change**, not just releases. Gate PRs on per-category regression, not aggregate.
- **Feed overrides back in.** The "humans overrode the LLM" SPARQL query is your gold-set growth pipeline. Run monthly, present candidates, add confirmed ones with `category` filled in. Aim for 20–50 production-derived cases per month.
- **Track runs over time.** Plot per-category accuracy across runs. "We lost 8 points on `transliteration-variant` three releases ago and never recovered" is invisible in any single run.

### A subtlety worth internalizing

The gold set should be representative of your **failure modes**, not your **corpus**. If 95% of real pairs are obvious same-language reprints, the gold set should still be heavily weighted toward the hard 5%. Otherwise high gold-set accuracy will be uninformative because it'll be dominated by easy cases the model gets right by reflex.

---

## Appendix: key references

- **BFFI data model:** <https://schema.finto.fi/bffi/>
- **LKD project:** <https://github.com/NatLibFi/lkd>
- **`marc2bibframe2`:** <https://github.com/lcnetdev/marc2bibframe2>
- **`bib-rdf-pipeline`** (archived June 2025, reference only): <https://github.com/NatLibFi/bib-rdf-pipeline>
- **Skosmos:** <https://github.com/NatLibFi/Skosmos>
- **Skosify:** <https://github.com/NatLibFi/Skosify>
- **PROV-O:** <https://www.w3.org/TR/prov-o/>
- **Finto (live Skosmos instance):** <https://finto.fi/>
- **KANTO (Finnish authority file):** via Finto
