# vocab/

Vocabulary files the pipeline reads: the vendored BFFI and BIBFRAME
ontologies, plus small TTL bridges we maintain ourselves between
upstream authorities that don't publish cross-references.

Distinct from:

- `finto-dumps/` — vendored upstream SKOS dumps from Finto (MTS, YSO,
  ALLFO, KANTO, etc.); we don't edit these.
- `config/` — pipeline-configuration TTLs (Skosify overlay, SHACL
  shapes); these shape pipeline behaviour rather than carry data.

| File | Purpose |
|---|---|
| `lkd.rdf` | Vendored BFFI 1.0.0 ontology (RDF/XML, ~4600 lines). The canonical reference for class and property definitions, AND the closed set of terms we may emit under the `bffi:` namespace. Vendored because `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network. |
| `bibframe.rdf` | Vendored BIBFRAME 3.0.1 ontology (RDF/XML, ~3200 lines, dated 2025-12-03 — the PMO-absorption release per `docs/bf_to_bffi_mapping.md`). Fetched from `https://id.loc.gov/ontologies/bibframe.rdf`. Used to cross-check what `bf:*` URIs marc2bibframe2 emits against the official BIBFRAME vocabulary — particularly important for surfacing terms BIBFRAME declares but `lkd.rdf` (BFFI) doesn't acknowledge (e.g. `bf:provisionActivityStatement`, `bf:accompaniedBy`). |
| `loc-countries-bridge.ttl` | LoC MARC country code → YSO bridge with cached fi/sv/en prefLabels. See the "Country labels — LoC vs YSO upstream gap" subsection of `docs/bf_to_bffi_mapping.md`. |
| `loc-issuance-bridge.ttl` | LoC issuance code bridge. |
| `loc-languages-bridge.ttl` | LoC MARC language code bridge. |

## Refreshing `bibframe.rdf`

LoC publishes the ontology at a content-negotiated URL:

```sh
curl -sSL -H "Accept: application/rdf+xml" \
    -o vocab/bibframe.rdf \
    https://id.loc.gov/ontologies/bibframe.rdf
```

Replace the file in-place; rdflib parses it via the existing
`Graph().parse(..., format="xml")` call site (see e.g.
`src/bffi_pipeline/stages/bibframe_to_bffi/mappings.py`). The
`owl:versionInfo` literal at the top of the file records the
publication version (e.g. `3.0.1`) — bump only when LoC publishes a
new ontology version we want to track.

Licence: every file in this directory is CC0 — matching the project's
`vocab/lkd.rdf` policy and Finto vocabularies.
