# vocab/

Project-owned local vocabulary / bridge TTLs.

This directory holds small TTL files we maintain ourselves — typically
bridges between two upstream authorities that neither side publishes
a cross-reference for. Distinct from:

- `finto-dumps/` — vendored upstream SKOS dumps from Finto (MTS, YSO,
  ALLFO, KANTO, etc.); we don't edit these.
- `config/` — pipeline-configuration TTLs (Skosify overlay, SHACL
  shapes); these shape pipeline behaviour rather than carry data.
- `docs/lkd.rdf` — the vendored BFFI 1.0.0 ontology, kept under
  `docs/` for legacy reasons (predates this directory).

| File | Purpose |
|---|---|
| `loc-countries-bridge.ttl` | LoC MARC country code → YSO bridge with cached fi/sv/en prefLabels. See `docs/bffi_limitations.md` L-12. |

Licence: every file in this directory is CC0 — matching the
project's `docs/lkd.rdf` policy and Finto vocabularies.
