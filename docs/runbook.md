# Production runbook

End-to-end recipe for running the BFFI pipeline against the ~800 k-record Helmet corpus on the M5 Max. Five sections — setup, observability, running, then two reference tables (Make + CLI). Anything not here lives in [`docs/local-inference.md`](local-inference.md) (mlx-lm install), [`docs/observability.md`](observability.md) (metric vocabulary), or `bffi-pipeline <cmd> --help`.

## 1. Setup: docker-compose + LLM

### Container stack (Fuseki + Skosmos)

```sh
docker compose up -d fuseki skosmos     # core data services (always on)
```

Verify:
```sh
curl -fsS http://localhost:3030/$/ping    # Fuseki  → "pong"
curl -fsS http://localhost:9090/          # Skosmos → 200
```

`make` auto-detects `podman` as a `docker` substitute; the bundled `docker-compose.yml` is podman-compatible.

### LLM servers (mlx-lm)

Full install in [`docs/local-inference.md`](local-inference.md#installation). Operationally:

```sh
# In a separate terminal each (or backgrounded with nohup):
scripts/start-mlx-lm.sh                                    # primary on :8001 (Qwen3-8B)
scripts/start-mlx-lm.sh --port 8002 --model "$LLM_MODEL_FALLBACK"  # fallback on :8002 (Qwen3-32B)
```

Verify:
```sh
curl -fsS http://127.0.0.1:8001/v1/models                  # primary  → JSON
curl -fsS http://127.0.0.1:8002/v1/models                  # fallback → JSON
```

`.env` must carry `LLM_BASE_URL_PRIMARY`, `LLM_BASE_URL_FALLBACK`, `LLM_MODEL_PRIMARY`, `LLM_MODEL_FALLBACK`. Copy from `.env.example`.

### Vocabulary dumps (one-time)

```sh
make refresh-finto                       # downloads + loads KANTO/YSO/KAUNO/MUSO/SLM into Fuseki
```

---

## 2. Observability

One command:

```sh
make observability-up
```

This starts the four observability components together:
- **Prometheus** (TSDB) — scrapes :9100 every 5s
- **Grafana** (dashboard) — bundled `bffi-pipeline` dashboard auto-provisioned
- **Caddy** (reverse-proxy + file-server) — single entry point at `http://localhost:8080`
- **serve-metrics** (host process) — tails `runs/*/stage-events.jsonl`

All operator-facing UI lives at **`http://localhost:8080`**:

| Path | What |
|---|---|
| `/` | Bundled dashboard; pick the active run from the top dropdown |
| `/prometheus/` | Ad-hoc PromQL |
| `/files/` | Browse `runs/<uuid>/` — cataloguer TSVs, export tarballs, per-record artifacts |

Stop everything: `make observability-down`. Wipe Prometheus TSDB without restarting: `make observability-reset-prometheus` (operator-side caveats in the target's inline comment).

Metric vocabulary: [`docs/observability.md`](observability.md).

---

## 3. Running with the runner

The canonical entry point chains M2 → M3 → M5 → M6 → M8 → M9 → Skosify → Load in one Python process under a single run UUID, with the dashboard's four-state model (pending / running / done / skipped) lighting up correctly:

```sh
uv run bffi-pipeline run \
  --input-dir marcxml/samples/helmet/500/marcxml \
  -d "p-30-audit-baseline-500-2026-05-14"
```

The runner mints its own UUID and prints it at startup. Stage events flow to `runs/<uuid>/stage-events.jsonl`; serve-metrics picks them up automatically.

### Key flags

| Flag | What |
|---|---|
| `-i / --input-dir` | MARCXML source for M2 (required unless M2 is skipped) |
| `-d / --description` | Free-text run label; surfaces in Grafana's header tile |
| `--stages "m8,m9,export"` | Restrict to a stage subset (e.g. resume after M5+M6 cached) |
| `--from-stage m6` | Resume from this stage; preceding stages emit a `skipped` event with reason `resume-from-stage` |
| `--skip "skosify,load"` | Skip these stages; emit `skipped` with reason `operator-skipped` |
| `--force-stages "m3,m6"` | Pass `--force` (or `--restart` for M6) to these stages |

### Bundle the output for handoff

`bffi-pipeline run` writes the export tarball at the end of the canonical chain (the `export` stage is on `CANONICAL_STAGES`); operators don't run a separate export step. Output goes to `runs/<uuid>/bffi-export-<uuid>.tar.gz`.

### Single-stage runs (debug / iteration)

Use `bffi-pipeline run --from-stage <stage> --force-stages <stage>` to re-run an individual stage. The nine per-stage CLI commands (`marc-to-bf`, `bf-to-bffi`, `embed`, `judge`, `merge`, `reconcile`, `skosify`, `load`, `export`) were removed in P-38 Phase C-2; operators who try the old invocations get a discoverable migration message pointing at the new path. Tuning knobs that used to live on per-stage flags (e.g. `judge --concurrency`) are now namespaced env vars (`M6_CONCURRENCY`, `M9_CONCURRENCY`, etc.) — see `.env.example` and `docs/local-inference.md`.

---

## 4. `make` commands

| Target | Purpose |
|---|---|
| `make help` | List every target |
| `make lint` | `ruff check && ruff format --check && mypy --strict` |
| `make format` | `ruff format + ruff check --fix` |
| `make test` | `pytest tests/ -m "not requires_llm"` |
| `make test-integration` | Stub — not implemented |
| `make eval LABEL=<id>` | Run the M12 gold-set evaluation locally |
| `make refresh-finto` | Download Finto vocab dumps + load into Fuseki |
| `make observability-up` | Start Prometheus + Grafana + Caddy + serve-metrics |
| `make observability-down` | Stop them all |
| `make observability-reset-prometheus` | Wipe the Prometheus TSDB via the admin API |
| `make clean-caches` | Remove the M6 judge + M9 picker SQLite caches |
| `make install-hooks` | Force re-install the `.githooks/` pre-commit hook |
| `make convert` / `make publish` | Stubs — not implemented; use `bffi-pipeline run` |

A pre-commit hook gating `make lint && make test` on staged `*.py` files installs automatically on first `make` invocation per clone.

---

## 5. `uv run bffi-pipeline` commands

`uv run bffi-pipeline --help` lists everything; below is the operator-facing subset grouped by purpose.

### Pipeline orchestration

| Command | Purpose |
|---|---|
| `run` | Chain M2 → … → Load in one process (the canonical entry — see § 3) |
| `plan` | Declare the planned stage set for the current run (used by the runner internally) |
| `status` | Render the current pipeline state from `stage-events.jsonl` (`--tail` for live) |
| `export` | Bundle the M9-finalised BFFI as a CC0 tarball |
| `runs` | Run lifecycle management — `list`, `prune`, `tag`, `info`, `mark-complete`, `clear-fuseki` |

### Individual stages (debug / iteration)

| Command | Stage | Output |
|---|---|---|
| `marc-to-bf <dir>` | M2 | `bibframe/<bib_id>.rdf`, `helmet-map.jsonl`, `bibframe/_errors.tsv` |
| `bf-to-bffi` | M3 | `bffi/<bib_id>.ttl`, `bffi/_validation.tsv` |
| `workkey-stats <path>` | M4 | Block-size histogram (prints, no file) |
| `embed` | M5 | `embeddings.faiss`, `embeddings.idmap.json`, `embed-candidates.jsonl` |
| `embed-stats` | M5 reporting | Band counts + similarity histogram |
| `embed-benchmark` | M5 / M12 | Compare embedding models on the gold set |
| `judge` | M6 | `judge-decisions.jsonl`, `judge-cache.sqlite`, provenance |
| `merge` | M8 | `canonical.ttl`, `canonical-map.jsonl`, `canonical-conflicts.jsonl`, `canonical-mint-failures.tsv` |
| `reconcile` | M9 | Writes back to `canonical.ttl`, `reconcile-cache.sqlite`, provenance |
| `skosify` | M10 phase 1 | `canonical-skosified.ttl` |
| `load` | M10 phase 2 | Fuseki upload + Boundary-5 smoke |

### Auxiliary

| Command | Purpose |
|---|---|
| `load-finto` | Refresh KANTO/YSO/KAUNO/MUSO/SLM named graphs in Fuseki |
| `lookup-helmet <bib_id>` | Resolve a Helmet bib_id to its canonical Work + Expressions |
| `eval` | Score the gold set against the M6 judge (M12) |
| `grow-gold` | Promote human-overridden judge decisions into the gold set (M12 phase 3) |
| `ysa-disambiguation-report` | Cataloguer-review CSV for YSA → YSO disambiguation residue |
| `provenance` | Provenance graph maintenance (M7); `compact --older-than 90d` is the common operation |
| `serve-metrics` | Prometheus exporter (`make observability-up` manages this; manual invocation rarely needed) |

---

## Provenance compaction (quarterly cron)

Suggested:

```cron
0 4 1 */3 *  cd /path/to/bffi-pipeline && uv run bffi-pipeline provenance compact --older-than 90d >> ~/Library/Logs/bffi-compact.log 2>&1
```

Quarterly is a comfortable margin under the 90-day staleness floor.

## M11 — Skosmos UI smoke checklist

After a successful `bffi-pipeline run` lands data in Fuseki, point a browser at `http://localhost:9090/` (Skosmos default) and verify:

1. The bundled BFFI vocabulary is listed on the Skosmos landing page.
2. Search for a known cataloguer term (e.g. `"sota"`) returns canonical Works with localised labels.
3. Each Work page shows its primary author + subjects + genre/form, with KANTO/YSO/KAUNO/MUSO URIs resolving.
4. Language switcher renders `fi`, `sv`, `en` labels correctly.
5. Provenance tab links to the per-record `bffi-prov:Activity` chain (M7 served via Fuseki).
6. Cataloguer-supplied authority URIs (P-15) render as Skosmos cross-references, not re-reconciled labels.
7. AdminMetadata block shows `descriptionCreationDate` + `descriptionAuthentication` (fully-resolved / needs-review) per spec § 8.

A failed item is a P-30 truth-table candidate — the pipeline's RDF is correct, but a downstream consumer (Skosmos) renders it wrong.

## What's still missing

- Cataloguer-driven gold-set growth (`gold/gold.jsonl` is at 13 bootstrap cases; target 50–100 stratified).
- The post-fix re-bench on a fresh helmet-5k under P-36 Phase C (subject-label SPARQL routing).
- The full P-30 observability audit pass — surfaces inventoried but drift checks not yet run.
