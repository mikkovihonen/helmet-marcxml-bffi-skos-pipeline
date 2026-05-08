# Build Plan — BFFI Pipeline

> **Read CLAUDE.md first.** This file is the milestone-ordered build plan. Each milestone produces something runnable and testable. Don't move to milestone N+1 until N is green.

For technical detail on what each stage produces, see `docs/marcxml-to-bffi-skosmos-pipeline.md` (the spec). For validation expectations, see `docs/validation-strategy.md`. For local LLM setup, see `docs/local-inference.md`. For the records you'll need to request from Helmet, see `docs/external-dependencies.md`.

## Tech stack

These are not up for renegotiation unless a milestone reveals a blocker. If you hit one, stop and surface it.

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Type hints, structural pattern matching, native asyncio maturity. |
| Package manager | `uv` | Fast, lockfile-based, replaces pip + venv + pip-tools. |
| RDF | `rdflib` 7.x | The standard. SPARQL CONSTRUCT, Turtle parsing, namespace handling. |
| MARC | `pymarc` for inspection + LoC `marc2bibframe2` XSLT for conversion | Don't reimplement the XSLT. |
| XSLT | `lxml` (libxslt-based) | XSLT 1.0 support; matches what `marc2bibframe2` was tested against. |
| LLM judge | `langchain-openai` (pointed at local server) + Pydantic v2 | `with_structured_output` gives the schema constraint. |
| Local LLM server | Ollama (default) or vllm-mlx (production batch) | Both expose OpenAI `/v1/chat/completions`. |
| Inference framework | MLX (Apple's native Metal-optimized framework) | 20–50% faster than llama.cpp on Apple Silicon. |
| Judge model (primary) | Qwen3 32B Instruct, MLX 4-bit | ~18–20 GB resident; 50–80 tok/s on M5 Max. |
| Judge model (cascade) | Qwen3 72B Instruct, MLX 4-bit | ~40 GB resident; 25–40 tok/s; for borderline cases. |
| Embeddings | `sentence-transformers` with `BAAI/bge-m3` (default; benchmark winner in M5) | Multilingual; benchmarked in M5. |
| Vector index | FAISS `IndexHNSWFlat` (in-process) | ~5 GB for 800k Works; sub-ms queries. |
| IDs | `python-ulid` | Time-ordered, sortable. |
| CLI | `typer` | Type-driven argument parsing. |
| Testing | `pytest` + `pytest-asyncio` | |
| Triple store | Apache Jena Fuseki 5.x (Docker, pinned) | `jena-text` for label search. |
| UI | Skosmos 3.x (Docker, pinned) | Reads from Fuseki. |
| Linting/format | `ruff` (lint + format) | One tool, fast. |
| Type checking | `mypy --strict` | On all `src/`. |

## Repository layout

```
bffi-pipeline/
├── CLAUDE.md                          # short conventions file
├── README.md
├── LICENSE                            # Apache License 2.0
├── pyproject.toml                     # uv-managed
├── uv.lock
├── Makefile
├── docker-compose.yml                 # Fuseki 5.x + Skosmos 3.x (pinned)
├── .env.example
├── .gitignore                         # data/, logs/, eval-runs/, .env
│
├── docs/                              # this file's siblings
│   ├── BUILD_PLAN.md
│   ├── marcxml-to-bffi-skosmos-pipeline.md # spec
│   ├── validation-strategy.md
│   ├── local-inference.md
│   ├── external-dependencies.md
│   ├── ci-strategy.md
│   └── runbook.md                     # produced as M13 deliverable
│
├── config/
│   ├── bffi.cfg                       # Skosify config
│   ├── skosmos-config.ttl
│   ├── overlay/
│   │   └── bffi-skos-overlay.ttl      # RDFS subclass declarations + Helmet source URI
│   └── shapes/
│       ├── bibframe-conversion.shape.ttl  # SHACL: Boundary 2
│       ├── bffi.shape.ttl                 # SHACL: Boundary 3
│       └── post-load-smoke.rq             # SPARQL ASK: Boundary 5
│
├── sparql/
│   ├── bf_to_bffi_work.rq             # Pass 1 CONSTRUCT
│   ├── bf_to_bffi_expression.rq       # Pass 2 CONSTRUCT
│   └── queries/                       # Read-side queries
│       ├── review_queue.rq
│       ├── derivation_chain.rq
│       ├── overridden_decisions.rq
│       └── helmet_lookup.rq
│
├── prompts/
│   └── judge_v1.txt                   # versioned, hashed at runtime
│
├── src/bffi_pipeline/
│   ├── __init__.py
│   ├── cli.py                         # typer entry point
│   ├── config.py                      # Pydantic settings
│   ├── uris.py                        # deterministic URI minting
│   ├── schemas/
│   │   └── MARC21slim.xsd             # vendored from LoC
│   ├── validation/
│   │   ├── marcxml.py                 # Boundary 1
│   │   ├── bibframe.py                # Boundary 2
│   │   ├── bffi.py                    # Boundary 3
│   │   ├── decisions.py               # Boundary 4
│   │   └── post_load.py               # Boundary 5
│   ├── stages/
│   │   ├── preprocess.py
│   │   ├── marc_to_bf.py
│   │   ├── bf_to_bffi.py
│   │   ├── workkey.py
│   │   ├── embeddings.py
│   │   ├── judge.py
│   │   ├── reconcile.py
│   │   ├── merge.py
│   │   ├── skosify_run.py
│   │   └── load.py
│   ├── provenance/
│   │   ├── vocab.py
│   │   └── logger.py
│   └── eval/
│       ├── harness.py
│       ├── gold_set.py
│       └── grow.py
│
├── tests/
│   ├── conftest.py
│   ├── data/
│   │   ├── sample-marcxml/            # 15 curated Helmet records (Ask 1)
│   │   └── expected/                  # golden output for regression
│   ├── unit/
│   ├── integration/
│   └── eval/
│
├── gold/
│   └── gold.jsonl                     # versioned in git; "holdout" field marks eval cases
│
├── data/                              # gitignored; configurable via BFFI_DATA_DIR
├── logs/                              # gitignored; configurable via BFFI_LOGS_DIR
├── eval-runs/                         # gitignored; configurable via BFFI_EVAL_DIR
│
└── third_party/
    └── marc2bibframe2/                # git submodule of lcnetdev/marc2bibframe2
```

## Setup commands

After a fresh clone on an Apple Silicon Mac:

```bash
# One-time setup
git submodule update --init --recursive
uv sync
cp .env.example .env

# Local LLM server (host-native, NOT in Docker — Docker on Apple Silicon doesn't expose Metal/MLX)
brew install ollama
ollama serve &                                # runs on :11434
ollama pull qwen3:32b-instruct-q4_K_M         # primary judge (~18 GB)
ollama pull qwen3:72b-instruct-q4_K_M         # cascade fallback (~40 GB)

# Triple store + UI (Docker is fine for these — no GPU needed)
docker compose up -d                          # starts Fuseki + Skosmos

# Common workflows
make convert FILE=path/to/records.marcxml
make eval                                     # run gold set
make publish                                  # load to Fuseki, refresh Skosmos
make test
make test-integration                         # requires Docker stack + Ollama
make lint
```

`.env.example`:

```
# URI namespaces (do not change without surfacing the decision)
BFFI_WORK_NAMESPACE=http://urn.fi/URN:NBN:fi:bib:work:
BFFI_EXPRESSION_NAMESPACE=http://urn.fi/URN:NBN:fi:bib:expression:
BFFI_HELMET_SOURCE_URI=http://urn.fi/URN:NBN:fi:bib:source:helmet
BFFI_GRAPH_BASE=http://urn.fi/URN:NBN:fi:bib:graph:

# Local LLM
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama                            # placeholder; Ollama ignores it
LLM_MODEL_PRIMARY=qwen3:32b-instruct-q4_K_M
LLM_MODEL_FALLBACK=qwen3:72b-instruct-q4_K_M

# Triple store
FUSEKI_URL=http://localhost:3030/bffi

# Data directories
BFFI_DATA_DIR=./data
BFFI_LOGS_DIR=./logs
BFFI_EVAL_DIR=./eval-runs
```

---

## Milestones

### M0 — Skeleton

- [x] `pyproject.toml` with all dependencies pinned in `uv.lock`. Includes pytest marker declarations:
  ```toml
  [tool.pytest.ini_options]
  markers = [
      "integration: tests that require Docker services (Fuseki/Skosmos)",
      "requires_llm: tests that additionally require a running Ollama instance",
  ]
  ```
- [x] Repo structure as above (empty modules with docstrings are fine).
- [x] `Makefile` with all targets stubbed (`echo "not implemented"`).
- [x] `docker-compose.yml` runs Fuseki on `:3030` (dataset `bffi`) and Skosmos on `:9090` reading from Fuseki.
- [x] `make lint` passes on the empty skeleton.
- [x] `pytest tests/` runs (zero tests is fine) and exits 0.

**Definition of done:** `make test && make lint` is green.

### M1 — URI minting + config

- [x] `src/bffi_pipeline/config.py` with Pydantic Settings reading from `.env` and `config/`.
- [x] `src/bffi_pipeline/uris.py` with `mint_work_uri(creator_uri, original_title)` and `mint_expression_uri(work_uri, language)`. Deterministic SHA-1; covered by unit tests with explicit input → expected output pairs.
- [x] `tests/unit/test_uris.py` covering: stable across runs, sensitive to creator change, sensitive to title normalization, insensitive to whitespace.

**Definition of done:** URI tests pass; running `mint_work_uri` twice with the same args returns the same URI.

### M2 — MARCXML → BIBFRAME

**Input format:** A directory of MARCXML files, **one record per file**, named `<helmet_bib_id>.xml` (numeric ID, e.g. `12345678.xml`). UTF-8 only. Encoding discrepancies are hard errors — surface the offending filename.

- [x] Add `lcnetdev/marc2bibframe2` as a git submodule under `third_party/`.
- [x] `src/bffi_pipeline/stages/marc_to_bf.py` accepts a directory path, iterates `*.xml` files, validates UTF-8 (open with `encoding="utf-8"`, `errors="strict"`), and runs each through the XSLT via `lxml`. Output: one BIBFRAME RDF/XML file per input under `<BFFI_DATA_DIR>/bibframe/<helmet_bib_id>.rdf`.
- [x] **Filename validation:** filenames must match `^\d+\.xml$`. Anything else goes to the error log and is skipped.
- [x] **Helmet identifier emission:** post-process the marc2bibframe2 output to add a `bf:identifiedBy` triple to every minted `bf:Work` and `bf:Instance`, pointing at a `bf:Local` identifier with `rdf:value` = the bare Helmet bib ID and `bf:source` = `<http://urn.fi/URN:NBN:fi:bib:source:helmet>`.
- [x] **Sidecar map:** append one row per converted record to `<BFFI_DATA_DIR>/helmet-map.jsonl`:
  ```jsonl
  {"helmet_bib_id": "12345678", "source_file": "12345678.xml", "raw_work_uri": "...", "raw_instance_uri": "...", "converted_at": "2026-05-08T14:23:11Z", "marc2bibframe2_version": "..."}
  ```
  Operational read model — fast in-process lookup during M5/M6/M9 without round-tripping through SPARQL. Deduplicate on `helmet_bib_id` (last-write-wins).
- [x] **Conversion provenance:** for each successful conversion, emit a `prov:Activity` of type `bffi-prov:MarcConversion` with `prov:used` pointing at the source filename, `bffi-prov:helmetBibId`, and `bffi-prov:converterVersion`. The minted Work and Instance get `prov:wasGeneratedBy` pointing at this Activity.
- [x] **Initial AdminMetadata stamp:** for each minted raw `bf:Work` (and the corresponding `bf:Instance`), emit one `bffi:AdminMetadata` block linked via `bffi:adminMetadata`. Initial fields: `bffi:adminMetadataFor` (back-link), `bffi:descriptionCreationDate` and `bffi:dateGenerated` (both = conversion timestamp), `bffi:descriptionModifier` = `<bib:agent/marc2bibframe2>`, `bffi:generationProcess` = `<bib:gen-process/bffi-pipeline/v<version>>`, `bffi:descriptionConventions` = `<bib:desc-conv/bffi-1.0.0>`, `bffi:descriptionLevel` = `<bib:desc-level/minimum>`, `bffi:encodingLevel` = `<bib:enc-level/auto>`, `bffi:descriptionAuthentication` = `<bib:auth/auto-merged>`, `bffi:recordingSource` = `<bib:recording-source/helmet>`, `bffi:metadataLicensor` = `<bib:metadata-licensor/cc0>`, `bffi:sourceMetadata` = the Helmet record URI for this conversion. Spine link: `prov:wasGeneratedBy` = the `bffi-prov:MarcConversion` Activity URI. Vocabulary instances live in `config/bffi-admin-vocabulary.ttl` (see M10). See spec § 8 "AdminMetadata view".
- [x] **Boundary 1 validation (MARCXML).** Validate each input against `src/bffi_pipeline/schemas/MARC21slim.xsd` using cached `lxml.etree.XMLSchema`, then run a minimum-content check (at least one 1XX/7XX, one 245, one 008, one 336/337/338). Failures are typed (`marcxml-xml-syntax`, `marcxml-xsd-validation`, `marcxml-content-minimum`). Vendor `MARC21slim.xsd` from LoC; pin the version with a comment naming source URL and download date.
- [x] **Boundary 2 validation (BIBFRAME post-conversion).** Validate XSLT output against `config/shapes/bibframe-conversion.shape.ttl` via `pyshacl`. Records failing the shape go to `_errors.jsonl` with `error_type: "bibframe-shape"` and are excluded from downstream stages.
- [x] **Error handling:** all failure types go to `<BFFI_DATA_DIR>/bibframe/_errors.jsonl` with the Helmet bib ID (if extractable), filename, typed error code, and message. Continue with the rest of the corpus. Print a summary at the end.
- [x] Re-run skips files whose output already exists and is newer than the input.
- [ ] `tests/data/sample-marcxml/` directory containing the **~15 curated records from Helmet** (see `docs/external-dependencies.md` — Ask 1) plus three deliberately broken files (one bad-encoding, one XSD-failing, one minimum-content-failing). Filenames use the real `<id>.xml` pattern. **If real records aren't yet available**, M2 may be developed against synthetic MARCXML, but the milestone is not "done" until real records replace the synthetic ones. *(Currently 6 synthetic valid + 3 broken; awaiting Ask-1 records.)*
- [x] Integration test verifying: parseable BIBFRAME with Helmet identifier triples; populated `helmet-map.jsonl`; broken files in `_errors.jsonl` with correct error type; conversion provenance Activities exist for each success.

**Definition of done:** `bffi-pipeline marc-to-bf tests/data/sample-marcxml/` produces parseable BIBFRAME (passing both XSD and BIBFRAME shape) with Helmet identifiers attached, a populated sidecar map, a typed error file for failures, and conversion-provenance Activities.

### M3 — BIBFRAME → BFFI

- [ ] **Cross-check property allocation against `docs/lkd.rdf` before shipping the CONSTRUCT pair.** The vendored ontology (~4600 lines, RDF/XML) is the single source of truth for `bffi:*` predicate names and `rdfs:domain` / `rdfs:range`. `https://schema.finto.fi/bffi/` is the published URL but is currently 403-protected outside the Finto network. Spec § 3 has been verified against `docs/lkd.rdf`; re-verify whenever BFFI publishes a minor revision.
- [ ] `sparql/bf_to_bffi_work.rq` and `sparql/bf_to_bffi_expression.rq` per spec §3.
- [ ] `src/bffi_pipeline/stages/bf_to_bffi.py` runs both CONSTRUCTs against an in-memory rdflib graph and writes Turtle output.
- [ ] **Preserve `bf:identifiedBy` triples through the CONSTRUCT.** Both passes must copy the Helmet identifier from the source `bf:Work` onto the new `bffi:Work` *and* the new `bffi:Expression`. M8 is where multiple raw Works merge; M3 is where identifiers are first attached.
- [ ] Unit tests verify property allocation: translator's `bf:contribution` → `bffi:Expression`, primary creator → `bffi:Work`, language → Expression, originDate → Work.
- [ ] Unit test verifies identifier preservation: every minted `bffi:Work` and `bffi:Expression` carries `bf:identifiedBy` with the correct Helmet bib ID.
- [ ] Tests cover deterministic linking: every `bffi:Expression` has a matching `bffi:Work` via `bffi:expressionOf`.
- [ ] **Boundary 3 validation (BFFI post-CONSTRUCT).** Validate output against `config/shapes/bffi.shape.ttl` via `pyshacl`. Required shapes: every `bffi:Work` has at least one `bffi:hasExpression`; every `bffi:Expression` has exactly one `bffi:expressionOf`; every Work and Expression has a `bf:identifiedBy` with `bf:source = <http://urn.fi/URN:NBN:fi:bib:source:helmet>`; every Work has `skos:prefLabel` in at least one of `fi`/`sv`/`en`; class disjointness; properties allocated to Work-only don't appear on Expression and vice versa.
- [ ] Validation report goes to `<BFFI_DATA_DIR>/bffi/_validation.jsonl`. Failures emit a CLI warning with counts but **do not block** the pipeline — at 800k records, even 0.1% failure is 800 records that need triage, not a halt.
- [ ] Unit tests for the shapes: hand-craft one valid and one deliberately invalid graph per shape and assert each is judged correctly.

**Definition of done:** Sample records produce well-formed BFFI with correct property routing, Helmet identifiers attached, and a clean validation report (or a known/expected failure set documented in the runbook).

### M4 — Work-key blocking (Stage 1)

- [ ] `src/bffi_pipeline/stages/workkey.py` with `compute_blocking_key(work: dict) -> str`. Deterministic, accent-fold + lowercase + strip punctuation, normalized creator surname + first significant title token + content type code.
- [ ] CLI subcommand `bffi-pipeline workkey-stats <bffi.ttl>` reports block size distribution.
- [ ] Unit tests cover: same surname different given names → same block; transliteration variants → same block (use accent folding); accents → ignored.

**Definition of done:** Running on the sample produces blocks that group what should be grouped.

### M5 — Embedding candidates (Stage 2)

**Scale context:** The production corpus is ~800k Works. HNSW gives sub-ms queries with >98% recall at ~5 GB RAM peak. On the M5 Max with MPS-accelerated embedding, building takes ~30–60 minutes. The index's job is cross-block recall (catching transliteration variants and malformed title fields the rule-based blocker missed).

- [ ] **Benchmark embedding models against the gold set before locking in.** Compare `BAAI/bge-m3` (default), `intfloat/multilingual-e5-large`, and `jinaai/jina-embeddings-v3` on the gold-set pairs: for each model, compute cosine similarity on every gold pair and report mean similarity for `same_work` cases vs `different_work` cases (the wider the gap, the better). Pick the winner; document the comparison in a docstring at the top of `embeddings.py`. **If the winner is not BGE-M3, update model env vars in `.env.example` and any vector-dimension assumptions** (BGE-M3 is 1024-dim; e5-large is 1024-dim; jina-v3 is also 1024-dim, but verify and update HNSW config if it changes). This benchmark is itself an M5 sub-task; budget half a day for it.
- [ ] `src/bffi_pipeline/stages/embeddings.py` builds a FAISS `IndexHNSWFlat` over an L2-normalized 1024-dim embedding (BGE-M3 by default; chosen winner from benchmark). Use `metric=METRIC_INNER_PRODUCT`, `M=32`, `efConstruction=200`. Document why these values in a docstring.
- [ ] Embedding input string format: pipe-separated, fixed field order — `"creator: <X> | title: <Y> | language: <Z> | year: <Y> | type: <T>"`. Stable so re-embedding produces identical vectors.
- [ ] Use `sentence-transformers` with the `mps` device (PyTorch's Metal backend). Batches of 64–128 records on M5 Max should saturate the GPU. Report progress every 10k records.
- [ ] Persist the FAISS index to `<output_dir>/embeddings.faiss` and the URI→vector-id mapping to `<output_dir>/embeddings.idmap.json`. Downstream stages must reload, never rebuild. Skip the build step if both files exist and are newer than the input BFFI file.
- [ ] For each Work, query top-k (default `k=20`) neighbours and emit candidate pairs above the low threshold. Apply blocking-key intersection as a post-filter — only keep pairs that share a block — unless `--cross-block` is passed.
- [ ] **Threshold defaults are tightened from the spec** to reduce LLM workload given local-inference throughput constraints: auto-merge ≥0.90, escalate 0.78–0.90, reject ≤0.78. These must be configurable; validate the chosen values against the gold set in M12 before treating them as final.
- [ ] Output: JSONL of candidate pairs with similarity score and both blocking keys. Also emit summary counts: pairs in each band so M6 can plan run time.
- [ ] CLI subcommand `bffi-pipeline embed-stats` reports: index size, build time, top-k similarity distribution, fraction of pairs above each threshold, fraction of cross-block hits.
- [ ] Tune `efSearch` against the gold set: run with `efSearch ∈ {32, 64, 128, 256}` and pick the smallest value that finds all known pairs from the gold set's high-similarity cases. Record the chosen value and recall numbers in a docstring.
- [ ] Unit test (against a small synthetic corpus, not 800k): same creator/title in two languages scores above 0.85; obvious different works score below 0.5.

**Definition of done:** Candidate JSONL contains expected translation/transliteration pairs from the gold set; index file persists and reloads correctly; `embed-stats` runs in seconds; the count of pairs in the "escalate" band is reported and you have a realistic estimate of the M6 run time.

### M6 — LLM judge (Stage 3)

**This is the throughput-bound stage.** Read `docs/local-inference.md` before starting. Plan the production run as a multi-night batch job.

- [ ] `prompts/judge_v1.txt` containing the system prompt + few-shot block from spec §7. **Tune the few-shots specifically for the chosen model** — Qwen3 responds differently to few-shot phrasing than Claude does. Iterate against the gold set.
- [ ] `src/bffi_pipeline/stages/judge.py` with `WorkRecord`, `WorkMatchDecision`, and `judge_pair`. Read prompt from file; hash at startup.
- [ ] Use `langchain-openai` `ChatOpenAI` pointed at `LLM_BASE_URL`. Pass `temperature=0`, `seed=42`. Use `with_structured_output(WorkMatchDecision, method="json_schema")`.
- [ ] **Validate JSON output reliability before mass running.** Open-source models occasionally produce malformed JSON even with schema-constrained generation. Wrap calls with retry-on-parse-error (max 2 retries) and log permanent failures as `decision="uncertain"` with the parse error in the rationale. Don't crash the run on a bad parse.
- [ ] **Boundary 4 validation (semantic post-validators).** Add `@model_validator(mode="after")` to `WorkMatchDecision` enforcing: `decision="uncertain"` requires `confidence ≤ 0.7`; `decision="same_work"` requires non-empty `matching_fields`; rationale ≥ 20 chars and not containing stub phrases (`"i don't know"`, `"unable to determine"`, `"n/a"`). Validation failures share the retry path with JSON parse failures. **Validation-failed responses must not be cached** — caching cements bad outputs across re-runs.
- [ ] SQLite cache for repeated calls keyed on `(model, prompt_hash, record_a_canonical, record_b_canonical)`. Cache writes happen only after both structural and semantic validation pass.
- [ ] Implement the **two-model cascade**: `judge_pair` takes a model name; `cascade_judge` runs primary first, then fallback for `uncertain` or low-confidence `same_work`. Both decisions logged to provenance with distinct stage names.
- [ ] Add a `--server vllm-mlx` mode that uses concurrent requests for the production batch. Ollama mode stays serial.
- [ ] **Tune `--concurrency` for vllm-mlx** as a one-time benchmark sub-task: sweep concurrent request counts in `{4, 8, 16, 32}` against a fixed 1000-pair sample, measure throughput, pick the value that maximizes throughput without OOM. Record the chosen value in the runbook.
- [ ] **Failure recovery via three layered mechanisms:**
  - SQLite cache as primary — keyed on the tuple above, transactional commits per pair.
  - Checkpoint file at `<output_path>.checkpoint` written every 100 pairs recording `{start_time, last_completed_idx, total_pairs, cache_hits, fresh_calls}`. On startup, read it to report "resuming from pair X of Y, ETA …".
  - Inside `judge_pair`, wrap the LLM call with exponential-backoff retry (5s → 30s → 120s) for connection errors. After 3 retries exhaust, log `decision="uncertain"` and continue. **Never crash a multi-day run on a single bad pair.**
- [ ] CLI flags: `--resume` (default, auto-resume from checkpoint+cache), `--restart` (force redo).
- [ ] Progress reporting every 100 pairs with rolling average latency and ETA (e.g., `"12,400 / 50,000 pairs · 4.2s/pair · ETA 43h 28m · 8,200 cache hits"`).
- [ ] Unit tests on schema validation (no LLM calls) — confidence bounds, decision enum, required fields, malformed-JSON retry logic, **and the semantic validators**.
- [ ] Integration test (marked `@pytest.mark.requires_llm`) on five gold-set cases: translation, adaptation, common-title collision, transliteration variant, uncertainty case.

**Definition of done:** Judge produces structured decisions on all gray-zone candidates from M5; cascade works; cache hits produce identical output to fresh calls; gold-set per-category accuracy is reported and acceptable. Production run plan is documented in `docs/runbook.md`.

### M7 — Provenance logging

- [ ] `src/bffi_pipeline/provenance/vocab.py` defines PROV and bffi-prov namespaces, the `WorkMergeDecision` and `HumanReview` classes.
- [ ] `src/bffi_pipeline/provenance/logger.py` with `log_merge_decision` per spec §8 and `log_review` for human overrides.
- [ ] Provenance writes to a separate named graph `<BFFI_GRAPH_BASE>provenance`, with clear graph separation from the published authority data.
- [ ] **CLI subcommand `bffi-pipeline provenance compact --older-than 90d`** removes `bffi-prov:rawResponse` literals from Activity records older than the threshold, keeping the structured fields. Stores the date of last compaction in a small `<BFFI_GRAPH_BASE>provenance-meta` graph as `bffi-prov:lastCompactedAt`.
- [ ] **Stale-provenance warning at CLI startup:** every `bffi-pipeline ...` invocation queries `bffi-prov:lastCompactedAt`; if older than 90 days (or absent), print a warning to stderr. Don't block the command, just nag.
- [ ] Unit test: a mock decision produces triples that round-trip through rdflib and contain all required fields. Compaction unit test verifies that structured fields survive and `rawResponse` is removed for old records.

**Definition of done:** Every judge call writes a complete provenance record. Negative decisions are logged identically. Compaction subcommand works and the staleness warning fires when expected.

### M8 — Merge application

- [ ] `src/bffi_pipeline/stages/merge.py` reads judge decisions, groups Works by transitive `same_work` relation (union-find), mints canonical Work URIs, rewrites all `bffi:expressionOf` relations to point at canonical URIs, retains raw Works as `prov:wasDerivedFrom` targets.
- [ ] **Union the `bf:identifiedBy` sets** when raw Works merge. The canonical Work for "Sota ja rauha" that absorbed 47 raw Helmet records carries 47 `bf:identifiedBy` triples — one per source.
- [ ] **Write `<BFFI_DATA_DIR>/canonical-map.jsonl`:** one row per canonical Work recording the canonical URI, all source raw Work URIs, and all Helmet bib IDs:
  ```jsonl
  {"canonical_work_uri": "...", "raw_work_uris": ["...", "..."], "helmet_bib_ids": ["12345678", "12345679"], "merged_at": "2026-05-08T20:14:00Z"}
  ```
  Joined with `helmet-map.jsonl` from M2, this gives O(1) Helmet ID → canonical Work URI lookup.
- [ ] Idempotent: running merge twice is a no-op. Identifier sets are deduplicated.
- [ ] **Mint canonical-Work AdminMetadata.** Each canonical `bffi:Work` and each `bffi:Expression` produced at merge time gets a fresh `bffi:AdminMetadata` block via `bffi:adminMetadata`. Fields: `bffi:adminMetadataFor` (back-link), `bffi:descriptionCreationDate` = earliest absorbed M2 timestamp (from `helmet-map.jsonl`), `bffi:descriptionChangeDate` and `bffi:dateGenerated` = merge timestamp, `bffi:descriptionModifier` = the cascade winner (e.g., `<bib:agent/qwen3-32b-instruct>` or `<bib:agent/qwen3-72b-instruct>`), `bffi:descriptionAuthentication` = `<bib:auth/auto-merged>`, `bffi:descriptionLevel` = `<bib:desc-level/minimum>`, `bffi:encodingLevel` = `<bib:enc-level/auto>`, `bffi:generationProcess` = `<bib:gen-process/bffi-pipeline/v<version>>`, `bffi:descriptionConventions` = `<bib:desc-conv/bffi-1.0.0>`, `bffi:metadataLicensor` = `<bib:metadata-licensor/cc0>`, `bffi:recordingSource` = `<bib:recording-source/helmet>`, `bffi:sourceMetadata` = the union of absorbed Helmet record URIs (matching `canonical-map.jsonl`). Spine link: `prov:wasGeneratedBy` = the latest `bffi-prov:WorkMergeDecision` Activity URI for this Work. See spec § 8 "AdminMetadata view".
- [ ] Unit tests cover: chain merging (A=B, B=C → A=B=C); conflict handling (A=B, A≠C, B=C — flag for review, don't merge silently); identifier accumulation; idempotency; AdminMetadata block presence and `sourceMetadata` count = absorbed-record count.

**Definition of done:** Sample corpus produces merged authority Works with provenance pointing at the source records, every canonical Work carries one `bf:identifiedBy` per absorbed Helmet record AND one `bffi:adminMetadata` link to a populated `bffi:AdminMetadata` block, and `canonical-map.jsonl` is correctly populated.

### M9 — Reconciliation against KANTO / VIAF / YSO / KAUNO / MUSO

Reconciliation uses the LLM to pick the right authority URI from candidates. With local models this is more uneven than judge work, so the deterministic fallback matters.

**Authority priority (committed):**

- **Persons & corporate bodies:** KANTO first; VIAF as fallback for non-Finnish creators not found in KANTO.
- **General subjects:** YSO.
- **Fiction genre/form:** KAUNO.
- **Music subjects/forms:** MUSO.

Each entity type queries its primary authority first, falling back only on miss.

- [ ] `src/bffi_pipeline/stages/reconcile.py` queries Finto's REST API (Skosmos client) for KANTO, YSO, KAUNO, MUSO. VIAF via its API as fallback.
- [ ] For each agent string, retrieve top-k (default `k=10`) candidates by lexical similarity from the relevant authority.
- [ ] **Tiered decision logic** — don't reach for the LLM if you don't have to:
  - If exactly one candidate has lexical similarity > 0.95 → take it deterministically. Log to provenance with stage `"reconciliation-lexical"`.
  - If multiple high-similarity candidates → LLM-pick from the top-k. Log with stage `"reconciliation-llm"`.
  - If LLM returns `uncertain` OR LLM confidence < 0.80 → **deterministic fallback**: take the highest lexical-similarity candidate but set the canonical Work's AdminMetadata `bffi:descriptionAuthentication` to `<bib:auth/needs-review>` (see spec § 8 "AdminMetadata view"). Log the reconciliation Activity with stage `"reconciliation-fallback"`.
  - If no candidate has lexical similarity > 0.70 → leave unreconciled. Log with stage `"reconciliation-no-candidate"`.
- [ ] Cache results aggressively. Cache key includes the authority source, the input agent string, and the date (re-fetch monthly).
- [ ] Add the appropriate authority URI link (e.g., `bffi:creator` pointing at a KANTO URI) to canonical Works on success. On any non-success, leave the literal but log the attempt.
- [ ] **AdminMetadata update on success.** When reconciliation lands on a real authority URI (any of the four success cases above except `"reconciliation-no-candidate"`), append the chosen authority URI to the canonical Work's AdminMetadata via `bffi:sourceConsulted` and bump `bffi:descriptionChangeDate` to the reconciliation timestamp. On the `"reconciliation-fallback"` path, also set `bffi:descriptionAuthentication` = `<bib:auth/needs-review>` (already specified above). See spec § 8 "AdminMetadata view".
- [ ] Generate a reconciliation review queue from the `"reconciliation-fallback"` and `"reconciliation-no-candidate"` cases. The query is the AdminMetadata-side filter `?w bffi:adminMetadata/bffi:descriptionAuthentication <bib:auth/needs-review>`, joined to the latest reconciliation Activity for rationale.

**Definition of done:** Sample creators that exist in KANTO are linked deterministically where possible, by LLM where lexical is ambiguous, and flagged for review where neither is confident. The provenance graph distinguishes all four cases. The reconciliation review queue is non-empty and meaningful.

### M10 — Skosify overlay + Fuseki load

- [ ] `config/overlay/bffi-skos-overlay.ttl` and `config/bffi.cfg` per spec §5.
- [ ] **`config/bffi-admin-vocabulary.ttl`** per spec § 8 "AdminMetadata view" — stable URIs for shared value-class instances (Agents dual-typed `prov:SoftwareAgent, bffi:Agent`; `bffi:GenerationProcess`; `bffi:DescriptionAuthentication` for `auto-merged` / `needs-review` / `verified`; `bffi:DescriptionConventions`; `bffi:DescriptionLevel`; `bffi:EncodingLevel`; `bffi:MetadataLicensor` for CC0; `bffi:RecordingSource` for Helmet). Loaded alongside the overlay.
- [ ] **Declare the Helmet source URI in the overlay file** so it's resolvable as a real entity in Skosmos:
  ```turtle
  <http://urn.fi/URN:NBN:fi:bib:source:helmet> a bf:Source ;
      rdfs:label "Helmet"@en, "Helmet"@fi, "Helmet"@sv ;
      bf:code "helmet-bib" ;
      rdfs:comment "Joint catalog of the public libraries of Helsinki, Espoo, Vantaa, and Kauniainen. Underlying ILS is Sierra; this is implementation detail and not preserved in the data."@en .
  ```
- [ ] `src/bffi_pipeline/stages/skosify_run.py` shells out to Skosify with the overlay, producing dual-typed output.
- [ ] `src/bffi_pipeline/stages/load.py` uploads to Fuseki via SPARQL Graph Store Protocol. Loads main data and `config/bffi-admin-vocabulary.ttl` into the configured `bffi-works` graph; provenance Activities into the provenance graph.
- [ ] **Pin Fuseki version** in `docker-compose.yml` to a specific Apache Jena 5.x release (e.g., `stain/jena-fuseki:5.0.0`). Verify `jena-text` is enabled. Document the chosen version in the runbook.
- [ ] **Boundary 5 validation (post-load smoke tests).** Run all `ASK` queries from `config/shapes/post-load-smoke.rq` against Fuseki immediately after load. All must return `true`:
  - `ASK { ?w a bffi:Work, skos:Concept ; skos:prefLabel ?l }` — Skosify dual-typing worked.
  - `ASK { ?e a bffi:Expression, skos:Concept ; bffi:expressionOf ?w }` — Expressions linked to Works.
  - `ASK { ?w a bffi:Work ; bf:identifiedBy [ bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] }` — Helmet identifiers preserved.
  - `ASK { ?w skos:narrower ?e . ?e skos:broader ?w }` — Skosify-inferred inverses present.
  
  Any failure rolls back the load: drop the loaded named graph and exit non-zero. Don't leave Fuseki in a half-loaded state.
- [ ] **Helmet lookup query and CLI:** create `sparql/queries/helmet_lookup.rq`:
  ```sparql
  PREFIX bf:   <http://id.loc.gov/ontologies/bibframe/>
  PREFIX bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:>
  PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

  SELECT ?canonicalWork ?expression WHERE {
    ?canonicalWork a bffi:Work ;
                   bf:identifiedBy [
                     a bf:Local ;
                     rdf:value $helmet_id ;
                     bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet>
                   ] .
    OPTIONAL {
      ?expression bffi:expressionOf ?canonicalWork ;
                  bf:identifiedBy [
                    rdf:value $helmet_id ;
                    bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet>
                  ] .
    }
  }
  ```
  And expose it as `bffi-pipeline lookup-helmet <id>` — runs the query against Fuseki and prints the canonical Work URI plus all Expressions plus a brief merge summary.

**Definition of done:** `make publish` loads the sample, the pinned Fuseki version is documented, the Helmet source URI is declared and visible, and Skosmos at `localhost:9090` shows the vocabulary with browseable Works and Expressions carrying their Helmet identifiers.

### M11 — Skosmos config

- [ ] `config/skosmos-config.ttl` per spec §4 with the two `skosmos:indexShowClass` entries.
- [ ] **Pin Skosmos to a specific 3.x release** in `docker-compose.yml`. Document the chosen version in the runbook.
- [ ] **Configure language priority:** `skosmos:language "fi", "sv", "en"` and `skosmos:defaultLanguage "fi"`. Every test/sample record should carry Finnish labels at minimum.
- [ ] Verified: hierarchy view shows Works with Expressions nested below them.
- [ ] Search works in Finnish, Swedish, and English.

**Definition of done:** Manual smoke test of Skosmos 3 UI passes — types display correctly, hierarchy is right, default display is Finnish, multilingual search works across all three languages.

### M12 — Gold set + evaluation harness

- [ ] `gold/gold.jsonl` with 50–100 starter cases, stratified by category per spec §9. **Hold-out: 30%, hand-marked with `"holdout": true` per case** (not hash-derived). Stratification across categories matters — every category should have at least 2–3 hold-out cases. Revisit at ~500 total cases (likely 12–24 months in) and consider dropping to 20%.
- [ ] `src/bffi_pipeline/eval/harness.py` per spec §9. Reads the JSONL, splits on the `holdout` field, uses training cases for any few-shot prompts and hold-out cases for accuracy reporting.
- [ ] `src/bffi_pipeline/eval/grow.py` runs the "overridden decisions" SPARQL query and outputs candidates for gold-set growth. New cases default to `"holdout": false`; the user explicitly flips the flag.
- [ ] **CI on GitHub-hosted Linux runners only** (see `docs/ci-strategy.md`). Lint, type-check, unit tests, and integration tests (Fuseki + Skosmos via Docker services) run on every PR. **The LLM eval is NOT in CI** — local LLM dependency makes it impractical on hosted runners.
- [ ] **Eval runs manually on the M5 Max** before any PR that touches `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/`. Output of `make eval` is pasted into the PR description.
- [ ] Integration tests requiring Ollama are tagged `@pytest.mark.requires_llm` and excluded from CI via `-m "not requires_llm"`.

**Definition of done:** `make eval` reports accuracy by category against the hold-out set; gold set is hand-marked with explicit hold-out flags; CI runs cleanly on Linux runners; PR template prompts for eval output where relevant.

### M13 — Documentation + handoff

- [ ] `LICENSE` file with Apache License 2.0 text. Add `Copyright (c) <year> University Of Helsinki (The National Library Of Finland)` at top, matching NLF convention.
- [ ] `README.md` in English, covering: what this is, NLF pro bono context, prerequisites (M5 Max + 128 GB + Ollama setup), how to run on a sample, how to run on real data, license note.
- [ ] Inline docstrings on every public function in `src/`. Docstrings in English.
- [ ] One end-to-end runbook in `docs/runbook.md` (English): from a fresh directory of MARCXML files to live Skosmos display, including expected timings on M5 Max, the chosen pinned versions, and the chosen vllm-mlx concurrency value from M6 tuning.
- [ ] Architecture diagram (mermaid in README is fine) showing the stages and their inputs/outputs.
- [ ] `pyproject.toml` carries `license = "Apache-2.0"` and `authors`.

**Definition of done:** A new contributor can clone the repo, follow the README, and produce a working Skosmos 3 instance from the sample data within an hour. The repo is in the shape NLF would expect for an upstream contribution.

---

## When you finish a milestone

1. Run `make lint && make test`. Both must pass.
2. Update `README.md` if the user-facing surface changed.
3. Update this file's milestone checklist (mark items `[x]`).
4. Commit with a message tagging the milestone: `M3: BIBFRAME → BFFI conversion`.
5. Surface any blockers, deferred decisions, or scope questions before starting the next milestone.

## Reference: spec sections by milestone

| Milestone | Spec section |
|---|---|
| M0 | (skeleton; no spec dependency) |
| M1 | (URI minting; no direct spec dependency) |
| M2 | §1 (background), §2 (pipeline overview) |
| M3 | §3 (SPARQL CONSTRUCT) |
| M4 | §6 (Stage 1 blocking) |
| M5 | §6 (Stage 2 embeddings) |
| M6 | §6 (Stage 3), §7 (judge code) |
| M7 | §8 (provenance) |
| M8 | §6 (merge logic) |
| M9 | §6 (reconciliation paragraph) |
| M10 | §5 (Skosify overlay), §2 (load step) |
| M11 | §4 (Skosmos config) |
| M12 | §9 (gold set + harness) |
| M13 | (documentation; consolidates all sections) |

## Open questions to surface at the right milestone

Most decisions are committed. Only ask if:

1. **BFFI property allocation specifics** (before M3). The spec gives illustrative routing; fetch and parse `schema.finto.fi/bffi/` to get the canonical assignments.
2. **KANTO/YSO/KAUNO/MUSO API specifics** (before M9). Finto's REST API endpoints, rate limits, and response shapes need verification from `api.finto.fi`.
3. **Judge model gold-set baseline** (before declaring M6 done). If any category is below 75%, ask before proceeding — the answer might be a different model, more few-shots, or routing that category to human review only.
4. **Production run scheduling** (before kicking off the M6 batch over 800k records). Confirm uninterrupted laptop time (no sleep, plugged in, sufficient disk).
