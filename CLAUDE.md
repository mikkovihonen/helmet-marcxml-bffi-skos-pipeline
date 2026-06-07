# CLAUDE.md

BFFI pipeline: MARCXML → BFFI authority Works/Expressions → Skosmos. Pro bono; will be contributed to the National Library of Finland. Target corpus: ~800,000 Helmet bibliographic records.

## Project specs and plans

- `docs/tech-stack.md` — consolidated toolchain reference (languages, RDF tooling, inference stack, vocabularies, services). Start here for "what does this project use for X?".
- `docs/validation-strategy.md` — five validation boundaries (MARCXML → BIBFRAME → BFFI → judge → post-load).
- `docs/bibliographic-minimum.md` — the minimum bibliographic field set every exported record must carry + the full synthesis-policy table (which fields the pipeline synthesises, where, with what marker, at what confidence). Source-of-truth for P-41 Phase B's salvage tiers and the `bffi-prov:Synthesis` Activity contract.
- `docs/local-inference.md` — Apple Silicon / mlx-lm setup, model choice, throughput, cascade strategy.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` — original end-to-end technical specification (archived). Section-level back-references from older commits, plans, and source comments still point here; live successors are listed at the top of that document.
- `docs/external-dependencies.md` — records and confirmations to request from Helmet cataloguers.
- `docs/ci-strategy.md` — CI rationale and PR template.
- `docs/lkd.rdf` — full BFFI 1.0.0 ontology (RDF/XML, ~4600 lines), vendored because `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network. **The canonical reference for class and property definitions, AND the closed set of terms we may emit under the `bffi:` namespace.** See the BFFI namespace discipline rule in Conventions.
- `docs/plans/` — committed-to-action plans of record (`p-<NN>-<slug>.md`). Each plan has sequenced phases with verification checkpoints, a risk register, and a rollback procedure, plus a `Plan-base commit` and `Phase commits` for tying execution to git history. **State is encoded by sub-folder**: `proposed/` (forward-looking ideas, proposal-shape content, status `proposed | rejected (reason)`), `backlog/` (graduated, drafted, not started), `in-progress/` (at least one phase shipped), `completed/` (done), `abandoned/` (dropped, with reason). Filenames use the `p-<NN>-<slug>.md` convention uniformly across every sub-folder so state transitions are a single `git mv` (plus a content rewrite from proposal-shape to plan-shape on first graduation). `git log --follow <path>` traces a document's lineage end-to-end. Completed plans with `Source proposal: prop-<NN>-...` fields predate the 2026-05-14 naming unification and are historically accurate — don't rewrite them. **Consult `docs/plans/proposed/` and the current plans before recommending an architectural change** — the idea may already be on record.
- `docs/archived/` — historical / superseded documents kept for reference only. Includes `BUILD_PLAN.md` (original build-order checklist for M0-M13 — superseded by `docs/plans/`; **don't cite it as the source of M-number meanings in new code or docs**, the M-prefix today means pipeline stage, not build milestone) and `marcxml-to-bffi-skosmos-pipeline.md` (original technical spec; live successors are listed in the document's archived banner). Path references from source code or live docs may point here; do not edit archived material except for typos or to add a supersede pointer.

## Operating constraints

- Pro bono. **No paid API services.** All LLM inference runs locally on Apple Silicon (target: MacBook Pro M5 Max, 128 GB).
- Open-source tooling only.
- License: code **Apache 2.0** (matching NLF tools); published RDF data **CC0** (matching Finto vocabularies).
- No **outbound** telemetry / error reporting — i.e. no Datadog, Sentry, Honeycomb, or any other monitoring service that sends pipeline data to a third party. Local-only observability is fine: running Prometheus + Grafana in a container next to the existing Fuseki + Skosmos services (so the operator can `localhost:3001` a dashboard) does not violate this constraint because no data leaves the operator's machine. See `docs/plans/completed/p-11-structured-observability.md` for the local stack.

## Committed identifiers (do not change without surfacing)

- Work URI namespace: `http://urn.fi/URN:NBN:fi:bib:work:`
- Expression URI namespace: `http://urn.fi/URN:NBN:fi:bib:expression:`
- Manifestation URI namespace: `http://urn.fi/URN:NBN:fi:bib:manifestation:` (1:1 with Helmet bib records; no canonical/raw split — the raw URI minted at M3 IS the canonical URI because Manifestations don't merge)
- Helmet source URI (used in `bf:identifiedBy`): `http://urn.fi/URN:NBN:fi:bib:source:helmet`
- Named-graph base for Fuseki: `http://urn.fi/URN:NBN:fi:bib:graph:`
- `bffi-prov` namespace: `http://urn.fi/URN:NBN:fi:schema:bffi-prov#` (provenance vocabulary — Activity classes, decision/confidence/rationale predicates, stage tags). Full `bffi-prov:stage` enum and Activity class list live in `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 (archived spec; treat enum additions as code changes — extend `STAGE_*` constants in `src/bffi_pipeline/stages/judge.py` and document the new value in the relevant active plan).
- `bffi:adminMetadata` linking property: `http://urn.fi/URN:NBN:fi:schema:bffi:adminMetadata` (`owl:equivalentProperty` of `bf:adminMetadata`). Every canonical `bffi:Work` and `bffi:Expression` carries one `bffi:adminMetadata` triple to a `bffi:AdminMetadata` block summarising administrative state. The AdminMetadata view is layered alongside the PROV-O graph (not a replacement); see archived spec § 8.
- Sentinel agent URI: `http://urn.fi/URN:NBN:fi:bib:agent:unknown` (P-41 Phase B B3 — anonymous-by-convention salvage). One shared URI for all records the M2 creator-salvage tier resolves through B3; carries `bffi:syntheticSentinel "true"^^xsd:boolean`. Excluded from M5 embedding agent-name vectors, M6 LLM judge pair escalation, M8 union-find keying, and M9 KANTO/Finto reconciliation — multiple records sharing this URI is the property "no known author," not the claim of being by the same person. Defined at `bffi_pipeline.provenance.vocab.SENTINEL_AGENT_UNKNOWN`.
- Authority priority: KANTO → VIAF (persons / corporate bodies); YSO (subjects); KAUNO (fiction genre/form); MUSO (music).
- Display language priority for `skos:prefLabel`: `fi`, `sv`, `en`.
- Documentation language: English throughout.

## Conventions

- **URIs:** All minted via `src/bffi_pipeline/uris.py`. Never concatenate URI strings elsewhere. Deterministic SHA-1 of canonical inputs; UUIDs only for `prov:Activity` records.
- **BFFI namespace discipline:** The `bffi:` namespace (`http://urn.fi/URN:NBN:fi:schema:bffi:`) is **closed**. We may only emit classes and properties that exist in `docs/lkd.rdf`. When we need something not in `lkd.rdf`, pick in this order:
    1. **Reuse an existing standard term.** RDF (`rdf:Statement` reification), RDFS, OWL, SKOS, PROV-O (`prov:hadPrimarySource`, `prov:wasGeneratedBy`, etc.), BIBFRAME (`bf:*` — also closed; consult its ontology), DC Terms. Prefer this path.
    2. **Use the `bffi-prov:` namespace** (`http://urn.fi/URN:NBN:fi:schema:bffi-prov#`) for *pipeline-internal* metadata — Activity classes, decision audit predicates, synthetic-sentinel flags. This namespace is ours; extending it is fine.
    3. **Propose adding the term to BFFI through NLF.** Open a proposal in `docs/plans/proposed/` (precedent: P-49 Layer 3). Until ratified, do not emit it under `bffi:`.

  Test `tests/unit/test_bffi_namespace_discipline.py` enforces this: scans `src/bffi_pipeline/provenance/vocab.py` and `sparql/*.rq` for every `bffi:<name>` reference and asserts each one appears as `rdf:about="http://urn.fi/URN:NBN:fi:schema:bffi:<name>"` in `docs/lkd.rdf`. No allowlists; no exceptions. CI breaks on any local mint.
- **Prompts:** All in `prompts/` as versioned files. Hashed at runtime; hash logged to provenance. Never inline in Python code.
- **SPARQL:** All in `sparql/` as versioned files. Read at startup; parametrize with Jinja2 if needed (autoescape off).
- **Idempotency:** Every stage has deterministic outputs and writes atomically (`.tmp` then rename). Re-runs skip when output is newer than input unless `--force`.
- **Stage isolation:** Stages in `src/bffi_pipeline/stages/` don't import each other. Orchestration lives in `cli.py`.
- **Errors over silent fallbacks:** If reconciliation fails, raise. If Fuseki is unreachable, raise. The only retry logic is in the LLM judge for transient API errors.
- **Provenance is mandatory:** Every merge/reconciliation decision (including negative) writes to the provenance graph before returning. No "optional logging" flag.
- **Type strictness:** `mypy --strict` on all of `src/`. Pydantic v2 for cross-module data. `dataclass(frozen=True)` for internal value objects.
- **Tests against fixtures, not network:** Unit tests never hit the API or Fuseki. Integration tests are tagged; LLM-dependent tests carry an additional `requires_llm` mark and are excluded from CI.

## Workflow rules

- Before starting work on a plan, read it through and run its `git diff <plan-base>..HEAD -- <relevant paths>` drift check. If you're not working off a plan, check `docs/plans/` (all sub-folders, including `proposed/`) first to see whether a plan or proposal already covers the work.
- `make lint && make test` must pass before any commit.
- LLM eval (`make eval`) runs locally on the M5 Max — never in CI. Output is pasted into the PR description if the PR touches `prompts/`, `gold/`, or `src/bffi_pipeline/stages/judge.py`.
- Commit messages tag the relevant pipeline stage or plan phase, e.g. `M3: BIBFRAME → BFFI conversion` for stage-scoped work (M3 = pipeline stage `bf-to-bffi`; the M-prefix is the live stage ID, not a `BUILD_PLAN` milestone reference) or `P-04 Phase A: lock embedding model` for plan execution.
- When a plan phase completes, fill in its `Phase commit` field in the plan document with the merge commit hash. The archived `docs/archived/BUILD_PLAN.md` checklist is frozen and is not updated as new work ships.

## What not to do

- Don't write a generic "MARC to anything" framework. This is a BFFI pipeline.
- Don't introduce a workflow engine (Airflow, Prefect, Dagster). The Makefile + typer CLI is the orchestration.
- Don't reach for async unless a stage genuinely benefits.
- Don't modify `third_party/marc2bibframe2/` (git submodule). Wrap, don't fork.
- Don't mint local `bffi:` terms. The namespace is closed to what `docs/lkd.rdf` declares — see the **BFFI namespace discipline** rule in Conventions for the legitimate alternatives (standard W3C / PROV-O / BIBFRAME terms; `bffi-prov:` for pipeline metadata; NLF proposal for genuine ontology gaps).
- Don't merge silent failures into provenance. Log `uncertain` with the actual error.
- Don't add features that aren't covered by an active plan in `docs/plans/`. Surface new directions as a proposal in `docs/plans/proposed/` first; only graduate into a plan under `docs/plans/backlog/` after the trade-off is on the record.
