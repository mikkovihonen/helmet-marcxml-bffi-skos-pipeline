# CLAUDE.md

BFFI pipeline: MARCXML → BFFI authority Works/Expressions → Skosmos. Pro bono; will be contributed to the National Library of Finland. Target corpus: ~800,000 Helmet bibliographic records.

## Project specs and plans

- `docs/tech-stack.md` — consolidated toolchain reference (languages, RDF tooling, inference stack, vocabularies, services). Start here for "what does this project use for X?".
- `docs/validation-strategy.md` — five validation boundaries (MARCXML → BIBFRAME → BFFI → judge → post-load).
- `docs/local-inference.md` — Apple Silicon / mlx-lm setup, model choice, throughput, cascade strategy.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` — original end-to-end technical specification (archived). Section-level back-references from older commits, plans, and source comments still point here; live successors are listed at the top of that document.
- `docs/external-dependencies.md` — records and confirmations to request from Helmet cataloguers.
- `docs/ci-strategy.md` — CI rationale and PR template.
- `docs/lkd.rdf` — full BFFI 1.0.0 ontology (RDF/XML, ~4600 lines), vendored because `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network. **The canonical reference for class and property definitions; consult before adding any `bffi:*` term to spec, code, or shapes.**
- `docs/proposals/` — forward-looking ideas not yet committed to (`prop-<NN>-<slug>.md`). Each carries `Status: proposed | planning (graduated) | done | rejected (reason)` and a `Proposal-base commit` for drift detection. **Skim the proposals README and the current proposals index before recommending an architectural change** — the idea may already be on record, possibly with a documented reason not to pursue it.
- `docs/plans/` — committed-to-action plans of record (`p-<NN>-<slug>.md`). Each plan has sequenced phases with verification checkpoints, a risk register, and a rollback procedure, plus a `Plan-base commit` and `Phase commits` for tying execution to git history. **State is encoded by sub-folder**: `backlog/` (drafted, not started), `in-progress/` (at least one phase shipped), `completed/` (done), `abandoned/` (dropped, with reason). State transitions happen via `git mv` in the same commit as the corresponding phase commit. Plans graduate from proposals; **consult before re-scoping work that overlaps an active plan**.
- `docs/archived/` — historical / superseded documents kept for reference only. Includes `BUILD_PLAN.md` (milestone-ordered checklist M0-M13; the live execution detail has moved to `docs/plans/`) and `marcxml-to-bffi-skosmos-pipeline.md` (original technical spec; live successors are listed in the document's archived banner). Path references from source code or live docs may point here; do not edit archived material except for typos or to add a supersede pointer.

## Operating constraints

- Pro bono. **No paid API services.** All LLM inference runs locally on Apple Silicon (target: MacBook Pro M5 Max, 128 GB).
- Open-source tooling only.
- License: code **Apache 2.0** (matching NLF tools); published RDF data **CC0** (matching Finto vocabularies).
- No **outbound** telemetry / error reporting — i.e. no Datadog, Sentry, Honeycomb, or any other monitoring service that sends pipeline data to a third party. Local-only observability is fine: running Prometheus + Grafana in a container next to the existing Fuseki + Skosmos services (so the operator can `localhost:3001` a dashboard) does not violate this constraint because no data leaves the operator's machine. See `docs/proposals/prop-11-structured-observability.md` for the planned local stack.

## Committed identifiers (do not change without surfacing)

- Work URI namespace: `http://urn.fi/URN:NBN:fi:bib:work:`
- Expression URI namespace: `http://urn.fi/URN:NBN:fi:bib:expression:`
- Helmet source URI (used in `bf:identifiedBy`): `http://urn.fi/URN:NBN:fi:bib:source:helmet`
- Named-graph base for Fuseki: `http://urn.fi/URN:NBN:fi:bib:graph:`
- `bffi-prov` namespace: `http://urn.fi/URN:NBN:fi:schema:bffi-prov#` (provenance vocabulary — Activity classes, decision/confidence/rationale predicates, stage tags). Full `bffi-prov:stage` enum and Activity class list live in `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 (archived spec; treat enum additions as code changes — extend `STAGE_*` constants in `src/bffi_pipeline/stages/judge.py` and document the new value in the relevant active plan).
- `bffi:adminMetadata` linking property: `http://urn.fi/URN:NBN:fi:schema:bffi:adminMetadata` (`owl:equivalentProperty` of `bf:adminMetadata`). Every canonical `bffi:Work` and `bffi:Expression` carries one `bffi:adminMetadata` triple to a `bffi:AdminMetadata` block summarising administrative state. The AdminMetadata view is layered alongside the PROV-O graph (not a replacement); see archived spec § 8.
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

- Before starting work on a plan, read it through and run its `git diff <plan-base>..HEAD -- <relevant paths>` drift check. If you're not working off a plan, check `docs/plans/` and `docs/proposals/` first to see whether a plan or proposal already covers the work.
- `make lint && make test` must pass before any commit.
- LLM eval (`make eval`) runs locally on the M5 Max — never in CI. Output is pasted into the PR description if the PR touches `prompts/`, `gold/`, or `src/bffi_pipeline/stages/judge.py`.
- Commit messages tag the relevant milestone or plan phase, e.g. `M3: BIBFRAME → BFFI conversion` for milestone work or `P-04 Phase A: lock embedding model` for plan execution.
- When a plan phase completes, fill in its `Phase commit` field in the plan document with the merge commit hash. Don't update the historical milestone checkboxes in `docs/archived/BUILD_PLAN.md`.

## What not to do

- Don't write a generic "MARC to anything" framework. This is a BFFI pipeline.
- Don't introduce a workflow engine (Airflow, Prefect, Dagster). The Makefile + typer CLI is the orchestration.
- Don't reach for async unless a stage genuinely benefits.
- Don't modify `third_party/marc2bibframe2/` (git submodule). Wrap, don't fork.
- Don't merge silent failures into provenance. Log `uncertain` with the actual error.
- Don't add features that aren't covered by an active plan in `docs/plans/`. Surface new directions as a proposal in `docs/proposals/` first; only graduate into a plan after the trade-off is on the record.
