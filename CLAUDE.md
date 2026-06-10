# CLAUDE.md (rewrite branch)

Conversion-first BFFI pipeline: MARCXML ↔ BIBFRAME (LoC marc2bibframe2) ↔ BFFI canonical Turtle — both directions. Pro bono; will be contributed to the National Library of Finland. Target corpus: ~800,000 Helmet bibliographic records.

**This branch's scope is bidirectional conversion + evaluation only.** Four pillars:

1. **MARCXML export** from the Helmet Sierra dump.
2. **MARC → BIBFRAME** via the LoC marc2bibframe2 XSLT.
3. **BIBFRAME → BFFI** via SPARQL CONSTRUCT (or equivalent RDF processing), emitting BFFI-only canonical Turtle.
4. **BFFI → MARC** — the reverse direction, reconstructing MARCXML from the canonical BFFI graph for round-trip verification, downstream MARC consumers, and the diff residue registry in `docs/bffi_limitations.md`.

Plus an evaluation harness wrapping the four — round-trip diff, cataloguer-review HTML, mapping-discipline tests.

No clustering, no LLM judge, no reconciliation, no Skosmos load enrichment. Anything in those stages stays on `main` as the legacy reference; see `docs/plans/p-057-rewrite-conversion-first-branch.md` (import from main when ready) for the rewrite's framing.

## Project docs

- `vocab/lkd.rdf` — full BFFI 1.0.0 ontology (RDF/XML, ~4600 lines), vendored because `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network. **The canonical reference for class and property definitions, AND the closed set of terms we may emit under the `bffi:` namespace.** See the BFFI namespace discipline rule in Conventions.
- `docs/bf_to_bffi_mapping.md` — generated reference (rdflib parse of `lkd.rdf`) for every `bf:*` class and predicate encountered in the conversion, with its `bffi:*` counterpart and routing notes. Source of truth for every conversion decision. Sent to NLF for review.
- `docs/bffi_limitations.md` — registry of round-trip cases where source MARC data survives but lands in a different MARC field / shape / convention because BFFI 1.0.0 + marc2bibframe2 don't preserve the distinction (e.g. MARC 260 → 264, `$2 yso/fin` → `$2 yso`). Each entry pinpoints the BFFI ontology shortfall and documents the conclusion (acceptable / planned-fix / candidate-for-NLF-extension). Add a new entry whenever you find a similar case during a diff residue audit.
- `docs/validation-strategy.md` — three validation boundaries on the conversion side (MARCXML input → BIBFRAME post-conversion → BFFI post-CONSTRUCT).
- `docs/observability.md` — local Prometheus + Grafana stack wrapped by Caddy. Stage events → JSONL sidecar → tail-and-export → Prometheus scrape → Grafana panels, all reachable at `http://localhost:8080`. **Built in from the ground up — every stage emits structured events from its first commit.** See the observability constraint in Operating constraints.
- `docs/plans/` — plans of record, one file per plan as `p-NNN-<slug>.md` (three-digit zero-padded; flat — no sub-folders). Status is tracked in `docs/plans/README.md` (not by sub-folder). Filenames stay stable across status transitions; the file's own `git log` is the lineage. **Consult `docs/plans/README.md` before recommending an architectural change** — the idea may already be on record.

## Operating constraints

- Pro bono. **No paid API services.**
- Open-source tooling only.
- License: code **Apache 2.0** (matching NLF tools); published RDF data **CC0** (matching Finto vocabularies).
- **Observability is built in from the ground up.** Every stage emits structured events to a `stage-events.jsonl` sidecar from its first commit. The local Prometheus + Grafana stack (wrapped by Caddy at `http://localhost:8080`) is the operator's single source of truth for "what's happening right now?" See `docs/observability.md`.
- No **outbound** telemetry / error reporting — no Datadog, Sentry, Honeycomb, or similar. The Prometheus + Grafana + Caddy stack runs entirely on the operator's machine; no data leaves the box.

## Committed identifiers (do not change without surfacing)

- Work URI namespace: `http://urn.fi/URN:NBN:fi:bib:work:`
- Expression URI namespace: `http://urn.fi/URN:NBN:fi:bib:expression:`
- Manifestation URI namespace: `http://urn.fi/URN:NBN:fi:bib:manifestation:` (1:1 with Helmet bib records).
- Helmet source URI: `http://urn.fi/URN:NBN:fi:bib:source:helmet`
- `bffi-prov` namespace: `http://urn.fi/URN:NBN:fi:schema:bffi-prov#` (provenance vocabulary — Activity classes, stage tags).
- `bffi:adminMetadata` linking property: `http://urn.fi/URN:NBN:fi:schema:bffi:adminMetadata` (`owl:equivalentProperty` of `bf:adminMetadata`).
- Display language priority for `skos:prefLabel`: `fi`, `sv`, `en`.
- Documentation language: English throughout.

## Conventions

- **URIs**: All minted through a single helper module; never concatenate URI strings elsewhere. Deterministic SHA-1 of canonical inputs; UUIDs only for `prov:Activity` records.
- **BFFI namespace discipline**: The `bffi:` namespace (`http://urn.fi/URN:NBN:fi:schema:bffi:`) is **closed**. We may only emit classes and properties that exist in `vocab/lkd.rdf`. When we need something not in `lkd.rdf`, pick in this order:
    1. **Reuse an existing standard term.** RDF (`rdf:Statement` reification), RDFS, OWL, SKOS, PROV-O, DC Terms. Prefer this path.
    2. **Use the `bffi-prov:` namespace** for *pipeline-internal* metadata — Activity classes, decision audit predicates, synthetic-sentinel flags. This namespace is ours; extending it is fine.
    3. **Propose adding the term to BFFI through NLF.** Open a plan in `docs/plans/`. Until ratified, do not emit it under `bffi:`.
- **Hard-cut closed-namespace emit**: **zero `bf:*` URIs in the BFFI emit graph.** BIBFRAME (`bf:*`) stays inside the conversion's INPUT (the marc2bibframe2 output); it must never appear in the BFFI output. The BIBFRAME view is recoverable by OWL inference through the re-anchor pattern (`bffi:Sub rdfs:subClassOf bffi:Anchor owl:equivalentClass bf:X`). See `docs/bf_to_bffi_mapping.md` for the routing decisions.
- **Turtle prefix bindings**: Every Turtle-serialising path MUST bind its namespaces through a single shared helper. Never write a private `graph.bind("foo", FOO)` list — even for one or two prefixes. rdflib invents non-deterministic `@prefix ns1: …` declarations otherwise, and concatenating Turtle from different records then silently reinterprets the local-name half of `ns1:…` triples in whichever record's prefix block loses the redeclaration race.
- **SPARQL**: All in `sparql/` as versioned files. Read at startup; parametrize with Jinja2 if needed (autoescape off).
- **Idempotency**: Every stage has deterministic outputs and writes atomically (`.tmp` then rename). Re-runs skip when output is newer than input unless `--force`.
- **Stage isolation**: Stages don't import each other. Orchestration lives in `cli.py`.
- **Errors over silent fallbacks**: Conversion failures raise. The only retry logic is for transient external errors (e.g. a vocab fetch).
- **Provenance is mandatory**: Every conversion decision that does anything non-trivial (e.g. discriminator-routing a `bf:Hub` to `bffi:Work` vs `bffi:Expression`) writes to the provenance graph before returning. No "optional logging" flag.
- **Type strictness**: `mypy --strict` on all of `src/`. Pydantic v2 for cross-module data. `dataclass(frozen=True)` for internal value objects.
- **Tests against fixtures, not network**: Unit tests never hit external services.

## Workflow rules

- Before starting work on a plan, read it through. If you're not working off a plan, check `docs/plans/README.md` first to see whether a plan or proposal already covers the work.
- `make lint && make test` must pass before any commit.
- **Canonical run-directory convention**: every pipeline invocation writes into a `runs/<yyyymmdd-hhmm-<6hex>>/` directory (UTC timestamp + 6 random hex chars). Mint one with `bffi-pipeline new-run` and pass it (or sub-paths under it) to each stage's `--output-dir` / `--html` option. The CLI validates this on every output-side argument; non-canonical paths exit with `error: --output-dir: …` before the stage starts. Existing ad-hoc directories like `runs/test-step5-20k/` predate the convention and aren't re-used. See `src/bffi_pipeline/runs.py`.
- Commit messages tag the relevant stage or plan phase, e.g. `convert: BIBFRAME → BFFI routing for bf:Hub` or `P-057 Phase A: scaffold conversion driver`.

## What not to do

- Don't write a generic "MARC to anything" framework. This is a BFFI pipeline.
- Don't introduce a workflow engine (Airflow, Prefect, Dagster). The Makefile + typer CLI is the orchestration.
- Don't reach for async unless a stage genuinely benefits.
- Don't modify `third_party/marc2bibframe2/` (git submodule). Wrap, don't fork.
- Don't emit `bf:*` URIs from the BFFI conversion. The namespace boundary is hard-cut.
- Don't mint local `bffi:` terms. The namespace is closed to what `vocab/lkd.rdf` declares — see the **BFFI namespace discipline** rule in Conventions for the legitimate alternatives.
- Don't write private `graph.bind("foo", FOO)` lists when emitting Turtle. Every binding goes through the shared helper.
- Don't merge silent failures into provenance. Log `uncertain` with the actual error.
- Don't add features that aren't covered by a plan in `docs/plans/`. Surface new directions as a plan with status `proposed` first; only flip to `active` after the trade-off is on the record.
- Don't add downstream-stage code (clustering, judge, reconciliation, Skosmos load) on this branch. Those stay on `main`.
- Don't read `bffi-prov:` (pipeline-internal provenance) when reconstructing MARC in the BFFI → MARC direction. The whole point of the round-trip is to verify that the `bffi:` namespace alone can reconstruct the source. Pipeline-internal data is fair for UI / pairing machinery (e.g. lineage tokens used by the diff comparator), never for deciding what content emits. See `docs/bffi_limitations.md`'s cardinal-rule note.
