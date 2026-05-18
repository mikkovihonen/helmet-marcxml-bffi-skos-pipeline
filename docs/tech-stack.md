# Tech stack

The end-to-end inventory of what the pipeline runs on and why each
piece was picked. Use this when onboarding a new contributor or
handing off to the NLF maintenance team — every entry links into the
deeper doc that owns the topic.

## At a glance

```
                                            ┌────────────────────┐
   Sierra Postgres replica ────marcxml-export-sierra──► MARCXML ──► M2 marc2bibframe2 XSLT ─► BIBFRAME RDF/XML
                                            │
                                            ▼
                                       M3 SPARQL CONSTRUCT (rdflib + jinja2)
                                            │
                                            ▼
                       BFFI Turtle  ──►  M4 blocking ──► M5 BGE-M3 + FAISS HNSW ──► candidate pairs
                                                                                       │
                                                                                       ▼
                                                                       M6 Qwen3 cascade judge (mlx-lm)
                                                                                       │
                                                                                       ▼
                                                                       M8 union-find merge ──► canonical Works
                                                                                       │
                                                                                       ▼
                                              M9 reconcile against Finto graphs in Fuseki + Finto API + VIAF
                                                                                       │
                                                                                       ▼
                                                                M10 Skosify overlay + Fuseki PUT/POST
                                                                                       │
                                                                                       ▼
                                                                M11 Skosmos v3.2 (Apache + PHP) UI on :9090
                                                                                       │
                                                                                       ▼
                                                                       M12 gold-set eval (make eval)
```

## Languages, build, lint

| Component | Pin | Role |
|---|---|---|
| Python | 3.14 (pinned in `pyproject.toml` + `.python-version`, enforced via `uv`) | One runtime for the whole pipeline. |
| [uv](https://github.com/astral-sh/uv) | Latest | Dep resolver + virtualenv manager. Everything pinned in `uv.lock`. |
| ruff | ≥ 0.5 | Lint (`make lint`) + format (`make format`). |
| mypy | ≥ 1.10, `--strict` | Type-checks every line of `src/`. |
| pytest | ≥ 8.2 (+ `pytest-asyncio`) | 704 unit tests; integration tests tagged `requires_llm` are excluded from CI. |
| Makefile | n/a | Orchestration entry points (`make lint test eval publish refresh-finto`). |

CI runs `make lint && make test` + the integration job (Fuseki service container) on GitHub-hosted Ubuntu. The LLM eval is **never in CI** — operator runs it on the M5 Max before relevant PRs ([`ci-strategy.md`](ci-strategy.md)).

## Data formats

| Layer | Format | Tooling |
|---|---|---|
| Source | MARCXML 21 (one record per file, named `<bib_id>.xml`) | `pymarc ≥ 5.1`, `lxml ≥ 5.2` (XSLT runtime) |
| M2 output | BIBFRAME RDF/XML | `third_party/marc2bibframe2` (git submodule, NLF/LoC XSLT) |
| M3 output | BFFI Turtle (one Work + Expression(s) per file) | `rdflib ≥ 7.0`, Jinja2 SPARQL templates from `sparql/` |
| M5 artefacts | JSONL (`embed-candidates.jsonl`), FAISS index, idmap.json | `sentence-transformers`, `faiss-cpu` |
| M6 artefacts | JSONL decisions, SQLite cache, JSONL watchdog events | Plain stdlib + Pydantic schemas |
| M8 output | Turtle (canonical.ttl) + JSONL helmet-map / canonical-map | rdflib |
| M9 outputs | Turtle + reconciliation summary JSONL | rdflib |
| M10 publish | SKOS Turtle (`canonical-skosified.ttl`) | `skosify ≥ 2.3` |
| M11 UI | SKOS as served by Skosmos via Fuseki | Skosmos v3.2 + PHP |

The transition contract between each pair of stages is one of the five Boundary validators in [`validation-strategy.md`](validation-strategy.md).

## RDF + SPARQL tooling

| Piece | Version | Use |
|---|---|---|
| `rdflib` | ≥ 7.0 | Parse / serialise / traverse RDF graphs across M2-M10. |
| `pyshacl` | ≥ 0.26 | SHACL validation at every boundary; shapes live under `config/shapes/`. |
| `skosify` | ≥ 2.3 | M10 overlay (lifts BFFI `hasExpression` into SKOS `narrower`, etc.). |
| `SPARQLWrapper` | ≥ 2.0 | M9 Finto endpoint queries. |
| Apache Jena Fuseki | **5.4.0** | Triple store. Pinned to the JAR version Skosmos's vendored Dockerfile downloads. Configured for named-graph mode (`<urn.fi/...graph:...>` prefixes). |
| SPARQL files | `sparql/` | All queries on disk, hashed at startup, parametrised with Jinja2 (autoescape off). Never inlined in Python. |

## Inference / ML

| Component | Choice | Documented in |
|---|---|---|
| **LLM serving** | **mlx-lm** (Apple, `ml-explore/mlx-lm`, PyPI `mlx-lm`) — the only supported backend after P-02 § D6. | [`local-inference.md`](local-inference.md) |
| **LLM primary** | `Qwen3-8B-4bit` (mlx-lm; `Qwen/Qwen3-8B-MLX-4bit` on HF) | Same |
| **LLM fallback** | `Qwen3-32B-4bit` (mlx-lm; `mlx-community/Qwen3-32B-4bit` on HF). The older spec referenced `qwen2.5:72b-instruct-q4_K_M` but the dev machine doesn't fit the 72B — see `~/.claude/projects/.../memory/dev_machine_constraints.md` | Same |
| **LLM draft model (P-02 Phase C — abandoned)** | `Qwen3-1.7B-4bit` — kept on disk but not used in production; spec-decode regressed throughput on M2 Max (see [`plans/completed/p-02-inference-stack-tuning.md`](plans/completed/p-02-inference-stack-tuning.md) § C5) | [`plans/completed/p-02-inference-stack-tuning.md`](plans/completed/p-02-inference-stack-tuning.md) |
| **LLM client** | `langchain-openai ≥ 0.1` (OpenAI-compatible HTTP) | [`local-inference.md`](local-inference.md) |
| **Embedding model** | `BAAI/bge-m3` (1024-dim, multilingual). Benchmark to lock in vs e5-large / jina-v3 is [P-04](plans/backlog/p-04-m5-calibration.md). | [`plans/backlog/p-04-m5-calibration.md`](plans/backlog/p-04-m5-calibration.md) |
| **Embedding runtime** | `sentence-transformers ≥ 3.0` on PyTorch MPS (Apple Silicon GPU) | Same |
| **ANN index** | FAISS HNSW: `M=32 efConstruction=200 efSearch=64`, IP metric on L2-normalised vectors | M5 stage docstring + [`p-04`](plans/backlog/p-04-m5-calibration.md) Phase B |
| **Language detection (M3 title cascade)** | [Lingua](https://github.com/pemistahl/lingua-py) `≥ 2.0` + Qwen3 cascade for parallel-title disambiguation | `src/bffi_pipeline/title_lang.py`, `title_lang_llm.py` |
| **LLM watchdog** | Per-call + per-pair wall-time ceilings via `LLM_CALL_TIMEOUT_SECONDS` / `LLM_PAIR_TIMEOUT_SECONDS`. Plan-of-record [P-03](plans/completed/p-03-m6-stall-watchdog.md). | Plan |
| **Prompt management** | Versioned text files in `prompts/`, hashed at startup, hash logged to provenance. No inline prompts in code. | Spec § 7, CLAUDE.md |

**Stack decision recorded**: P-02 § A1 documents the trade-off table for mlx-lm vs the higher-level `waybarrios/vllm-mlx` wrapper. mlx-lm chosen for Apple-team maintenance + smaller transitive-dep footprint over the multi-year NLF horizon.

## Source ingestion

| Component | Pin | Role |
|---|---|---|
| `pymarc` | ≥ 5.1 | MARCXML field-level parsing inside `marcxml_export_pipeline.sierra`. |
| `SQLAlchemy[asyncio]` | ≥ 2.0 | Streaming SELECT against Sierra's Postgres replica. |
| `asyncpg` | ≥ 0.29 | Underlying async Postgres driver. |
| `marcxml-export-sierra` CLI | This repo | Synthesises MARC 001/003/005/907 when source rows lack them — the contract M2 needs to keep work-key minting stable. Smoke → validate → full sequence documented in `docs/runbook.md` § "Sierra export". |

Architectural decision recorded in commit `e194e6d`: the exporter ships as a sibling Python package `marcxml_export_pipeline.sierra` (not nested under `bffi_pipeline`) so future ILS sources (Koha, Alma, …) can grow without entangling the BFFI conversion code.

## Authority vocabularies (Finto)

Loaded into Fuseki by `bffi-pipeline load-finto`. Each vocab lives in its own named graph; Skosmos's `config/skosmos-config.ttl` points at the same graph URIs.

| Vocab | Use | Graph |
|---|---|---|
| KANTO / finaf | Persons, corporate bodies (primary creator reconciliation) | `http://urn.fi/URN:NBN:fi:au:finaf:` |
| YSO | Subjects (general) | `http://www.yso.fi/onto/yso/` |
| YSO-aika | Time-period subjects (M3 routes 650 time-terms → 648) | (same YSO graph; identified via `skos:inScheme <…/aika>`) |
| YSO-paikat | Place subjects | (same YSO graph) |
| KAUNO | Fiction genre/form | `http://www.yso.fi/onto/kauno/` |
| MUSO | Music genre/form | `http://www.yso.fi/onto/muso/` |
| SLM | Subject category | `http://urn.fi/URN:NBN:fi:au:slm:` |
| ALLARS | Subject (Swedish-language alternate to YSO) | `http://www.yso.fi/onto/allars/` |
| KAUNOKKI | Fiction subjects (Finnish) | `http://urn.fi/URN:NBN:fi:au:kaunokki:` |
| LCSH | Subjects (LoC) | `http://id.loc.gov/authorities/subjects/` |
| LCGFT | Genre/form (LoC) | `http://id.loc.gov/authorities/genreForms/` |
| childrensSubjects | LoC children's subjects | `http://id.loc.gov/authorities/childrensSubjects/` |
| relators | LoC role codes | `http://id.loc.gov/vocabulary/relators/` |

Priority chain for reconciliation: **KANTO → VIAF** (persons / corporate bodies); **YSO** (subjects); **KAUNO** (fiction genre/form); **MUSO** (music). Display-language priority for `skos:prefLabel`: `fi`, `sv`, `en`.

The full BFFI 1.0.0 ontology is vendored at [`docs/lkd.rdf`](lkd.rdf) (RDF/XML, ~4600 lines) — `https://schema.finto.fi/bffi/` returns HTTP 403 outside the Finto network, so the local copy is the canonical reference.

## Containerised services

| Service | Image / source | Port | Purpose |
|---|---|---|---|
| Apache Jena Fuseki | 5.4.0 (built into the Skosmos compose) | 3030 | Triple store; dataset name `bffi`. SPARQL endpoint at `/bffi/query`, Graph Store Protocol at `/bffi/data`. |
| Skosmos | NatLibFi `v3.2` (git submodule under `third_party/Skosmos`; built locally — no published Docker image) | 9090 | Per-vocab browsing UI in `fi`/`sv`/`en`. |

Compose file: [`docker-compose.yml`](../docker-compose.yml). Both containers build from source via `docker compose build` (~5-10 min one-off) before `docker compose up -d`.

## CLI + entry points

| Command | Source module | Purpose |
|---|---|---|
| `bffi-pipeline` (typer) | `src/bffi_pipeline/cli.py` | Every M-stage CLI: `marc-to-bf`, `bf-to-bffi`, `embed`, `judge`, `merge`, `reconcile`, `skosify`, `load`, `eval`, `grow-gold`, `load-finto`, `embed-benchmark`, `ysa-disambiguation-report`, … |
| `marcxml-export-sierra` (argparse) | `src/marcxml_export_pipeline/sierra/marcxml.py` | Sierra → MARCXML export, single-CLI sibling package. |
| Operator scripts | `scripts/` | `start-mlx-lm.sh` (mlx-lm server launcher), `select-run-sample.py` (stratified MARCXML sub-sampler), `test-runs-lifecycle.sh` (P-32 runs CLI smoke driver), `audit-merge-clusters.py` (M5 / M6 same-Work cluster triage). Pipeline stages run directly via `bffi-pipeline <subcommand>` — no shell driver. |

Heavy LLM / ML imports (sentence-transformers, faiss, mlx-lm) are deferred to function bodies so the CLI's `--help` stays fast.

## Persistence + observability

| Surface | Format | Notes |
|---|---|---|
| Stage outputs | Files under `BFFI_DATA_DIR` (default `./data/`) | Idempotent: every stage skips when output is newer than input unless `--force`. |
| M5 / M8 / M9 maps | JSONL | `helmet-map.jsonl`, `embed-candidates.jsonl`, `canonical-map.jsonl`, `reconciliation-summary.jsonl`, `watchdog-events.jsonl`, … |
| M6 cache | SQLite (`judge-cache.sqlite`) | Keyed on `(model, prompt_hash, record_a_canonical, record_b_canonical)`. Writes happen only after both structural and semantic (Boundary-4) validation pass. |
| Provenance | Turtle (`provenance.ttl`) | PROV-O + bffi-prov; every merge / reconciliation decision (positive *and* negative) lands here. Compacted via `bffi-pipeline provenance compact --older-than 90d`. Spec § 8. |
| Pipeline log | Plain text (`pipeline.log`) | Stage-transition markers (`STAGE_*_START`, `STAGE_*_DONE Ns`, `PIPELINE_DONE`) for `tail -f` monitoring. P-03 watchdog events stream here with prefix `WATCHDOG_EVENT `. |
| Eval runs | JSON (`eval-runs/<label>.json`) | Aggregate metrics + failure list per gold-set evaluation. |

## Gold set + evaluation

| Piece | Location | Role |
|---|---|---|
| Gold pairs | `gold/gold.jsonl` | 17 cataloguer-vetted same/different pairs across `GoldCategory` literal values. Bootstrap; [P-06](plans/backlog/p-06-gold-set-growth.md) grows to 50-100 with per-category min-2 holdout. |
| Gold loader | `src/bffi_pipeline/eval/gold_set.py` | Pydantic-strict load + holdout split + stratification assertion. |
| Eval harness | `src/bffi_pipeline/eval/harness.py` | Walks gold cases through an injectable judge; reports per-category accuracy + confusion matrix + median latency. CLI: `bffi-pipeline eval --run-label <id>`. |
| Growth tool | `src/bffi_pipeline/eval/grow.py` | Reads cataloguer-overridden M6 decisions from Fuseki and emits JSONL candidates for cataloguer hand-merge. CLI: `bffi-pipeline grow-gold`. |

PR template prompts for the eval block whenever a PR touches `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/`.

## Project-process tooling

| Artefact | Purpose |
|---|---|
| `docs/plans/proposed/` (`prop-<NN>-<slug>.md`) | Forward-looking sketches not yet committed to. Each carries `Status: proposed | rejected (reason)` + `Proposal-base commit`. Graduated proposals are deleted from this folder; the resulting plan under `backlog/` (or further) becomes canonical. |
| `docs/plans/` (`p-<NN>-<slug>.md`) | Committed-to-action plans of record. State encoded via sub-folders (`proposed/` → `backlog/` → `in-progress/` → `completed/` / `abandoned/`). Each carries `Plan-base commit` + per-phase commit hashes filled in as work ships. |
| `docs/archived/` | Superseded docs kept for reference (`BUILD_PLAN.md`, the earlier `local-inference.md`, …). |
| Auto-memory | `~/.claude/projects/.../memory/` | Project-specific operational notes (dev-machine LLM constraints, corpus location, curated dev sample, …). |

## Operating constraints

- **Pro bono for the National Library of Finland.** Target hand-off audience: NLF maintenance team.
- **No paid LLM APIs.** All inference local on Apple Silicon.
- **Open-source tooling only.**
- **License**: code **Apache 2.0** (matching NLF tools); published RDF data **CC0** (matching Finto vocabularies).
- **No telemetry / error reporting** — provenance is in-graph (`bffi-prov:`), not in any external collector.
- **Type strictness**: `mypy --strict` over all of `src/`.
- **Production target**: MacBook Pro M5 Max with 128 GB unified memory. The dev machine (smaller) carries an `~/.claude/.../memory/dev_machine_constraints.md` override (qwen2.5:72b cascade fallback doesn't fit).

## Where to go next

- New to the project? Read [`runbook.md`](runbook.md) first — that's the canonical end-to-end recipe.
- Setting up the LLM stack? [`local-inference.md`](local-inference.md).
- Adding a new validation step? [`validation-strategy.md`](validation-strategy.md).
- Adding a new pipeline stage or significant refactor? Surface as a proposal under [`docs/plans/proposed/`](plans/proposed/) first; graduate to a plan under [`docs/plans/backlog/`](plans/backlog/) once committed.
