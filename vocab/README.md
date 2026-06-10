# vocab/

Vocabulary files the pipeline reads: the vendored BFFI ontology, plus
small TTL bridges we maintain ourselves between upstream authorities
that don't publish cross-references.

Distinct from:

- `finto-dumps/` — vendored upstream SKOS dumps from Finto (MTS, YSO,
  ALLFO, KANTO, etc.); we don't edit these.
- `config/` — pipeline-configuration TTLs (Skosify overlay, SHACL
  shapes); these shape pipeline behaviour rather than carry data.

| File | Purpose |
|---|---|
| `lkd.rdf` | Vendored BFFI 1.0.0 ontology (RDF/XML, ~4600 lines). The canonical reference for class and property definitions, AND the closed set of terms we may emit under the `bffi:` namespace. Vendored because `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network. |
| `loc-countries-bridge.ttl` | LoC MARC country code → YSO bridge with cached fi/sv/en prefLabels. See `docs/bffi_limitations.md` L-12. |
| `loc-issuance-bridge.ttl` | LoC issuance code bridge. |
| `loc-languages-bridge.ttl` | LoC MARC language code bridge. |

Licence: every file in this directory is CC0 — matching the project's
`vocab/lkd.rdf` policy and Finto vocabularies.
