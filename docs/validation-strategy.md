# Validation strategy

Data crosses five distinct boundaries in the pipeline, and each gets its own validation. Don't conflate them — rdflib's permissive parsing is **not** validation, and silent acceptance at one boundary becomes corruption at the next.

## The five boundaries

| # | Boundary | When | Tool | Failure mode |
|---|---|---|---|---|
| 1 | MARCXML input | M2 entry | XSD + minimum-content checks | Skip record, log to `_errors.jsonl` |
| 2 | BIBFRAME post-conversion | M2 exit | SHACL (`bibframe-conversion.shape.ttl`) | Skip record, log to `_errors.jsonl` |
| 3 | BFFI post-CONSTRUCT | M3 exit | SHACL (`bffi.shape.ttl`) | Report to `_validation.jsonl`, warn on CLI, don't block |
| 4 | Judge output | M6 | Pydantic structural + semantic validators | Retry; on persistent failure log `decision="uncertain"` |
| 5 | Post-load smoke | M10 exit | SPARQL `ASK` queries | Roll back the load |

## Layout

```
config/shapes/
├── bibframe-conversion.shape.ttl    # Boundary 2
├── bffi.shape.ttl                   # Boundary 3 — the big one
└── post-load-smoke.rq               # Boundary 5

src/bffi_pipeline/
├── schemas/
│   └── MARC21slim.xsd               # vendored from LoC, version pinned in a comment
└── validation/
    ├── marcxml.py                   # Boundary 1
    ├── bibframe.py                  # Boundary 2 (pyshacl wrapper)
    ├── bffi.py                      # Boundary 3 (pyshacl wrapper)
    ├── decisions.py                 # Boundary 4 (Pydantic validators)
    └── post_load.py                 # Boundary 5
```

## What each boundary checks

### Boundary 1 — MARCXML input

Two stages.

**Stage 1: XSD validation** against the LoC MARC21 slim schema using cached `lxml.etree.XMLSchema`.

**Stage 2: minimum-content check.** At least one 1XX/7XX (creator), one 245 (title), one 008, one 336/337/338 (RDA content/media/carrier).

The XSD catches malformed XML; the content check catches records too thin to produce useful BFFI. Failures are typed (`marcxml-xml-syntax`, `marcxml-xsd-validation`, `marcxml-content-minimum`) so you can grep the error log by category.

### Boundary 2 — BIBFRAME post-conversion

A small SHACL shape verifying what the pipeline assumes from marc2bibframe2:

- Every record produces at least one `bf:Work`.
- Every Work has a `bf:title` with `bf:mainTitle`.
- Every Work has at least one `bf:contribution`.

Intentionally minimal — this validates "BIBFRAME my pipeline can handle," not "correct BIBFRAME" (the latter is unbounded).

### Boundary 3 — BFFI post-CONSTRUCT

All shape constraints below derive from `docs/lkd.rdf`; that file is the single source of truth for BFFI domain/range checks. The shape file `config/shapes/bffi.shape.ttl` should carry a comment naming the schema commit it was generated against, so a future BFFI revision can be diffed against the shape.

This is where validation pays for itself. Required shapes:

- Every `bffi:Work` has at least one `bffi:hasExpression`.
- Every `bffi:Expression` has exactly one `bffi:expressionOf` pointing at a `bffi:Work`.
- Every Work and Expression has `bf:identifiedBy` with `bf:source = <http://urn.fi/URN:NBN:fi:bib:source:helmet>` (Helmet ID preservation).
- Every Work has `skos:prefLabel` in at least one of `fi`/`sv`/`en`.
- Class disjointness — nothing is both `bffi:Work` and `bffi:Expression`.
- Properties BFFI assigns to Work-only don't appear on Expression and vice versa. **This is the invariant most likely to drift as the model evolves; don't skip it.**
- Every canonical `bffi:Work` and every `bffi:Expression` has exactly one `bffi:adminMetadata` triple pointing at one `bffi:AdminMetadata` instance. Each `bffi:AdminMetadata` instance carries at minimum the six required predicates: `bffi:adminMetadataFor`, `bffi:descriptionCreationDate`, `bffi:descriptionConventions`, `bffi:descriptionAuthentication`, `bffi:generationProcess`, `bffi:recordingSource` (plus `bffi:metadataLicensor` for the CC0 commitment). See spec § 8 "AdminMetadata view".

`pyshacl` runs this on every conversion batch. Failures don't block the pipeline (you still get the data) but they surface in `<BFFI_DATA_DIR>/bffi/_validation.jsonl` and emit a CLI warning. CI fails on regressions in the validation report against a baseline.

### Boundary 4 — Judge output

Pydantic structural constraint via `with_structured_output(WorkMatchDecision, method="json_schema")` is already in M6. Add semantic post-validators with `@model_validator(mode="after")`:

- `decision="uncertain"` with `confidence > 0.7` is incoherent.
- `decision="same_work"` with empty `matching_fields` is incoherent.
- Rationale containing stub phrases ("I don't know", "unable to determine", "n/a", "not sure") is invalid.
- Rationale shorter than 20 characters is invalid.

Validation failures here trigger the same retry path as JSON parse errors (max 2 retries; persistent failure → log `decision="uncertain"` with the validation error in the rationale). **Validation-failed responses must not be cached** — otherwise you cement bad outputs and re-runs reproduce them.

### Boundary 5 — Fuseki post-load

SPARQL `ASK` queries that must all return `true` after loading:

```sparql
ASK { ?w a bffi:Work, skos:Concept ; skos:prefLabel ?l }
ASK { ?e a bffi:Expression, skos:Concept ; bffi:expressionOf ?w }
ASK { ?w a bffi:Work ; bf:identifiedBy [ bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] }
ASK { ?w skos:narrower ?e . ?e skos:broader ?w }   # Skosify-inferred inverses

# AdminMetadata view — every canonical Work and every Expression has a populated
# bffi:AdminMetadata block, and the vocabulary instances loaded from
# config/bffi-admin-vocabulary.ttl resolve to typed entities.
ASK { ?w a bffi:Work ;
         bffi:adminMetadata [ a bffi:AdminMetadata ;
                              bffi:descriptionConventions <http://urn.fi/URN:NBN:fi:bib:desc-conv/bffi-1.0.0> ;
                              bffi:descriptionAuthentication ?auth ] }
ASK { ?e a bffi:Expression ;
         bffi:adminMetadata [ a bffi:AdminMetadata ;
                              bffi:generationProcess ?gp ] }
ASK { <http://urn.fi/URN:NBN:fi:bib:auth/auto-merged> a bffi:DescriptionAuthentication }
```

These run automatically after `make publish` and before declaring the load successful. Failure rolls back the load (delete the loaded graph; don't leave Fuseki in a half-loaded state).

## What this isn't

Deliberate non-goals:

- **No validating intermediate rdflib graphs in unit tests.** The shapes are the contract; once they pass, unit tests check specific behaviors, not graph well-formedness.
- **No separate Skosify-output validation boundary.** If Skosify produces something that fails the BFFI shape, that's a Skosify config problem feeding back to M5, not a new validation point.
- **No blocking on Boundary-3 validation in production runs.** At 800k records, even a 0.1% failure rate is 800 records — surface them in the report and let cataloguers triage. The CI gate is where strict blocking happens.
- **No tests that re-implement validation.** The shapes file *is* the test. Unit tests verify the shapes work by hand-crafting one valid graph and one invalid graph per shape and checking each is judged correctly.

## A note on the BFFI shape file

`bffi.shape.ttl` is going to be the most-edited artifact in the repo over time. Every BFFI version bump, every new Work or Expression subclass, every new property in the model — they all flow through this file. Treat it as living documentation: every change gets a comment explaining what real-world failure mode it catches, and every new shape gets a corresponding pair of unit-test fixtures (one valid, one invalid). It's the one file where stale shapes silently let bad data through and nobody notices for months.
