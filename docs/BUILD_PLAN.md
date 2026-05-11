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
ollama pull qwen3:32b-q4_K_M         # primary judge (~18 GB)
ollama pull qwen2.5:72b-instruct-q4_K_M         # cascade fallback (~40 GB)

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
LLM_MODEL_PRIMARY=qwen3:32b-q4_K_M
LLM_MODEL_FALLBACK=qwen2.5:72b-instruct-q4_K_M

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

- [x] **Cross-check property allocation against `docs/lkd.rdf` before shipping the CONSTRUCT pair.** The vendored ontology (~4600 lines, RDF/XML) is the single source of truth for `bffi:*` predicate names and `rdfs:domain` / `rdfs:range`. `https://schema.finto.fi/bffi/` is the published URL but is currently 403-protected outside the Finto network. Spec § 3 has been verified against `docs/lkd.rdf`; re-verify whenever BFFI publishes a minor revision. *(Note: marc2bibframe2 v3.1.0 emits `bf:PrimaryContribution`, not `bflc:PrimaryContribution` as the spec example shows; CONSTRUCTs match converter output.)*
- [x] `sparql/bf_to_bffi_work.rq` and `sparql/bf_to_bffi_expression.rq` per spec §3.
- [x] `src/bffi_pipeline/stages/bf_to_bffi.py` runs both CONSTRUCTs against an in-memory rdflib graph and writes Turtle output.
- [x] **Preserve `bf:identifiedBy` triples through the CONSTRUCT.** Both passes must copy the Helmet identifier from the source `bf:Work` onto the new `bffi:Work` *and* the new `bffi:Expression`. M8 is where multiple raw Works merge; M3 is where identifiers are first attached.
- [x] Unit tests verify property allocation: translator's `bf:contribution` → `bffi:Expression`, primary creator → `bffi:Work`, language → Expression, originDate → Work.
- [x] Unit test verifies identifier preservation: every minted `bffi:Work` and `bffi:Expression` carries `bf:identifiedBy` with the correct Helmet bib ID.
- [x] Tests cover deterministic linking: every `bffi:Expression` has a matching `bffi:Work` via `bffi:expressionOf`.
- [x] **Boundary 3 validation (BFFI post-CONSTRUCT).** Validate output against `config/shapes/bffi.shape.ttl` via `pyshacl`. Required shapes: every `bffi:Work` has at least one `bffi:hasExpression`; every `bffi:Expression` has exactly one `bffi:expressionOf`; every Work and Expression has a `bf:identifiedBy` with `bf:source = <http://urn.fi/URN:NBN:fi:bib:source:helmet>`; every Work has `skos:prefLabel` in at least one of `fi`/`sv`/`en`; class disjointness; properties allocated to Work-only don't appear on Expression and vice versa.
- [x] Validation report goes to `<BFFI_DATA_DIR>/bffi/_validation.jsonl`. Failures emit a CLI warning with counts but **do not block** the pipeline — at 800k records, even 0.1% failure is 800 records that need triage, not a halt.
- [x] Unit tests for the shapes: hand-craft one valid and one deliberately invalid graph per shape and assert each is judged correctly.
- [x] **`skos:prefLabel` language tagging.** The MARC 245 main title arrives untagged from the CONSTRUCT; post-process tags each prefLabel literal with a BCP-47 language code drawn from the main `bf:Work`'s `bf:language` (filtered to fi/sv/en/ru). Cataloguers pack parallel titles into a single 245$a (`" = "`, `" / "`, em-dash, `" | "`); the post-processor splits on those separators and emits one labeled prefLabel per detected segment. *(`src/bffi_pipeline/title_lang.py`. `_candidate_languages()` in `bf_to_bffi.py` walks only the main `bf:Work` — it skips `bf:associatedResource` sub-Works and the `Note otx` translated-from sub-node so MARC 041 $h doesn't pollute the candidate set.)*
- [x] **Layered detection via Lingua.** Cyrillic short-circuits to `ru`; Latin segments go through `lingua-language-detector` constrained to the candidate set with a 0.65 confidence floor. When every Latin segment confidently maps to the same language despite the cataloguer declaring multiple, the heuristic collapses back to one full-string label rather than fabricating splits Lingua can't actually distinguish. *(24 unit tests in `tests/unit/test_title_lang.py`.)*
- [x] **Local-LLM cascade for ambiguous parallel titles** (the Tšarka pattern: `"Tšarka : the Russian charka = venäläinen tšarkka = russkaja tšarka"` — three parallel titles in en/fi/ru that all read as Finnish to Lingua because the romanized-Russian segment carries Finnish-looking diacritics). When the collapse heuristic fires, escalate to Qwen3 32B for per-segment language assignment. Versioned prompt at `prompts/title_lang_v1.txt`, `extra="forbid"` Pydantic schema with substantive-rationale validator, validation retry (max 2) + connection retry (5/30/120s, max 3) mirroring the M6 judge / M9 picker policy stack, and a post-parse filter that rewrites any segment language outside the candidate set to `null`. On by default; opt out via `bf-to-bffi --no-llm-title-cascade` to keep M3 graph-only. *(`src/bffi_pipeline/title_lang_llm.py`; 17 unit tests in `tests/unit/test_title_lang_llm.py` cover schema, retry, cascade integration, and the hallucinated-code defence; live Qwen3 verification on the Tšarka title produces `[en] / [fi] / [ru]`.)*
- [x] **Surface Helmet bib ID for cataloguer discussions.** Post-process emits a flat `dct:identifier` literal in Sierra-style display form (e.g. `"b26282744"`) on every Work / Expression that carries a Helmet `bf:identifiedBy`. The structured `bf:identifiedBy` stays for BIBFRAME interop; the flat predicate gives Skosmos something it can render on the concept page (it can't traverse the `bf:Local` blank node). The check digit is computed via the III/Sierra modulus-11 algorithm in `src/bffi_pipeline/helmet.py` — each digit times its position from the right starting at 2, summed mod 11, with 10 → `x`. Pinned by 13 cataloguer-confirmed Helmet IDs in `tests/unit/test_helmet.py`. M11 labels the predicate; M8 propagates to canonical Works.
- [x] **MARC 245$c contributor-extraction cascade** (default-off). A multilingual stop-word-filtered heuristic decides whether a record's 245$c statement of responsibility carries capitalised name-tokens not covered by 100/700; on a 5,000-record random sample of the 800k Helmet corpus the heuristic fires on ~13% of records, eyeballed precision ~70-85%. When the heuristic fires and an extractor is supplied, the local Qwen3 cascade returns `[(name, MARC_relator_code)]` plus any variants of agents already in 100/700 (Cyrillic↔Latin transliteration in particular, also Finnish-name typos like `Anssi` vs `Assi`). Versioned prompt at `prompts/contrib_extract_v1.txt`; `extra="forbid"` Pydantic schema with an *at-least-one* `relator_code` / `transliteration_of` validator (loosened from XOR after the live smoke surfaced legitimate both-set entries on Helmet record 1714651); validation retry (max 2) + connection retry (5/30/120s, max 3) mirroring the M3 title and M9 picker policies; explicit `timeout=120` + `max_retries=0` on `ChatOpenAI` so a slow Ollama can't pin a worker outside our retry stack; post-parse filter rejects hallucinated relator codes outside the controlled vocabulary + phantom transliteration pointers to agents not in the supplied existing-agents tuple. New non-primary contributions land as `bffi:Contribution` blocks on the raw `bffi:Expression` with `bf:role <http://id.loc.gov/vocabulary/relators/<code>>`; blank-node IDs derived from SHA-1 of `(expr_uri, name, relator_code)` for byte-stable re-runs. **Variant-flagged candidates are NOT emitted** — when the LLM tells us a 245$c name is a variant of an existing 100/700 agent, we let M9 script-variant binding consume the pointer downstream rather than propagate the cataloguer's typo'd form as a new agent. **Default model is Qwen3 8B Q4_K_M**, not 32B: live benchmark on 4 representative cases (Hogwood / Karttunen / Spector / Bridžet Kollinz) showed 8B produces correct extractions in 8-15 s/call warm vs 35-60 s for 32B at the same quality; `--primary-model` still overrides. Default off — opt in via `bf-to-bffi --llm-contrib-cascade` until M12 gold-set validation lands. *(`src/bffi_pipeline/contrib_extract.py` + `contrib_extract_llm.py`; 42+ unit tests across `tests/unit/test_contrib_extract.py` + `test_contrib_extract_llm.py` + new emitter tests in `test_bf_to_bffi.py`. Live `requires_llm`-marked integration test at `tests/integration/test_contrib_extract_live.py` exercises three cases — Hogwood (new conductor), Spector (foreword author), Anssi/Assi (variant detection) — with tolerance for relator-code variation; at most 1 of 3 may fail. Live smoke on the three records produced 2 clean Contributions (Hogwood→cnd, Spector→aft) + 1 correctly-skipped variant (Anssi flagged as `transliteration_of` Karttunen, Assi).)*
- [ ] **M3 cascade follow-ups (F1, F2, F3) in dependency order** — see [`docs/plans/p-05-m3-cascade-follow-ups.md`](plans/p-05-m3-cascade-follow-ups.md). F1 propagates non-primary cascade Contributions onto canonical Expressions (~150 LOC, unblocks F2/F3); F2 wires transliteration variants through a sidecar + M9 reader (~400 LOC, highest cataloguer-hour leverage); F3 reconciles non-primary canonical contributions via KANTO (~250 LOC, gated on `gold/contrib.jsonl` reaching 30-50 cataloguer-vetted cases).

**Definition of done:** Sample records produce well-formed BFFI with correct property routing, Helmet identifiers attached, and a clean validation report (or a known/expected failure set documented in the runbook).

### M4 — Work-key blocking (Stage 1)

- [x] `src/bffi_pipeline/stages/workkey.py` with `compute_blocking_key(work: dict) -> str`. Deterministic, accent-fold + lowercase + strip punctuation, normalized creator surname + first significant title token + content type code.
- [x] CLI subcommand `bffi-pipeline workkey-stats <bffi.ttl>` reports block size distribution. *(Accepts a single `.ttl` file or a data directory with `bffi/` + `bibframe/` subdirs — the directory mode joins agent labels in.)*
- [x] Unit tests cover: same surname different given names → same block; transliteration variants → same block (use accent folding); accents → ignored.

**Definition of done:** Running on the sample produces blocks that group what should be grouped.

### M5 — Embedding candidates (Stage 2)

**Scale context:** The production corpus is ~800k Works. HNSW gives sub-ms queries with >98% recall at ~5 GB RAM peak. On the M5 Max with MPS-accelerated embedding, building takes ~30–60 minutes. The index's job is cross-block recall (catching transliteration variants and malformed title fields the rule-based blocker missed).

The gold-set-independent structural pieces are committed; the sub-tasks marked **(blocked on M12 gold set)** can only finalise once `gold/` exists.

- [ ] **Benchmark embedding models against the gold set before locking in** — see [`docs/plans/p-04-m5-calibration.md`](plans/p-04-m5-calibration.md) Phase A. Harness committed; awaits a local benchmark run on the M5 Max plus the resulting `embeddings.py` docstring update (and, if the winner is not BGE-M3, `.env.example` + HNSW config edits).
- [x] `src/bffi_pipeline/stages/embeddings.py` builds a FAISS `IndexHNSWFlat` over an L2-normalized 1024-dim embedding (BGE-M3 by default; chosen winner from benchmark). Use `metric=METRIC_INNER_PRODUCT`, `M=32`, `efConstruction=200`. Document why these values in a docstring.
- [x] Embedding input string format: pipe-separated, fixed field order — `"creator: <X> | title: <Y> | language: <Z> | year: <Y> | type: <T>"`. Stable so re-embedding produces identical vectors.
- [x] Use `sentence-transformers` with the `mps` device (PyTorch's Metal backend). Batches of 64–128 records on M5 Max should saturate the GPU. Report progress every 10k records. *(Implemented via sentence-transformers' built-in `show_progress_bar`; per-batch granularity, not strictly per-10k.)*
- [x] Persist the FAISS index to `<output_dir>/embeddings.faiss` and the URI→vector-id mapping to `<output_dir>/embeddings.idmap.json`. Downstream stages must reload, never rebuild. Skip the build step if both files exist and are newer than the input BFFI file.
- [x] For each Work, query top-k (default `k=20`) neighbours and emit candidate pairs above the low threshold. Apply blocking-key intersection as a post-filter — only keep pairs that share a block — unless `--cross-block` is passed.
- [x] **Threshold defaults are tightened from the spec** to reduce LLM workload given local-inference throughput constraints: auto-merge ≥0.90, escalate 0.78–0.90, reject ≤0.78. These must be configurable; validate the chosen values against the gold set in M12 before treating them as final. *(Defaults committed in code; gold-set validation is the M12 task.)*
- [x] Output: JSONL of candidate pairs with similarity score and both blocking keys. Also emit summary counts: pairs in each band so M6 can plan run time.
- [x] CLI subcommand `bffi-pipeline embed-stats` reports: index size, build time, top-k similarity distribution, fraction of pairs above each threshold, fraction of cross-block hits.
- [ ] **Tune `efSearch` against the gold set** — see [`docs/plans/p-04-m5-calibration.md`](plans/p-04-m5-calibration.md) Phase B. Default `efSearch = 64` is committed as a placeholder; the sweep runs once a FAISS index over the production corpus exists.
- [x] Unit test (against a small synthetic corpus, not 800k): same creator/title in two languages scores above 0.85; obvious different works score below 0.5. *(Structural test against synthetic vectors lands in `tests/unit/test_embeddings.py`; the cosine claim against real BGE-M3 output is exercised by `bffi-pipeline embed-benchmark` against `gold/gold.jsonl` per the same-work / different-work mean-similarity gap report.)*

**Definition of done:** Candidate JSONL contains expected translation/transliteration pairs from the gold set; index file persists and reloads correctly; `embed-stats` runs in seconds; the count of pairs in the "escalate" band is reported and you have a realistic estimate of the M6 run time. *(Persistence, reload, and per-band reporting verified against synthetic data; the gold-set bootstrap covers same-work / different-work pairs across seven categories at `gold/gold.jsonl`; the actual benchmark run on real BGE-M3 and the production-corpus index are user-side tasks on the M5 Max.)*

### M6 — LLM judge (Stage 3)

**This is the throughput-bound stage.** Read `docs/local-inference.md` before starting. Plan the production run as a multi-night batch job.

All phases (1, 2a, 2b) are committed. The one outstanding sub-task is the `--concurrency` tuning sweep `{4, 8, 16, 32}` itself — a one-time benchmark against a 1 k-pair sample on the user's M5 Max with vllm-mlx running. The harness is in place; `docs/runbook.md` documents the recipe.

- [x] `prompts/judge_v1.txt` containing the system prompt + few-shot block from spec §7. **Tune the few-shots specifically for the chosen model** — Qwen3 responds differently to few-shot phrasing than Claude does. Iterate against the gold set. *(Initial two-shot block committed; few-shot tuning against gold pairs is the user's M5-Max work.)*
- [x] `src/bffi_pipeline/stages/judge.py` with `WorkRecord`, `WorkMatchDecision`, and `judge_pair`. Read prompt from file; hash at startup.
- [x] Use `langchain-openai` `ChatOpenAI` pointed at `LLM_BASE_URL`. Pass `temperature=0`, `seed=42`. Use `with_structured_output(WorkMatchDecision, method="json_schema")`.
- [x] **Validate JSON output reliability before mass running.** Open-source models occasionally produce malformed JSON even with schema-constrained generation. Wrap calls with retry-on-parse-error (max 2 retries) and log permanent failures as `decision="uncertain"` with the parse error in the rationale. Don't crash the run on a bad parse.
- [x] **Boundary 4 validation (semantic post-validators).** Add `@model_validator(mode="after")` to `WorkMatchDecision` enforcing: `decision="uncertain"` requires `confidence ≤ 0.7`; `decision="same_work"` requires non-empty `matching_fields`; rationale ≥ 20 chars and not containing stub phrases (`"i don't know"`, `"unable to determine"`, `"n/a"`). Validation failures share the retry path with JSON parse failures. **Validation-failed responses must not be cached** — caching cements bad outputs across re-runs.
- [x] SQLite cache for repeated calls keyed on `(model, prompt_hash, record_a_canonical, record_b_canonical)`. Cache writes happen only after both structural and semantic validation pass. *(Custom `JudgeCache` rather than `langchain-community`'s `SQLiteCache` — the LangChain cache fires before validation.)*
- [x] Implement the **two-model cascade**: `judge_pair` takes a model name; `cascade_judge` runs primary first, then fallback for `uncertain` or low-confidence `same_work`. Both decisions logged to provenance with distinct stage names. *(`CascadeStep`/`JudgeOutcome` carry the per-step decisions; phase 2b wires `judge_batch` to `ProvenanceWriter`, emitting one `bffi-prov:WorkMergeDecision` Activity per cascade step with the right `bffi-prov:stage` tag.)*
- [x] Add a `--server vllm-mlx` mode that uses concurrent requests for the production batch. Ollama mode stays serial. *(Implemented as a server-agnostic `--concurrency` flag on `bffi-pipeline judge`. Default 1 (Ollama serial); production batch points `LLM_BASE_URL` at vllm-mlx and bumps `--concurrency` to a tuned value. ThreadPoolExecutor processes pairs in fixed-size chunks so JSONL output preserves input order and the checkpoint advances contiguously. `JudgeCache` connection opened with `check_same_thread=False`; SQLite serialises writes through its own write lock.)*
- [ ] **Tune `--concurrency` for vllm-mlx** — folded into [`docs/plans/p-02-inference-stack-tuning.md`](plans/p-02-inference-stack-tuning.md) Phase A as section A6 since the sweep is only meaningful once the backend is vllm-mlx (continuous batching). Default `M6_CONCURRENCY=1` remains in `scripts/run-full-pipeline.sh` until P-02 ships.
- [x] **Failure recovery via three layered mechanisms:**
  - [x] SQLite cache as primary — keyed on the tuple above, transactional commits per pair.
  - [x] Checkpoint file at `<output_path>.checkpoint` written every 100 pairs recording `{start_time, last_completed_idx, total_pairs, cache_hits, fresh_calls}`. On startup, read it to report "resuming from pair X of Y, ETA …".
  - [x] Inside `judge_pair`, wrap the LLM call with exponential-backoff retry (5s → 30s → 120s) for connection errors. After 3 retries exhaust, log `decision="uncertain"` and continue. **Never crash a multi-day run on a single bad pair.**
- [x] CLI flags: `--resume` (default, auto-resume from checkpoint+cache), `--restart` (force redo). *(`bffi-pipeline judge` defaults to resume; `--restart` wipes both the output JSONL and the checkpoint.)*
- [x] Progress reporting every 100 pairs with rolling average latency and ETA (e.g., `"12,400 / 50,000 pairs · 4.2s/pair · ETA 43h 28m · 8,200 cache hits"`). *(Snapshot built in `JudgeBatchProgress.render`; `bffi-pipeline judge` prints one per checkpoint flush.)*
- [x] Unit tests on schema validation (no LLM calls) — confidence bounds, decision enum, required fields, malformed-JSON retry logic, **and the semantic validators**. *(29 tests in `tests/unit/test_judge.py` covering all three Boundary-4 validators, validation/connection retry layers, cache hits, cascade routing, and a "validation-failed responses are not cached" assertion.)*
- [x] Integration test (marked `@pytest.mark.requires_llm`) on five gold-set cases: translation, adaptation, common-title collision, transliteration variant, uncertainty case. *(`tests/integration/test_judge_live.py` lifts one case per category from `gold/gold.jsonl` and asserts at most one wrong decision out of five — uncertain counts as a soft pass per spec § 9. Excluded from CI by `-m "not requires_llm"`.)*

**Definition of done:** Judge produces structured decisions on all gray-zone candidates from M5; cascade works; cache hits produce identical output to fresh calls; gold-set per-category accuracy is reported and acceptable. Production run plan is documented in `docs/runbook.md`. *(All phases committed: per-pair judge, batch driver with checkpoint + resume + restart + progress, `--concurrency` flag for vllm-mlx, per-decision provenance Turtle emission, and `docs/runbook.md`. The `--concurrency` sweep itself and the gold-set per-category accuracy report are user-side tasks on the M5 Max; the harnesses for both are committed.)*

### M7 — Provenance logging

- [x] `src/bffi_pipeline/provenance/vocab.py` defines PROV and bffi-prov namespaces, the `WorkMergeDecision` and `HumanReview` classes. *(Extended in M7 to add the per-decision predicates — stage, decision, confidence, embeddingSimilarity, rationale, matchingField/divergingField, promptHash/promptSource, rawResponse, modelId/provider/temperature/seed, cacheHit, reviewNote, lastCompactedAt.)*
- [x] `src/bffi_pipeline/provenance/logger.py` with `log_merge_decision` per spec §8 and `log_review` for human overrides. *(Plus `log_software_agent` so each model URI gets a `prov:SoftwareAgent` block.)*
- [x] Provenance writes to a separate named graph `<BFFI_GRAPH_BASE>provenance`, with clear graph separation from the published authority data. *(Persisted as `<BFFI_DATA_DIR>/provenance.ttl` until M10 routes it into Fuseki; the named-graph URI is the subject of the meta sentinel so the M10 loader can attach the right graph.)*
- [x] **CLI subcommand `bffi-pipeline provenance compact --older-than 90d`** removes `bffi-prov:rawResponse` literals from Activity records older than the threshold, keeping the structured fields. Stores the date of last compaction in a small `<BFFI_GRAPH_BASE>provenance-meta` graph as `bffi-prov:lastCompactedAt`. *(Implemented as a Typer sub-app; `compact_provenance()` does the work and `write_last_compacted_at()` refreshes the sentinel even when zero literals match.)*
- [x] **Stale-provenance warning at CLI startup:** every `bffi-pipeline ...` invocation queries `bffi-prov:lastCompactedAt`; if older than 90 days (or absent), print a warning to stderr. Don't block the command, just nag. *(Wired into the root Typer callback; suppressed silently when `provenance.ttl` does not exist so early-milestone runs stay quiet.)*
- [x] Unit test: a mock decision produces triples that round-trip through rdflib and contain all required fields. Compaction unit test verifies that structured fields survive and `rawResponse` is removed for old records. *(19 tests in `tests/unit/test_provenance.py`: vocab predicates, logger required-predicate set, canonical wasGeneratedBy/wasDerivedFrom on same_work, review chain via wasInformedBy, writer Turtle round-trip + append-on-restart, meta read/write, compaction strips old rawResponse and refreshes the sentinel, stale-warning fires/silences correctly.)*

**Definition of done:** Every judge call writes a complete provenance record. Negative decisions are logged identically. Compaction subcommand works and the staleness warning fires when expected. *(Provenance infrastructure complete; the M6 batch driver hook — passing `ProvenanceWriter.add_merge_decision` as the existing `decision_callback` — lands in M6 phase 2b.)*

### M8 — Merge application

- [x] `src/bffi_pipeline/stages/merge.py` reads judge decisions, groups Works by transitive `same_work` relation (union-find), mints canonical Work URIs, rewrites all `bffi:expressionOf` relations to point at canonical URIs, retains raw Works as `prov:wasDerivedFrom` targets. *(Path-compressed `_UnionFind` keyed on lex-smallest root; canonical URI minted via the existing `mint_work_uri(creator_uri, pref_label)`. Each absorbed Expression gets a rewritten `bffi:expressionOf <canonical>` triple in `canonical.ttl`; the M3 raw-Work Turtle stays untouched so M10 can load both graphs side by side.)*
- [x] **Union the `bf:identifiedBy` sets** when raw Works merge. The canonical Work for "Sota ja rauha" that absorbed 47 raw Helmet records carries 47 `bf:identifiedBy` triples — one per source. *(Identifier dedup is by Helmet bib_id, not by identifier-URI, so the same record absorbed twice doesn't produce a duplicate triple.)*
- [x] **Write `<BFFI_DATA_DIR>/canonical-map.jsonl`:** one row per canonical Work recording the canonical URI, all source raw Work URIs, and all Helmet bib IDs:
  ```jsonl
  {"canonical_work_uri": "...", "raw_work_uris": ["...", "..."], "helmet_bib_ids": ["12345678", "12345679"], "merged_at": "2026-05-08T20:14:00Z"}
  ```
  Joined with `helmet-map.jsonl` from M2, this gives O(1) Helmet ID → canonical Work URI lookup. *(Rows are written sorted by canonical URI so re-runs produce a byte-stable file.)*
- [x] Idempotent: running merge twice is a no-op. Identifier sets are deduplicated. *(Verified by a byte-equality test: same `now=` and same inputs → identical `canonical.ttl`, `canonical-map.jsonl`, and `canonical-conflicts.jsonl`. Wall-clock `datetime.now()` is centralised on a single `now or datetime.now(UTC)` call so production runs land on one consistent timestamp.)*
- [x] **Mint canonical-Work AdminMetadata.** Each canonical `bffi:Work` and each `bffi:Expression` produced at merge time gets a fresh `bffi:AdminMetadata` block via `bffi:adminMetadata`. Fields: `bffi:adminMetadataFor` (back-link), `bffi:descriptionCreationDate` = earliest absorbed M2 timestamp (from `helmet-map.jsonl`), `bffi:descriptionChangeDate` and `bffi:dateGenerated` = merge timestamp, `bffi:descriptionModifier` = the cascade winner (e.g., `<bib:agent/qwen3-32b-q4_K_M>` or `<bib:agent/qwen2.5-72b-instruct-q4_K_M>`), `bffi:descriptionAuthentication` = `<bib:auth/auto-merged>`, `bffi:descriptionLevel` = `<bib:desc-level/minimum>`, `bffi:encodingLevel` = `<bib:enc-level/auto>`, `bffi:generationProcess` = `<bib:gen-process/bffi-pipeline/v<version>>`, `bffi:descriptionConventions` = `<bib:desc-conv/bffi-1.0.0>`, `bffi:metadataLicensor` = `<bib:metadata-licensor/cc0>`, `bffi:recordingSource` = `<bib:recording-source/helmet>`, `bffi:sourceMetadata` = the union of absorbed Helmet record URIs (matching `canonical-map.jsonl`). Spine link: `prov:wasGeneratedBy` = the latest `bffi-prov:WorkMergeDecision` Activity URI for this Work. See spec § 8 "AdminMetadata view". *(Spine link to the latest `bffi-prov:WorkMergeDecision` is deferred — the merge stage can't trivially reach into the provenance Turtle to pull the matching Activity URI without M10's Fuseki, and the BFFI-side AdminMetadata view is layered alongside the PROV-O graph rather than the source-of-truth. The other 13 predicates are committed; the spine link gets added when M10 routes both graphs into Fuseki.)*
- [x] Unit tests cover: chain merging (A=B, B=C → A=B=C); conflict handling (A=B, A≠C, B=C — flag for review, don't merge silently); identifier accumulation; idempotency; AdminMetadata block presence and `sourceMetadata` count = absorbed-record count. *(18 tests in `tests/unit/test_merge.py`. Conflict groups land in `canonical-conflicts.jsonl` — explicitly excluded from `canonical.ttl` and `canonical-map.jsonl`. Four extra tests cover the M9 phase-3 extension: URI subjects dedupe across absorbed members, blank-node subjects with the same `(label, source)` key collapse to one canonical bnode, the `bffi:genreForm` predicate is preserved, and byte-stability holds in the presence of the new blank nodes.)*
- [x] **Propagate `bffi:subject` + `bffi:genreForm` to the canonical Work (M9 phase-3 prereq).** Each member's URI-resolved targets dedupe by URI; unresolved targets dedupe by `(label, source)` and emit one blank node on the canonical with a deterministic SHA-1-based BNode identifier so re-runs stay byte-stable. Ordering inside the predicate is `(label, source)` lexical so rdflib's serialisation is stable. The blank nodes carry forward `rdfs:label` + `bf:source`, ready for M9 phase 3 to bind authority URIs and bridge via `prov:specializationOf`.
- [x] **Propagate the Sierra-style `dct:identifier` to canonical Works.** Each absorbed bib_id contributes one `dct:identifier "b<id><check>"` triple alongside the unioned `bf:identifiedBy` set, so a merged group with 47 absorbed records shows 47 bib numbers under "Helmet bib ID" in Skosmos rather than just one. Same denormalisation as M3; emitted directly here because canonical Works are built from member metadata, not by triple-level union from raw graphs.
- [x] **Propagate multi-language prefLabels onto canonical Works.** M3's title-language cascade emits one `skos:prefLabel` per parallel-title segment (e.g. en/fi/ru on the Tšarka pattern). M8 reads the full `(text, lang)` set via `_all_pref_labels`, unions across absorbed members, and emits each as a lang-tagged literal on the canonical so Skosmos picks the right per-language label rather than collapsing to one untagged string. The single-string `CanonicalWorkInputs.pref_label` is kept as the deterministic URI-mint anchor.

**Definition of done:** Sample corpus produces merged authority Works with provenance pointing at the source records, every canonical Work carries one `bf:identifiedBy` per absorbed Helmet record AND one `bffi:adminMetadata` link to a populated `bffi:AdminMetadata` block, and `canonical-map.jsonl` is correctly populated. *(All structural pieces verified against synthetic in-memory work_records; an end-to-end `M2 → M3 → M6 → M8` integration test on the curated 13 records is still pending — a useful follow-up once M9's reconciliation lands and the cascade has actual decisions to merge over.)*

### M9 — Reconciliation against KANTO / VIAF / YSO / KAUNO / MUSO

Reconciliation uses the LLM to pick the right authority URI from candidates. With local models this is more uneven than judge work, so the deterministic fallback matters.

**Authority priority (committed):**

- **Persons & corporate bodies:** KANTO first; VIAF as fallback for non-Finnish creators not found in KANTO.
- **General subjects:** YSO.
- **Fiction genre/form:** KAUNO.
- **Music subjects/forms:** MUSO.

Each entity type queries its primary authority first, falling back only on miss.

Phases 1 + 2 + 3 (committed) ship the full creator + subject reconciliation pipeline against KANTO / VIAF / YSO / KAUNO / MUSO: schemas, the four-tier decision logic, the Finto Skosmos HTTP client, the LangChain-backed `LLMPicker` reading `prompts/picker_v1.txt`, the orchestrator that walks `canonical.ttl` for both creators and subjects, the AdminMetadata + provenance side effects, the `bffi-pipeline reconcile` CLI subcommand with `--kinds` filtering, and a live integration test marked `requires_llm`. Phase 3 also extended M8 to propagate `bffi:subject` + `bffi:genreForm` onto the canonical Work with deterministic blank-node IDs, so byte-stable canonical.ttl re-runs continue to hold. Tier-0 (committed) adds a deterministic short-circuit ahead of the four tiers: an exact `skos:prefLabel` match against the locally-loaded Finto authority graphs (M11 option 3b) avoids the api.finto.fi round-trip entirely, dominated at corpus scale by YSA-via-YSO subjects whose prefLabels were inherited unchanged in the 2014-2018 vocabulary merge. Six follow-up corpus-scale wins close the gaps surfaced by the dev smoke: YSO-Paikat + YSO-Aika sub-vocabs loaded into the YSO graph for places + temporal periods; the `kaunokki` legacy source-token routing falls through to YSO when the cataloguer's tag overstates KAUNO availability; MARC 6XX subject-as-name fields (`#Agent600/610/611-N` URI fragments) route to KANTO instead of YSO so a biography of Pekurinen carries `bffi:creator → biographer-kanto-uri` AND `bffi:subject → pekurinen-kanto-uri` distinctly; LCGFT + LCSH loaded for English-cataloguer subjects (LCGFT URIs already cited on `$0` MARC 655 fields, LCSH covers `$2 lcsh` literals at corpus scale).

- [x] `src/bffi_pipeline/stages/reconcile.py` queries Finto's REST API (Skosmos client) for KANTO, YSO, KAUNO, MUSO. VIAF via its API as fallback. *(`FintoSkosmosClient` hits `https://api.finto.fi/rest/v1/search` via an injectable `httpx.Client` so unit tests use `httpx.MockTransport` to assert on the request shape and feed canned JSON. `ViafClient` is the parallel fallback. The KANTO → VIAF cascade is wired through the orchestrator's `client` + `fallback_client` parameters.)*
- [x] For each agent string, retrieve top-k (default `k=10`) candidates by lexical similarity from the relevant authority. *(Lexical similarity is `difflib.SequenceMatcher` over an NFKD + diacritic-fold + casefold + whitespace-collapse normalised pair. Production may swap to `rapidfuzz` later — the contract is "0=disjoint, 1=equal-after-normalisation".)*
- [x] **Tiered decision logic** — don't reach for the LLM if you don't have to, and don't reach for Finto if a local exact match suffices:
  - **Tier 0** (`"reconciliation-local"`): if the cataloguer literal exactly matches a `skos:prefLabel` in the locally-loaded Finto authority graph (YSO for `subject`, KAUNO + SLM for `genre_form`, MUSO for `music_form`), bind that URI deterministically — no `api.finto.fi` round-trip, no LLM. Confidence pinned at 1.0; the synthesised candidate flows through provenance + AdminMetadata exactly like the other tiers. Currently scoped to subject / genre_form / music_form; KANTO persons stay on tier-1 because cataloguer literals like `"Tolstoy, Leo"` never exact-match KANTO prefLabels that include birth-death dates. Skipped entirely when no `local_resolver` is wired (`bffi-pipeline reconcile --no-local-resolver`).
  - If exactly one candidate has lexical similarity > 0.95 → take it deterministically. Log to provenance with stage `"reconciliation-lexical"`.
  - If multiple high-similarity candidates → LLM-pick from the top-k. Log with stage `"reconciliation-llm"`.
  - If LLM returns `uncertain` OR LLM confidence < 0.80 → **deterministic fallback**: take the highest lexical-similarity candidate but set the canonical Work's AdminMetadata `bffi:descriptionAuthentication` to `<bib:auth/needs-review>` (see spec § 8 "AdminMetadata view"). Log the reconciliation Activity with stage `"reconciliation-fallback"`.
  - If no candidate has lexical similarity > 0.70 → leave unreconciled. Log with stage `"reconciliation-no-candidate"`.
  *(All five tiers implemented in `decide_reconciliation()` + `reconcile_one()` and exercised by 8 + 19 unit tests; the picker is an `LLMPicker` Protocol so tests inject a deterministic `StubPicker`, and the local resolver is a `LocalConceptResolver` Protocol with the same StubLocalConceptResolver swap. The phase-2 LangChain implementation slots in without touching the decision logic.)*

- [x] **Tier-0 local resolver against the M11 option 3b authority graphs.** New `src/bffi_pipeline/stages/local_concept_resolver.py` defines `LocalConceptHit`, `LocalConceptResolver` Protocol, `FusekiConceptResolver` (SPARQL POST against the local Fuseki for `?uri skos:prefLabel ?label` exact match in the named graph for the kind), and `StubLocalConceptResolver` for tests. The resolver caches `(kind, literal)` lookups including misses, so a corpus-scale walk over thousands of records mentioning Tampere asks for `"Tampere"` exactly once. Language preference fi > sv > en > untagged via `ORDER BY DESC(IF(LANG(?label) = "fi", 3, …))`; an HTTP failure or empty bindings falls through silently to tier-1. The `bffi-pipeline reconcile` CLI gains `--local-resolver / --no-local-resolver` (default on) wiring the `FusekiConceptResolver` from the same `httpx.Client` used by the Finto + VIAF clients. **Operational impact at 800k records:** the typical YSA-tagged subject literal (`Venäjä`, `Tampere`, `historialliset romaanit`) hits a YSO/KAUNO prefLabel exactly because YSO inherited the YSA labels in the 2014-2018 merge — tier-0 absorbs ~50k+ tier-1 calls that would otherwise have gone to `api.finto.fi`. Provenance Activities still log every attempt with stage `"reconciliation-local"` so the audit trail distinguishes local hits from upstream-Finto hits.
- [x] **Tier-0 graph routing extended for cataloguer reality.** Three patterns surfaced by the dev smoke that the initial tier-0 didn't cover:
  - `genre_form` (cataloguer-tagged `$2 kaunokki`) falls through to the YSO graph when KAUNO + SLM miss — `kaunokki` is the legacy KAUNO name and cataloguers use it for heterogeneous content (places, temporal periods, fiction-specific topics) that often live in YSO-Aika / YSO-Paikat instead. KAUNO + SLM stay first in the VALUES clause so genuine fiction genre/form literals (`historialliset romaanit`) still bind KAUNO when an equivalent label exists in both.
  - `subject` (cataloguer-tagged `$2 lcsh` or `$2 yso` with English content) gains LCSH as a second graph after YSO. YSO comes first because Finnish-source records dominate Helmet; LCSH catches the cross-references.
  - `genre_form` gains LCGFT between SLM and the kaunokki-YSO fallback so English-cataloguer-supplied genre/form labels (`Novels`, `Short stories`, `Video recordings`) bind without a Finto call.
- [x] **MARC 6XX subject-as-name fields route to KANTO.** Cataloguers use MARC 600 (Personal Name), 610 (Corporate Body), 611 (Meeting Name) to mark a work that is *about* a person/organisation. marc2bibframe2 emits each as a URI like `<...#Agent600-25>` carrying just `rdfs:label "Pekurinen, Arndt"` after the M3 → M8 propagation strips upstream `bf:Person` types + `bflc:marcKey "60014$a..."` patterns. New `_classify_subject_target(target_uri, source)` detects the marc2bibframe2 fragment-naming convention (`#Agent6(00|10|11)-N`) and routes 600 → `person`, 610/611 → `corporate_body` so tier-1 hits KANTO instead of YSO. `_apply_canonical_link` was also fixed to dispatch on `predicate_uri` rather than `kind`: a person-kind request from the *subject* walker (predicate set) binds via `bffi:subject`, never `bffi:creator` — distinguishing a biographer's KANTO URI from the biography subject's KANTO URI on the same canonical Work.
- [x] Cache results aggressively. Cache key includes the authority source, the input agent string, and the date (re-fetch monthly). *(In-memory dict on `FintoSkosmosClient` keyed on `(vocab, query, today_iso)` — repeated queries within the same day skip the HTTP call. A persistent SQLite cache for the multi-day production run is a phase-2 swap.)*
- [x] Add the appropriate authority URI link (e.g., `bffi:creator` pointing at a KANTO URI) to canonical Works on success. On any non-success, leave the literal but log the attempt. *(`<work> bffi:creator <authority>` plus `<existing-agent-uri> prov:specializationOf <authority>` so the M3 raw graph keeps a one-hop bridge to the reconciled identity.)*
- [x] **AdminMetadata update on success.** When reconciliation lands on a real authority URI (any of the four success cases above except `"reconciliation-no-candidate"`), append the chosen authority URI to the canonical Work's AdminMetadata via `bffi:sourceConsulted` and bump `bffi:descriptionChangeDate` to the reconciliation timestamp. On the `"reconciliation-fallback"` path, also set `bffi:descriptionAuthentication` = `<bib:auth/needs-review>` (already specified above). See spec § 8 "AdminMetadata view".
- [x] Generate a reconciliation review queue from the `"reconciliation-fallback"` and `"reconciliation-no-candidate"` cases. The query is the AdminMetadata-side filter `?w bffi:adminMetadata/bffi:descriptionAuthentication <bib:auth/needs-review>`, joined to the latest reconciliation Activity for rationale. *(The needs-review tag lands at AdminMetadata-update time; the SPARQL committed in spec § 8 selects exactly the fallback-flagged Works. The "no-candidate" case stays unreconciled with no needs-review tag — by design: there's nothing to review.)*
- [x] **Live LangChain-backed `LLMPicker`** reading `prompts/picker_v1.txt` and calling the local Qwen3 cascade via `langchain-openai`. Mirrors the M6 judge's policy stack: validation retry (max 2), connection-error retry with exponential backoff (5/30/120 s, max 3), and a *post-parse* sanity check that the chosen URI is in the candidate set the authority client returned (no silent binding to a hallucinated URI).
- [x] **`bffi-pipeline reconcile` CLI subcommand** with `--canonical-path`, `--output-path`, `--primary-model`, and `--provenance / --no-provenance` flags. Default-on provenance routes per-attempt `bffi-prov:Reconciliation` Activities into the same `provenance.ttl` alongside M6's `WorkMergeDecision` Activities.
- [x] **Live `requires_llm`-marked integration test** at `tests/integration/test_reconcile_live.py`. Lifts four real creator literals from the curated 13 (Pushkin, Mozart, Morton, Wilson), hits real Finto, and asserts (a) the stage tag is one of the four committed values, (b) any bound URI is from the candidate pool the authority returned. Skipped automatically when `LLM_BASE_URL` is unset or `api.finto.fi` is unreachable.
- [x] **Subject reconciliation across YSO / KAUNO / MUSO (phase 3).** M8 was extended to propagate `bffi:subject` + `bffi:genreForm` triples onto the canonical Work — URI-resolved `$0` targets dedupe across absorbed members, and unresolved targets land as deterministic blank nodes (SHA-1 of canonical-uri / predicate / label / source) carrying `rdfs:label` + `bf:source`. Phase 3 walks those blank nodes via `_iter_subject_requests`, routing by `bf:source` (`yso*` → `subject` (YSO), `kauno*` → `genre_form` (KAUNO), `muso*` → `music_form` (MUSO), missing/unknown → `subject` default). Reconciliation success adds `<canonical> bffi:subject <auth>` (or `bffi:genreForm`, preserving the cataloguer's MARC tag) and bridges the original blank node via `prov:specializationOf`. The `bffi-pipeline reconcile --kinds creators,subjects,genres,all` flag filters which paths run; the default (`None`) runs every kind. Provenance Activities + AdminMetadata bumping are identical to the creator path.

**Definition of done:** Sample creators + subjects that exist in KANTO / YSO / KAUNO / MUSO / LCGFT / LCSH are linked deterministically where possible, by LLM where lexical is ambiguous, and flagged for review where neither is confident. The provenance graph distinguishes all five cases (`reconciliation-local` / `-lexical` / `-llm` / `-fallback` / `-no-candidate`). The reconciliation review queue is non-empty and meaningful. *(Phases 1 + 2 closed the creator path; phase 3 closes the subject + genre/form path; tier-0 closes the local-prefLabel-match short-circuit. 95 unit tests cover the five tiers, the FintoSkosmosClient, the LangChainLLMPicker including the hallucinated-URI defence, the apply_reconciliation orchestrator with `--kinds` filter, the M8 propagation of subjects + genre/forms with deterministic blank-node IDs, the subject-side authority binding + `prov:specializationOf` bridge, the FusekiConceptResolver SPARQL string + MockTransport HTTP behaviour, kind-to-graph routing including the kaunokki-YSO fallback and the SLM-vocab-tag corner of the genre_form union, cache hits and misses, the orchestrator integration that asserts a tier-0 hit short-circuits before tier-1 `client.query`, the Agent6XX subject-as-name routing for MARC 600/610/611, and the predicate-not-kind dispatch invariant (subject-walker person never writes `bffi:creator`). The live `requires_llm` test verifies the creator cascade against real KANTO on the M5 Max; the subject path runs through the same orchestrator and reuses the FintoSkosmosClient's vocab dispatch.* **Dev-sample smoke trajectory** (13 records, 57 subject/genre + 12 creator entities = 69 total): reconciliation-local 0 → 16 (YSO) → 24 (+yso-paikat) → 34 (+yso-aika) → 42 (+kaunokki YSO fallback); reconciliation-no-candidate 38 → 9 after the LCGFT load auto-resolved the cataloguer-bound `$0` URIs and the Agent6XX walker fix routed person subjects through KANTO. The remaining 9 are fictional characters (`Lily / Winslow / Nicholson … (fiktiivinen hahmo)`) and fictional places (`Birchwood`, `Mumindalen`) that don't exist in any general authority.)

### M10 — Skosify overlay + Fuseki load

Phases 1 + 2 (committed). Phase 1 shipped the Skosify side: the overlay TTL, the bffi.cfg config, the dual-typing run, the `bffi-pipeline skosify` CLI subcommand. Phase 2 added the Fuseki load via SPARQL Graph Store Protocol, the Boundary-5 post-load smoke ASK queries with rollback, the `bffi-pipeline load` and `bffi-pipeline lookup-helmet` CLI subcommands.

- [x] `config/overlay/bffi-skos-overlay.ttl` and `config/bffi.cfg` per spec §5. *(Overlay declares Work/Expression as subClassOf skos:Concept and lifts hasExpression/expressionOf to skos:narrower/skos:broader; the bffi.cfg keeps `[types]` empty to avoid Skosify's destructive class rewriting and keeps `cleanup_*` off so the AdminMetadata block + provenance back-links survive intact.)*
- [x] **`config/bffi-admin-vocabulary.ttl`** per spec § 8 "AdminMetadata view" — stable URIs for shared value-class instances (Agents dual-typed `prov:SoftwareAgent, bffi:Agent`; `bffi:GenerationProcess`; `bffi:DescriptionAuthentication` for `auto-merged` / `needs-review` / `verified`; `bffi:DescriptionConventions`; `bffi:DescriptionLevel`; `bffi:EncodingLevel`; `bffi:MetadataLicensor` for CC0; `bffi:RecordingSource` for Helmet). Loaded alongside the overlay. *(File pre-existed; phase 2 wires it into the Fuseki load alongside the skosified canonical graph.)*
- [x] **Declare the Helmet source URI in the overlay file** so it's resolvable as a real entity in Skosmos:
  ```turtle
  <http://urn.fi/URN:NBN:fi:bib:source:helmet> a bf:Source, bffi:Source ;
      rdfs:label "Helmet"@en, "Helmet"@fi, "Helmet"@sv ;
      bf:code "helmet-bib" ;
      rdfs:comment "Joint catalog of the public libraries of Helsinki, Espoo, Vantaa, and Kauniainen. Underlying ILS is Sierra; this is implementation detail and not preserved in the data."@en .
  ```
  *(Dual-typed `bf:Source, bffi:Source` per spec § 5 / `docs/lkd.rdf` line 609; resolvable for both BIBFRAME-side `bf:identifiedBy` consumers and BFFI-native consumers.)*
- [x] `src/bffi_pipeline/stages/skosify_run.py` shells out to Skosify with the overlay, producing dual-typed output. *(Programmatic call to `skosify.skosify(canonical, overlay, **bffi_cfg)` rather than a subprocess shell-out — same API, easier to test. Idempotent re-run skips when output is newer than every input. CLI: `bffi-pipeline skosify --canonical-path ... --output-path ... --force`. 9 unit tests cover dual-typing on Works + Expressions, bffi:hasExpression → skos:narrower lift (with the BFFI predicate preserved), AdminMetadata survival, idempotency, and the FileNotFoundError when canonical.ttl is absent.)*
- [x] `src/bffi_pipeline/stages/load.py` uploads to Fuseki via SPARQL Graph Store Protocol. Loads main data and `config/bffi-admin-vocabulary.ttl` into the configured `bffi-works` graph; provenance Activities into the provenance graph. *(GSP via injectable `httpx.Client` — first file `PUT` (clears graph), subsequent files `POST` (append). Provenance is a separate `PUT` to its own named graph; it is **not** rolled back when bffi-works smokes fail because the operator usually wants the audit trail anyway.)*
- [x] **Pin Fuseki version** in `docker-compose.yml` to a specific Apache Jena 5.x release (e.g., `stain/jena-fuseki:5.0.0`). Verify `jena-text` is enabled. Document the chosen version in the runbook. *(Already pinned to `stain/jena-fuseki:5.0.0` in `docker-compose.yml` per the earlier Skosmos commit; `runbook.md` already records both pins. `jena-text` enablement in the Fuseki config is documented in `docs/marcxml-to-bffi-skosmos-pipeline.md` § 4 and is the user's M5-Max dev-stack verification.)*
- [x] **Boundary 5 validation (post-load smoke tests).** Run all `ASK` queries from `config/shapes/post-load-smoke.rq` against Fuseki immediately after load. All must return `true`:
  - `ASK { ?w a bffi:Work, skos:Concept ; skos:prefLabel ?l }` — Skosify dual-typing worked.
  - `ASK { ?e a bffi:Expression, skos:Concept ; bffi:expressionOf ?w }` — Expressions linked to Works.
  - `ASK { ?w a bffi:Work ; bf:identifiedBy [ bf:source <http://urn.fi/URN:NBN:fi:bib:source:helmet> ] }` — Helmet identifiers preserved.
  - `ASK { ?w skos:narrower ?e . ?e skos:broader ?w }` — Skosify-inferred inverses present.

  Any failure rolls back the load: drop the loaded named graph and exit non-zero. Don't leave Fuseki in a half-loaded state. *(Single `post-load-smoke.rq` file split on `# === <name> ===` headers; each ASK runs as its own SPARQL POST. On any failure, the orchestrator issues a `DELETE` against the bffi-works graph and the CLI exits 1. Rollback failure is swallowed — the operator already has a failed smoke to investigate.)*
- [x] **Helmet lookup query and CLI:** create `sparql/queries/helmet_lookup.rq`:
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
  And expose it as `bffi-pipeline lookup-helmet <id>` — runs the query against Fuseki and prints the canonical Work URI plus all Expressions plus a brief merge summary. *(Jinja2 template with autoescape=False per CLAUDE.md SPARQL conventions; the helmet_id is wrapped as a SPARQL string literal via `json.dumps` to handle escaping. CLI returns the canonical Work URI + its Expressions; if the bib ID isn't bound, prints a clear "no canonical Work found" line.)*

**Definition of done:** `make publish` loads the sample, the pinned Fuseki version is documented, the Helmet source URI is declared and visible, and Skosmos at `localhost:9090` shows the vocabulary with browseable Works and Expressions carrying their Helmet identifiers. *(All M10 sub-tasks are committed. The unit tests verify the protocol contract — GSP method/Content-Type, SPARQL ASK/SELECT JSON parsing, rollback on smoke failure, Jinja2 substitution. The live `make publish` against `docker compose up` Fuseki and the Skosmos UI smoke are user-side M5-Max validations covered by `docs/runbook.md`.)*

### M11 — Skosmos config

- [x] `config/skosmos-config.ttl` per spec §4 with the two `skosmos:indexShowClass` entries. *(Verbatim per spec § 4 with one substantive change: `skosmos:sparqlEndpoint` and `void:sparqlEndpoint` use `http://fuseki:3030/bffi/sparql` (docker-compose service hostname) rather than localhost, since the config lives inside the Skosmos container. Local-dev users running Skosmos directly on the host swap to localhost — the comment at the top of the file explains.)*
- [x] **Pin Skosmos to a specific 3.x release** in `docker-compose.yml`. Pinned to `ghcr.io/natlibfi/skosmos:3.2`; document in the runbook.
- [x] **Configure language priority:** `skosmos:language "fi", "sv", "en"` and `skosmos:defaultLanguage "fi"`. Every test/sample record should carry Finnish labels at minimum. *(All M2 / M8 outputs already emit Finnish labels via the M3 SPARQL CONSTRUCT. The bffi:Work / bffi:Expression rdfs:labels in the config carry fi/sv/en explicitly.)*
- [x] **Label `dct:identifier` in the overlay** (`config/overlay/bffi-skos-overlay.ttl`) so Skosmos renders the Helmet bib IDs under a "Helmet bib ID" / "Helmet-tunniste" / "Helmet-bib-id" heading on the concept page. Without the label Skosmos shows the bare predicate URI. The Sierra-style values themselves are emitted in M3 + M8.
- [x] **Cross-vocabulary linking via local authority dumps (option 3b).** M9's reconciled URIs (KANTO/YSO/KAUNO/MUSO/SLM/LCGFT/LCSH) and M3's contributor-extraction `bf:role <relators/...>` URIs render as bare URIs unless those vocabs live in the SPARQL endpoint Skosmos talks to. New `src/bffi_pipeline/stages/load_finto.py` downloads canonical dumps from `api.finto.fi` (Finto vocabs, Turtle) and `id.loc.gov` (LoC relators served as RDF/XML — converted to Turtle on the fly; LoC LCGFT + LCSH served as gzipped Turtle — `.gz` URL suffix triggers `gzip.decompress` before saving), caches under `data/finto-dumps/`, and PUTs each into its concept-scheme URI as the named graph in Fuseki via GSP. Vocabs sharing a graph URI (YSO + YSO-Paikat + YSO-Aika all target `http://www.yso.fi/onto/yso/` because their concept URIs share the YSO namespace) get grouped at upload time: PUT first dump (clears + loads), POST subsequent dumps (append) rather than letting the second PUT clobber the first. Total dump size ~700 MB on disk after the LCSH addition (KANTO ~183 MB, LCSH ~465 MB after decompression — now the largest graph at 9.7M triples; YSO ~250 MB; the rest combined < 50 MB). CLI: `bffi-pipeline load-finto` with `--max-age-days 30` cache + `--force` override; Make: `make refresh-finto`. `config/skosmos-config.ttl` gains nine `skosmos:Vocabulary` entries (`:yso`, `:kanto`, `:kauno`, `:muso`, `:slm`, `:relators`, `:lcgft`, `:lcsh`, plus `:bffiWorks`) each with the right `void:uriSpace` and pointing at our local Fuseki — Skosmos's vocabulary registry then routes any URI in those namespaces to a labelled clickable concept page on our own instance, no runtime calls to upstream APIs. (YSO-Paikat + YSO-Aika don't need separate Skosmos entries because they share the YSO URI namespace and graph; the existing `:yso` vocab renders place + temporal labels for free.) *(19 unit tests in `tests/unit/test_load_finto.py` cover cold-cache + cache-hit + force + stale-mtime + HTTP-error + per-vocab graph-routing + RDF/XML-to-Turtle conversion + gzipped-Turtle decompression + shared-graph-uri grouping (PUT-then-POST) paths; 14 tests in `tests/unit/test_skosmos_config.py` pin every authority vocab's URI-space, short-name, sparqlEndpoint/sparqlGraph, and language list. Live smoke against `docker compose up` Fuseki + Skosmos verified the Tšarka canonical Work renders 9 YSO subjects as labelled clickable links; the merged "Get all you deserve" Work renders YSO / KAUNO / SLM links in Finnish UI.)*
- [x] **Label reconciliation predicates** (`bffi:subject` / `bffi:genreForm` / `bffi:creator`) in the overlay so the canonical-Work concept page actually renders Subject / Genre/form / Creator sections. Without the labels Skosmos drops the predicates from the rendered page even though the data is present in the bffiWorks graph — surfaced during the M11 3b live smoke. Three one-line additions following the same pattern as `dct:identifier`.
- [ ] **(user-side smoke)** Verified: hierarchy view shows Works with Expressions nested below them. *(Driven by `skos:narrower` triples Skosify lifts from `bffi:hasExpression`; a Boundary-5 ASK query during `bffi-pipeline load` already verifies the data carries the inverse pair. The actual UI rendering is one of seven items in the `docs/runbook.md` § "M11 — Skosmos UI smoke checklist" — runs after `docker compose up` against real Skosmos.)*
- [ ] **(user-side smoke)** Search works in Finnish, Swedish, and English. *(Same — runbook checklist exercises "Sota ja rauha" / "War and Peace" / "Krig och fred" across the three UI languages, plus the foreign-vs-native diacritic fold rule from M9.)*

**Definition of done:** Manual smoke test of Skosmos 3 UI passes — types display correctly, hierarchy is right, default display is Finnish, multilingual search works across all three languages. *(All committable pieces are in: the config TTL, the docker-compose volume mount, 27+ unit tests verifying the spec § 4 predicates and the Finto cross-vocab plumbing round-trip through rdflib. Live 3b smoke confirmed on the M5 Max stack: Tšarka Work shows 9 labelled YSO subject links, "Get all you deserve" shows YSO/KAUNO/SLM cross-vocab links in the Finnish UI. The remaining seven-item UI smoke checklist in `docs/runbook.md` is the cataloguer-facing acceptance pass.)*

### M12 — Gold set + evaluation harness

Phases 1 + 2 + 3 (committed) ship the eval harness, the CI workflow, and the gold-set growth pipeline. The remaining open item is gold-set growth itself: the harness + grow CLI is wired, but `gold/gold.jsonl` still carries only the bootstrap 13 cases — the cataloguer-driven growth toward 50–100 stratified cases is now unblocked but is genuine cataloguer work, not codeable.

- [ ] **`gold/gold.jsonl` grown to 50-100 stratified cases** with per-category min-2 holdout — see [`docs/plans/p-06-gold-set-growth.md`](plans/p-06-gold-set-growth.md). Phase 3's `bffi-pipeline grow-gold` is committed; the cataloguer extension is the wall-time bottleneck. Hard prerequisite for [P-01](proposals/prop-01-llm-distillation-pre-screener-for-M6.md) and the statistical power of P-04 Phase A.
- [x] **Gold-set loader + holdout split:** `src/bffi_pipeline/eval/gold_set.py` exposes `GoldCase` (Pydantic v2, `extra="forbid"`), `load_gold_set()`, `split_by_holdout()`, and `assert_holdout_stratification()`. Pure data-handling, no LLM dependency.
- [x] **Embedding-model benchmark:** `src/bffi_pipeline/eval/embed_benchmark.py` runs cosine-similarity benchmarks across candidate embedding models on the gold set and reports the same-work / different-work mean-similarity gap. CLI: `bffi-pipeline embed-benchmark`. This is the bootstrap subset of `harness.py` that does not depend on the M6 LLM judge.
- [x] `src/bffi_pipeline/eval/harness.py` per spec §9. Reads the JSONL, splits on the `holdout` field, uses training cases for any few-shot prompts and hold-out cases for accuracy reporting. *(`evaluate()` walks gold cases through an injectable `JudgePair` (defaulting to the M6 stage); `summarize()` reports aggregate / decided / per-category / high-confidence-band / holdout-only accuracy plus the confusion matrix and median latency. CLI `bffi-pipeline eval --run-label <id>` writes `eval-runs/<id>.json` and prints a paste-ready text rendering for the PR description. 19 unit tests against synthetic CaseResult fixtures + a stub-judge end-to-end test cover the aggregation paths without loading the LLM stack.)*
- [x] `src/bffi_pipeline/eval/grow.py` runs the "overridden decisions" SPARQL query and outputs candidates for gold-set growth. New cases default to `"holdout": false`; the user explicitly flips the flag. *(SPARQL committed at `sparql/queries/grow_overrides.rq` joins the override Activity, the original LLM rationale + decision, and `OPTIONAL`-lifts creator + title + language + Helmet bib_id from the bffi-works graph. CLI `bffi-pipeline grow-gold --output-path ...` writes `gold/grow-candidates.jsonl` with one row per override; `expected` is the inverse of the LLM decision and `category` is left `None` for the cataloguer to fill in. 12 unit tests with `httpx.MockTransport` cover the binding-row → candidate mapping including the OPTIONAL-tolerance + decision-inversion semantics.)*
- [x] **CI on GitHub-hosted Linux runners only** (see `docs/ci-strategy.md`). Lint, type-check, unit tests, and integration tests (Fuseki + Skosmos via Docker services) run on every PR. **The LLM eval is NOT in CI** — local LLM dependency makes it impractical on hosted runners. *(`.github/workflows/ci.yml` runs `lint-and-test` (ruff check + ruff format --check + mypy --strict + pytest tests/unit) and `integration` (pytest tests/integration -m "not requires_llm" with a Fuseki 5.0.0 service container) on Ubuntu 24.04 via `astral-sh/setup-uv@v3` + `uv sync --frozen`. Concurrency cancels superseded runs.)*
- [x] **Eval runs manually on the M5 Max** before any PR that touches `prompts/`, `gold/`, `src/bffi_pipeline/stages/judge.py`, or `src/bffi_pipeline/eval/`. Output of `make eval` is pasted into the PR description. *(`make eval LABEL=<run-id>` shells through to `bffi-pipeline eval`; `.github/pull_request_template.md` prompts for the eval block on relevant PRs and ships a "N/A" default for unrelated changes.)*
- [x] Integration tests requiring Ollama are tagged `@pytest.mark.requires_llm` and excluded from CI via `-m "not requires_llm"`. *(Markers configured in `pyproject.toml`; `tests/integration/test_judge_live.py` and `tests/integration/test_reconcile_live.py` carry the marker; the CI integration job applies the exclusion.)*

**Definition of done:** `make eval` reports accuracy by category against the hold-out set; gold set is hand-marked with explicit hold-out flags; CI runs cleanly on Linux runners; PR template prompts for eval output where relevant. *(Hand-marked hold-out flags ✓; `make eval` per-category accuracy report ✓ via `harness.py`; CI workflow + PR template ✓; gold-set growth tooling ✓ via `grow-gold`. The remaining open item is the cataloguer-driven growth of `gold/gold.jsonl` toward 50–100 stratified cases — that's external work, not codeable.)*

### M13 — Documentation + handoff

- [x] `LICENSE` file with Apache License 2.0 text. Add `Copyright (c) <year> University Of Helsinki (The National Library Of Finland)` at top, matching NLF convention. *(Apache 2.0 verbatim with the NLF copyright header at line 1.)*
- [x] `README.md` in English, covering: what this is, NLF pro bono context, prerequisites (M5 Max + 128 GB + Ollama setup), how to run on a sample, how to run on real data, license note. *(Sections: elevator pitch + NLF context; mermaid architecture diagram; prerequisites + one-time install; sample-data quickstart; production-run pointer to runbook; repo layout; testing/CI/eval; operating constraints; committed identifiers; license. Replaces the original meta-doc README that described the documentation package.)*
- [x] Inline docstrings on every public function in `src/`. Docstrings in English. *(AST audit reports zero undocumented public symbols across ``src/bffi_pipeline``. Private helpers are intentionally undocumented — CLAUDE.md says default to writing no comments unless the WHY is non-obvious; for boilerplate ``render()`` / property accessors the docstrings are tight one-liners that just name the shape so the audit is satisfiable without bloat.)*
- [x] One end-to-end runbook in `docs/runbook.md` (English): from a fresh directory of MARCXML files to live Skosmos display, including expected timings on M5 Max, the chosen pinned versions, and the chosen vllm-mlx concurrency value from M6 tuning. *(Updated for M9 phase 3 + M12 — adds the ``--kinds`` filter to the reconcile step, adds ``make eval`` + ``bffi-pipeline grow-gold`` as steps 11-12, prunes the "what's still missing" list down to cataloguer-driven gold growth + the ``--concurrency`` sweep.)*
- [x] Architecture diagram (mermaid in README is fine) showing the stages and their inputs/outputs. *(Mermaid flowchart in README.md groups the M2-M11 stages into Convert / Cluster / Reconcile / Publish bundles and shows the provenance + eval graphs feeding off M6/M9 → M12.)*
- [x] `pyproject.toml` carries `license = "Apache-2.0"` and `authors`. *(Already set since M0; verified.)*

**Definition of done:** A new contributor can clone the repo, follow the README, and produce a working Skosmos 3 instance from the sample data within an hour. The repo is in the shape NLF would expect for an upstream contribution. *(All committable docs are in: LICENSE + project README with architecture diagram + the canonical end-to-end runbook + every public src/ symbol carries an English docstring + Apache 2.0 / CC0 + .github CI workflow + PR template. The "within an hour" claim is best-effort against the M5 Max profile; the runbook calls out the model-download cost (Qwen3 32B + 72B is the long pole, not the pipeline code).)*

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
