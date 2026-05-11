# Production runbook

End-to-end recipe for running the BFFI pipeline against the
~800 k-record Helmet corpus on the M5 Max. M0-M12 are committed;
the surface-level checks of the Skosmos UI (M11) are user-side
smoke checks documented near the bottom of this file.

This is the *canonical* sequence — start here, not from individual
milestone notes.

## Pinned versions

| Component | Pinned | Where |
|---|---|---|
| `marc2bibframe2` | `third_party/marc2bibframe2` (git submodule) | M2 |
| Embedding model | `BAAI/bge-m3` (1024-dim, multilingual) | M5; benchmark via `bffi-pipeline embed-benchmark` |
| FAISS HNSW | `M=32`, `efConstruction=200`, `efSearch=64`, IP metric on L2-normalised vectors | M5 |
| LLM primary | `qwen3:32b-q4_K_M` (Ollama) / equivalent MLX 4-bit | M6 |
| LLM fallback | `qwen2.5:72b-instruct-q4_K_M` | M6 cascade — Qwen3 has no 72B size; cascade steps to previous-gen 72B |
| Skosmos | `third_party/Skosmos` (git submodule pinned at `v3.2`) | M11; built from source via `docker-compose build` since NatLibFi doesn't publish a Docker image |
| Apache Jena Fuseki | `5.4.0` (Maven JAR downloaded by Skosmos's vendored `dockerfiles/jena-fuseki2-docker/Dockerfile`) | M10 (`docker-compose.yml`); pinned to the same value Skosmos 3.2 commits to in its own compose file |

Override via environment / `.env`:

```
LLM_BASE_URL=http://localhost:11434/v1     # Ollama default
LLM_MODEL_PRIMARY=qwen3:32b-q4_K_M
LLM_MODEL_FALLBACK=qwen2.5:72b-instruct-q4_K_M
BFFI_DATA_DIR=./data
```

Local-LLM install (Ollama for development, mlx-lm for production
batches), model pulls, and the verification probe live in
[`docs/local-inference.md`](local-inference.md#installation).

## Throughput expectations on the M5 Max

| Stage | Mode | Time on 800 k records | RAM peak |
|---|---|---|---|
| M2 MARCXML → BIBFRAME | one-shot | ~15-30 min (XSLT-bound) | small |
| M3 BIBFRAME → BFFI | one-shot | ~10-20 min | small |
| M4 Stage-1 blocking | one-shot | seconds | small |
| M5 embedding build | sentence-transformers `mps` | 30-60 min | ~5 GB index + 2.5 GB model |
| M5 candidate query | top-k=20 | seconds | reuses index |
| M6 cascade | Ollama serial | 70-170 hours per 50 k pairs | ~20 GB primary; +40 GB if loading fallback |
| M6 cascade | mlx-lm batched | 10-25 hours per 50 k pairs | same |

Two things to plan around:

1. **Tighten the gray zone before kicking off M6.** Spec § 6 commits
   to ≥ 0.90 / ≤ 0.78 thresholds; the embed-stats output tells you
   how many pairs land in each band. If "escalate" is > 100 k pairs,
   re-tighten before committing to a multi-night run.
2. **mlx-lm + concurrency for production.** Ollama is fine for
   gold-set runs and the few-hundred-pair test sweeps. Production
   passes use `--concurrency` ≥ 4 and the mlx-lm server.

## Source-data export — Helmet Sierra Postgres replica

Upstream of M2 sits `marcxml-export-sierra`
([`src/marcxml_export_pipeline/sierra/`](../src/marcxml_export_pipeline/sierra/)),
which streams the Sierra Postgres replica and writes one MARCXML
file per non-suppressed bib record. The exporter synthesises
MARC 001 (from `record_num`), 003 (`FI-HELME`), 005 (record-
modified timestamp), and 907 (`.b<num><check>`) when the source
varfields lack them — this is what keeps marc2bibframe2's
work-key contract clean downstream (records with a blank 001 get
clustered into a single bogus canonical Work, the "SupaRed"
incident).

The export and the rest of the pipeline are decoupled — the
exporter writes MARCXML to disk and the downstream stages read
from disk. The driver script
[`scripts/run-sierra-export.sh`](../scripts/run-sierra-export.sh)
gates the full corpus run behind a smoke-export and a local
validation pass:

```bash
# .env should carry: DB_HOST / DB_PORT / DB_USER / DB_PASSWORD /
# DB_NAME (Sierra Postgres replica) and optionally
# MARCXML_EXPORT_AGENCY_CODE (defaults to FI-HELME).

# 1. Pre-flight: seed local Fuseki with vocab dumps and confirm
#    the Ollama judge models are pulled.
uv run bffi-pipeline load-finto
ollama list                       # expect the LLM_MODEL_PRIMARY/FALLBACK tags

# 2. Smoke export + local validation, no full export yet.
scripts/run-sierra-export.sh
# → /tmp/sierra-smoke/<bib_id>.xml (500 rows)
# → /tmp/sierra-smoke-validated/bibframe/*.rdf via marc2bibframe2
# → stops with "STOP" banner unless --confirm-full is passed.

# 3. If smoke + validate are green, gated full export:
scripts/run-sierra-export.sh --confirm-full
# → ./marcxml/sierra/<bib_id>.xml for every non-suppressed bib
#   (~800 k rows; 1-2 h on a healthy replica).
```

The exporter is shipped as a sibling Python package
(`marcxml_export_pipeline.sierra`) rather than nested under
`bffi_pipeline` so future ILS sources (Koha, Alma, …) can grow
as additional sub-packages without entangling them with the
BFFI conversion code.

## End-to-end command sequence

The whole pipeline against a single record dir:

```bash
# 0. One-time benchmark to lock in the embedding model.
#    Takes ~10 min for first model download; the comparison itself
#    is seconds against gold/gold.jsonl (~13 pairs at bootstrap).
bffi-pipeline embed-benchmark
# → reports same_work / different_work mean similarity per
#   {BGE-M3, e5-large, jina-v3}, ranks by widest gap, names the winner.

# 1. M2 — MARCXML to BIBFRAME RDF/XML.
bffi-pipeline marc-to-bf <input-dir>
# → writes <BFFI_DATA_DIR>/bibframe/<bib_id>.rdf,
#   <BFFI_DATA_DIR>/helmet-map.jsonl, and
#   <BFFI_DATA_DIR>/bibframe/_errors.jsonl for any rejected files.

# 2. M3 — BIBFRAME RDF/XML to BFFI Turtle.
bffi-pipeline bf-to-bffi
# → writes <BFFI_DATA_DIR>/bffi/<bib_id>.ttl plus
#   <BFFI_DATA_DIR>/bffi/_validation.jsonl for SHACL flags.

# 3. M4 — block-size statistics (no output file; prints histogram).
bffi-pipeline workkey-stats <BFFI_DATA_DIR>

# 4. M5 — build the FAISS HNSW index and emit candidate pairs.
#    First run downloads BGE-M3 (~2.3 GB); ~30-60 min on the M5 Max.
bffi-pipeline embed
# → writes <BFFI_DATA_DIR>/embeddings.faiss,
#   <BFFI_DATA_DIR>/embeddings.idmap.json,
#   <BFFI_DATA_DIR>/embed-candidates.jsonl,
#   prints band counts and similarity histogram.
#
# Re-runs without --force are idempotent: skipped when both files
# are newer than the input BFFI Turtle.

# 5. M6 — cascade judge over the escalate band.
#    Default --concurrency=1 (Ollama serial). Crash-safe: --resume
#    is the default and picks up from <output>.checkpoint.
bffi-pipeline judge
# Production batch (mlx-lm on :8000):
LLM_BASE_URL=http://localhost:8000/v1 \
    bffi-pipeline judge --concurrency 16
# → writes <BFFI_DATA_DIR>/judge-decisions.jsonl,
#   <BFFI_DATA_DIR>/judge-decisions.jsonl.checkpoint,
#   <BFFI_DATA_DIR>/provenance.ttl per spec § 8 (every cascade step
#   is one bffi-prov:WorkMergeDecision Activity), and
#   <BFFI_DATA_DIR>/judge-cache.sqlite (post-validation cache).

# 6. M8 — apply judge decisions, mint canonical Works.
bffi-pipeline merge
# → writes <BFFI_DATA_DIR>/canonical.ttl with merged Works
#   (one bf:identifiedBy per absorbed Helmet record + one
#   bffi:adminMetadata block per canonical Work),
#   <BFFI_DATA_DIR>/canonical-map.jsonl (canonical URI → raw URIs +
#   Helmet bib_ids, sorted for byte-stable diffs), and
#   <BFFI_DATA_DIR>/canonical-conflicts.jsonl when the judge
#   produced contradictory same/different decisions for the same
#   group (those Works are NOT silently merged — they're flagged
#   for human review).

# 7. M9 — reconcile creators + subjects against KANTO / VIAF / YSO /
#    KAUNO / MUSO (with LLM picker).
LLM_BASE_URL=http://localhost:11434/v1 \
    bffi-pipeline reconcile
# → walks canonical.ttl, queries Finto's Skosmos REST for KANTO
#   (creators), YSO (subjects), KAUNO (genre/form), and MUSO (music
#   form), falls back to VIAF for non-Finnish authors not in KANTO,
#   runs the four-tier decision per kind (lexical-direct ≥0.95,
#   llm-pick when ambiguous, fallback with needs-review tag when LLM
#   uncertain or confidence <0.80, no-candidate when nothing clears
#   the 0.70 floor), writes back bffi:creator / bffi:subject /
#   bffi:genreForm on the canonical Work + bumps AdminMetadata,
#   appends bffi-prov:Reconciliation Activities to provenance.ttl.
#
# Filter to a subset of kinds with --kinds:
#   bffi-pipeline reconcile --kinds creators        # KANTO + VIAF only
#   bffi-pipeline reconcile --kinds subjects,genres # YSO + KAUNO + MUSO
#
# After reconcile, surface the YSA → YSO bare-label residue for the
# cataloguer worklist (see "Expected reconciliation residue from the
# YSA → YSO vocabulary merge" section below):
#   bffi-pipeline ysa-disambiguation-report

# 8. M10 phase 1 — Skosify the canonical graph.
bffi-pipeline skosify
# → writes <BFFI_DATA_DIR>/canonical-skosified.ttl: bffi:Work +
#   skos:Concept dual-typing, bffi:hasExpression preserved alongside
#   the inferred skos:narrower / skos:broader, AdminMetadata +
#   provenance back-links survive intact.

# 9. M10 phase 2 — load into Fuseki, run Boundary-5 smoke ASKs.
docker compose up -d   # if not already running
bffi-pipeline load
# → uploads canonical-skosified.ttl + bffi-admin-vocabulary.ttl into
#   the bffi-works named graph, provenance.ttl into the provenance
#   graph, and runs all four smoke ASKs from
#   config/shapes/post-load-smoke.rq. On failure, the bffi-works
#   graph is rolled back (DELETE'd) and the CLI exits non-zero.
#
# Quick lookup once loaded:
bffi-pipeline lookup-helmet 2371438
# → "canonical Work: <uri> — 'Aatelisrosvo Dubrovskij'"
#   "  expression:   <uri> — '...'"

# 10. M7 — periodic provenance compaction (every ~90 days).
bffi-pipeline provenance compact --older-than 90d
# → strips bffi-prov:rawResponse from old Activities, refreshes
#   <BFFI_DATA_DIR>/provenance-meta.ttl#lastCompactedAt. CLI startup
#   prints a stderr nag once that sentinel is older than 90 days.

# 11. M12 — gold-set evaluation (manual, on M5 Max, before any PR
#     touching prompts/, gold/, judge.py, or eval/). Output is
#     paste-ready for the PR template.
make eval LABEL=qwen3-32b-prompt-v3
# → writes eval-runs/qwen3-32b-prompt-v3.json (gitignored) and
#   prints aggregate / decided / per-category / high-confidence
#   accuracy + median latency. Compare against the previous
#   main-branch run; per-category regression > 10 points is a
#   blocker.

# 12. M12 — gold-set growth from human-overridden judge decisions
#     (run monthly once production data is flowing).
bffi-pipeline grow-gold
# → reads provenance + bffi-works graphs from Fuseki, writes
#   gold/grow-candidates.jsonl (one row per override). Cataloguer
#   reviews, fills in `category`, and hand-merges promoted cases
#   into gold/gold.jsonl.
```

Each `bffi-pipeline ...` invocation runs the stale-provenance check
at startup; the warning fires once `provenance.ttl` exists *and* the
last compaction is missing or older than 90 days.

## M11 — Skosmos UI smoke checklist

After `docker compose up -d` (with the M11 config volume mount in
`docker-compose.yml`) and a successful `bffi-pipeline load`, open the
UI at http://localhost:9090 and walk through:

- **Vocabulary picker**: the "BFFI Works" / "Suomalaiset auktoriteettiteokset
  ja -ekspressiot" / "Finska auktoritetsverk och uttryck" entry is
  visible (one of the three depending on UI language).
- **Default language**: the UI lands on Finnish first; the language
  switcher at top-right shows fi / sv / en in that order.
- **Type filter**: the "Type" dropdown lists exactly two custom types,
  "Teos / Verk / Work" and "Ekspressio / Uttryck / Expression".
- **Hierarchy**: open any canonical Work — its Expressions appear
  nested below it under the "Narrower concepts" heading (driven by
  the `skos:narrower` triples Skosify lifts from `bffi:hasExpression`).
- **Helmet identifier**: scroll the Work's resource page; the
  `bf:identifiedBy` block lists each absorbed Helmet bib ID with the
  Helmet source URI rendered as a clickable label, not a dangling
  URI (the overlay's bf:Source declaration with multilingual labels).
- **Search**:
  - Search "Sota ja rauha" (Finnish form) — finds the canonical Work
    even if the cataloguer originally entered another translation.
  - Switch UI language to English, search "War and Peace" — Skosmos
    returns the same canonical Work (since the Russian-original
    Pushkin / Tolstoy work was merged across translations in M8).
  - Switch to Swedish, search "Krig och fred" (Swedish form) —
    finds the Work (when a Swedish Expression was merged in).
- **Foreign vs native diacritics** (M9 fold rule): search "Häme"
  finds Finnish Häme records; search "Hame" returns nothing similar
  (we preserve native åäö). Search "Tolstoï" returns the same as
  "Tolstoi" (foreign diacritic folded).

If any of those fail, log a bug and check:
- the Boundary-5 ASK queries from `bffi-pipeline load` — re-run with
  `--fuseki-url` pointed at your dataset and inspect output.
- the Fuseki dataset at http://localhost:3030/bffi/sparql via the
  Fuseki UI (`http://localhost:3030/`) — does the bffi-works graph
  exist? Does `SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }` show data?
- `jena-text` is enabled in the Fuseki config (required by
  `skosmos:sparqlDialect "JenaText"`).

## --concurrency tuning sweep (one-time, before the production batch)

Per spec § 7 / BUILD_PLAN M6, sweep `{4, 8, 16, 32}` against a fixed
1 k-pair sample on mlx-lm, measure throughput, and record the
chosen value here. Until the sweep runs, treat the 16-concurrency
default in the example above as a placeholder.

The recommended approach:

```bash
# Capture a 1 k-pair slice of escalate-band candidates.
head -1000 data/embed-candidates.jsonl > data/embed-candidates.sample.jsonl

for c in 4 8 16 32; do
    rm -rf data/judge-decisions.sample.jsonl* data/judge-cache.sqlite
    time LLM_BASE_URL=http://localhost:8000/v1 \
        bffi-pipeline judge \
            --candidates-path data/embed-candidates.sample.jsonl \
            --output-path data/judge-decisions.sample.jsonl \
            --concurrency $c \
            --no-provenance
done
```

Pick the value that maximises throughput without OOMing. Update this
section + the example command sequence above with the chosen value.

## Expected reconciliation residue from the YSA → YSO vocabulary merge

A 200-record corpus smoke surfaced a class of `reconciliation-no-candidate`
entries that look like tier-0 bugs but are actually cataloguing-data
quality, not pipeline issues. Document here so operators don't chase
them down twice.

**Pattern.** YSA (the pre-2018 general Finnish thesaurus) used bare
prefLabels for many lemmas that have multiple meanings — e.g. `lapset`
(both "children as an age group" and "children as family members").
During the 2014–2018 YSA → YSO merge, YSO replaced these with
**parenthetically-disambiguated** prefLabels:

| YSA bare form         | YSO disambiguated form(s)                                          |
| --------------------- | ------------------------------------------------------------------ |
| `lapset`              | `lapset (ikäryhmät)` p4354 + `lapset (perheenjäsenet)` p2357       |
| `sissit`              | `partisaanit` p8177 + `sissit (suomalaiset sotilaat)` p8175        |
| `pohjalaismurteet`    | `pohjalaismurteet (suomen kieli)` p17804 + `… (suomenruotsi)` p27707 |
| `2000-luku`           | `2000-luku (vuosikymmen)` p6200062009 + `2000-luku (vuosisata)` p6200062099 |

YSO **does not** carry the bare form (`lapset`) as `skos:altLabel` —
that's an intentional curatorial choice. Cataloguers are expected to
update records to one of the disambiguated forms; meanwhile MARC
records keep the bare YSA literal.

**What the pipeline does.** Tier-0 exact-prefLabel match correctly
misses (the bare token isn't in YSO). Tier-1 Finto prefix search
returns the 2–3 disambiguated candidates with similarity ~0.7–0.8
(below the 0.95 lexical-direct threshold). The LLM picker has no
context to choose between e.g. age-group vs. family-member sense,
so it falls through to `reconciliation-fallback` with the
canonical Work's AdminMetadata flagged
`bffi:descriptionAuthentication = <bib:auth/needs-review>` — or to
`reconciliation-no-candidate` if Finto returns nothing within the
similarity floor.

**Operational impact at corpus scale.** ~1% of YSA-tagged subjects on
the corpus sample (5–10 k records over the 800 k Helmet corpus). The
M9 review queue already surfaces them via the AdminMetadata filter:

```sparql
PREFIX bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:>
SELECT ?work ?inputLiteral WHERE {
  ?work bffi:adminMetadata/bffi:descriptionAuthentication
        <http://urn.fi/URN:NBN:fi:bib:auth/needs-review> .
  ?activity prov:used ?work ;
            bffi-prov:inputLiteral ?inputLiteral ;
            bffi-prov:stage "reconciliation-fallback" .
}
```

**What NOT to do.** Don't try to wire YSA's bare forms into a tier-0
altLabel lookup — they're absent from YSO by design. Don't lower the
lexical-similarity threshold to accept the 0.7-0.8 prefix matches —
that would silently bind to whichever disambiguated sense sorts first,
which is exactly the ambiguity cataloguers need to resolve.

**Resolution path.** Surface the needs-review queue to cataloguers; on
each record they pick the right disambiguated YSO URI and the next
pipeline run binds correctly.

**Cataloguer worklist.** Helmet cataloguers can't search the
needs-review queue from the current ILS, so the
`ysa-disambiguation-report` CLI produces a UTF-8-with-BOM CSV
(Excel-safe for Finnish diacritics) that they can open, sort, and
work through directly:

```bash
bffi-pipeline ysa-disambiguation-report
# → writes <BFFI_DATA_DIR>/ysa-disambiguation-report.csv with
#   columns: helmet_bib_id, canonical_work_uri, source_tag, literal,
#   case_type, n_candidates, candidate_uri, candidate_pref_label.
#   One row per (helmet_bib_id, literal, candidate) tuple — sort by
#   `literal` to apply one decision across the N records sharing it;
#   sort by `helmet_bib_id` to find each record in the ILS.
```

The report classifies each flagged literal into one of two
case types so cataloguers can prioritise:

- **`missed-altlabel`** — exactly one disambiguated YSO candidate,
  no real ambiguity. Cataloguer just adds `$0 <candidate_uri>` to
  the MARC record. Quick win. Examples on the 200-record sample:
  `1600-luku`–`2010-luku` decade literals (each → the single
  `*-luku (vuosikymmen)` URI).
- **`ambiguous`** — ≥ 2 disambiguated candidates; cataloguer
  inspects record context and picks. Examples: `Lappi` (4 senses —
  municipality / Tampere-neighborhood / Swedish-Lapland /
  Finnish-Lapland), `lapset`, `metro`, `musiikki`,
  `pohjalaismurteet`, `2000-luku` (decade vs. century).

The walker dedupes Fuseki round-trips per distinct literal, so
running this against the full 800 k canonical is cheap — one SPARQL
SELECT per unique flagged term, not per record.

## Compaction cron suggestion

Once production data is flowing:

```cron
# crontab -e
0 4 1 */3 *  cd /path/to/bffi-pipeline && uv run bffi-pipeline provenance compact --older-than 90d >> ~/Library/Logs/bffi-compact.log 2>&1
```

(Quarterly is a comfortable margin under the 90-day staleness floor.)

## What's still missing

The end-to-end pipeline through M12 is committed. Outstanding work:

- **Cataloguer-driven gold-set growth.** `gold/gold.jsonl` carries
  the bootstrap 13 cases; spec § 9 targets 50–100 stratified by
  category with ≥ 2 holdout per category. `bffi-pipeline grow-gold`
  proposes candidates from Fuseki overrides; promoting them into
  `gold/gold.jsonl` is cataloguer work and not codeable.
- **M11 user-side smoke** — the seven-item Skosmos UI checklist
  below requires `docker compose up` against real Skosmos and a
  loaded Fuseki dataset. Run before declaring a corpus loaded.
- **`--concurrency` sweep** — pick the production value (see the
  sweep section above) and update the example command sequence
  with the chosen number.

Pinned versions stay in the table above so the runbook is the one
place the production stack is named.
