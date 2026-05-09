# CLAUDE.md

BFFI pipeline: MARCXML → BFFI authority Works/Expressions → Skosmos. Pro bono; will be contributed to the National Library of Finland. Target corpus: ~800,000 Helmet bibliographic records.

## Project specs and plans

- `docs/marcxml-to-bffi-skosmos-pipeline.md` — technical spec (what to build).
- `docs/BUILD_PLAN.md` — milestone-ordered build plan (M0–M13). **Read before starting any milestone work.**
- `docs/validation-strategy.md` — five validation boundaries (MARCXML → BIBFRAME → BFFI → judge → post-load).
- `docs/local-inference.md` — Apple Silicon / Ollama setup, model choice, throughput, cascade strategy.
- `docs/external-dependencies.md` — records and confirmations to request from Helmet cataloguers.
- `docs/ci-strategy.md` — CI rationale and PR template.
- `docs/lkd.rdf` — full BFFI 1.0.0 ontology (RDF/XML, ~4600 lines), vendored because `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network. **The canonical reference for class and property definitions; consult before adding any `bffi:*` term to spec, code, or shapes.**

## Operating constraints

- Pro bono. **No paid API services.** All LLM inference runs locally on Apple Silicon (target: MacBook Pro M5 Max, 128 GB).
- Open-source tooling only.
- License: code **Apache 2.0** (matching NLF tools); published RDF data **CC0** (matching Finto vocabularies).
- No telemetry / error reporting.

## Committed identifiers (do not change without surfacing)

- Work URI namespace: `http://urn.fi/URN:NBN:fi:bib:work:`
- Expression URI namespace: `http://urn.fi/URN:NBN:fi:bib:expression:`
- Helmet source URI (used in `bf:identifiedBy`): `http://urn.fi/URN:NBN:fi:bib:source:helmet`
- Named-graph base for Fuseki: `http://urn.fi/URN:NBN:fi:bib:graph:`
- `bffi-prov` namespace: `http://urn.fi/URN:NBN:fi:schema:bffi-prov#` (provenance vocabulary — Activity classes, decision/confidence/rationale predicates, stage tags). Full `bffi-prov:stage` enum and Activity class list live in `docs/marcxml-to-bffi-skosmos-pipeline.md` § 8.
- `bffi:adminMetadata` linking property: `http://urn.fi/URN:NBN:fi:schema:bffi:adminMetadata` (`owl:equivalentProperty` of `bf:adminMetadata`). Every canonical `bffi:Work` and `bffi:Expression` carries one `bffi:adminMetadata` triple to a `bffi:AdminMetadata` block summarising administrative state. The AdminMetadata view is layered alongside the PROV-O graph (not a replacement); see spec § 8.
- Authority priority: KANTO → VIAF (persons / corporate bodies); YSO (subjects); KAUNO (fiction genre/form); MUSO (music).
- Display language priority for `skos:prefLabel`: `fi`, `sv`, `en`.
- Documentation language: English throughout.

## Conventions

- **URIs:** All minted via `src/bffi_pipeline/uris.py`. Never concatenate URI strings elsewhere. Deterministic SHA-1 of canonical inputs; UUIDs only for `prov:Activity` records.
- **Prompts:** All in `prompts/` as versioned files. Hashed at runtime; hash logged to provenance. Never inline in Python code.
- **SPARQL:** All in `sparql/` as versioned files. Read at startup; parametrize with Jinja2 if needed (autoescape off).
- **Idempotency:** Every stage has deterministic outputs and writes atomically (`.tmp` then rename). Re-runs skip when output is newer than input unless `--force`.
- **Stage isolation:** Stages in `src/bffi_pipeline/stages/` don't import each other. Orchestration lives in `cli.py`.
- **Errors over silent fallbacks:** If reconciliation fails, raise. If Fuseki is unreachable, raise. The only retry logic is in the LLM judge for transient API errors.
- **Provenance is mandatory:** Every merge/reconciliation decision (including negative) writes to the provenance graph before returning. No "optional logging" flag.
- **Type strictness:** `mypy --strict` on all of `src/`. Pydantic v2 for cross-module data. `dataclass(frozen=True)` for internal value objects.
- **Tests against fixtures, not network:** Unit tests never hit the API or Fuseki. Integration tests are tagged; LLM-dependent tests carry an additional `requires_llm` mark and are excluded from CI.

## Workflow rules

- Read `docs/BUILD_PLAN.md` before starting any milestone. Each milestone has an explicit definition of done; don't move on until it's met.
- `make lint && make test` must pass before any commit.
- LLM eval (`make eval`) runs locally on the M5 Max — never in CI. Output is pasted into the PR description if the PR touches `prompts/`, `gold/`, or `src/bffi_pipeline/stages/judge.py`.
- Commit messages tag the milestone, e.g. `M3: BIBFRAME → BFFI conversion`.
- Update the milestone checklist in `docs/BUILD_PLAN.md` (mark `[x]`) when done.

## What not to do

- Don't write a generic "MARC to anything" framework. This is a BFFI pipeline.
- Don't introduce a workflow engine (Airflow, Prefect, Dagster). The Makefile + typer CLI is the orchestration.
- Don't reach for async unless a stage genuinely benefits.
- Don't modify `third_party/marc2bibframe2/` (git submodule). Wrap, don't fork.
- Don't merge silent failures into provenance. Log `uncertain` with the actual error.
- Don't add features not in the build plan. Surface them as a milestone proposal first.
