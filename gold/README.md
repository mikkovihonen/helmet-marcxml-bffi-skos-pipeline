# Gold set — bootstrap

This is the **bootstrap** gold set for the BFFI pipeline (M12). It is
the seed used to:

1. Benchmark candidate embedding models on `same_work` vs
   `different_work` cosine-similarity gap (`bffi-pipeline embed-benchmark`,
   M5 sub-task).
2. Tune `efSearch` against high-similarity `same_work` known pairs
   (M5 sub-task; runs after the index is built on the production
   corpus).
3. Score the M6 LLM judge once it lands.

## Status

- **15 cases** committed (target per spec § 9: 50–100; bootstrap is
  intentionally smaller).
- **33% holdout** (5 of 15), hand-marked per case via the `holdout`
  field — *not* hash-derived.
- **8 categories** covered: translation, transliteration, adaptation,
  abridgement, music-recording-vs-notated, cross-genre,
  same-author-different-titles, and subject-as-name-discrimination.
- **Common-title-collision and edition-revision categories are not yet
  populated** — these need real Helmet pairs that the cataloguer Ask 1
  / Ask 2 didn't surface (Slot 5 in `docs/external-dependencies.md`
  remains outstanding).

## Per-category holdout coverage

| Category | n | holdout |
|---|---|---|
| translation | 3 | 2 |
| transliteration | 1 | 0 |
| adaptation | 1 | 0 |
| abridgement | 1 | 0 |
| music-recording-vs-notated | 1 | 0 |
| same-author-different-titles | 2 | 1 |
| cross-genre-different-work | 4 | 1 |
| subject-as-name-discrimination | 2 | 1 |

Spec § 9 asks for at least 2–3 holdouts per category. The bootstrap
set does **not** yet satisfy this — the loader's
`assert_holdout_stratification(min_per_category=2)` helper will raise
on this set. The check should be enforced once the gold set grows
past ~50 cases (post-M6 / first production run with human overrides).

## Sources

- **Real-record pairs** (11 of 15) cite their `helmet_bib_id` on each
  side. Most are drawn from the cataloguer-curated dev sample
  (`tests/data/sample-marcxml/curated/`) plus three additional
  Pushkin and Morton records (1690010, 2080863, 2297829, 2099930,
  2293686) sourced from the production corpus at
  `helmet-sierra-data-tools/output/marcxml/` to anchor the
  `same_work`, `same-author-different-titles`, and
  `subject-as-name-discrimination` cases.
- **Synthesized records** are tagged `"synthesized": true` on the
  affected side. Used only where the corresponding real bib was not
  found in the corpus — replace with real records as they appear.
  Cases currently carrying a synthesized side: `gs-0003`
  (transliteration), `gs-0004` (Morton English original), `gs-0005`
  (Závada source novel for the *Natural light* film), `gs-0006`
  (Tove Jansson source novel for the Moomin children's-book
  abridgement), `gs-0014` (second Pekurinen biography placeholder).

## Format

JSONL, one Work-pair per line — the format spec § 9 specifies.
Diffs cleanly in pull requests. Schema enforced by
`bffi_pipeline.eval.gold_set.GoldCase` (Pydantic v2,
`extra="forbid"`).

## Growing the gold set

Manual additions are welcome; the schema accepts them. The eventual
M12 growth pipeline (`src/bffi_pipeline/eval/grow.py`, currently a
stub) will surface candidates from the "humans overrode the LLM"
SPARQL query against Fuseki — that lands once M6 + M10 are in.

### Growing `gold/contrib.jsonl` from M3 contrib-extract cascade output

P-39 (M9 reconciles non-primary contributions against KANTO) is gated
on `gold/contrib.jsonl` reaching ≥ 30 cataloguer-vetted rows. The
candidate-generation tool `bffi-pipeline grow-gold-contrib` walks
M2's BIBFRAME output, runs the same heuristic + LLM cascade M3 runs
internally, and writes a JSONL of candidates the cataloguer reviews:

```bash
# Cost projection — measure heuristic-fire rate without LLM calls.
uv run bffi-pipeline grow-gold-contrib \
    --bibframe-dir runs/<run-uuid>/bibframe \
    --output-path /tmp/contrib-candidates-dryrun.jsonl \
    --no-llm-cascade

# Live run — invokes the Qwen3 8B mlx-lm cascade per heuristic-fire.
# Each call ~10 s warm; full 5 k corpus is ~10-15 min.
uv run bffi-pipeline grow-gold-contrib \
    --bibframe-dir runs/<run-uuid>/bibframe \
    --output-path gold/grow-candidates-contrib.jsonl

# Smoke against the first 100 BIBFRAME files.
uv run bffi-pipeline grow-gold-contrib \
    --bibframe-dir runs/<run-uuid>/bibframe \
    --output-path /tmp/contrib-candidates-smoke.jsonl \
    --limit 100
```

The tool dedups against `gold/contrib.jsonl` by `helmet_bib_id`, so
re-runs don't re-ask the cataloguer to re-vet cases already merged.

**Cataloguer review workflow (browser-based, no git / JSON
editing required):**

The cataloguer receives a **review bundle zip** from the operator
— a single `.zip` file carrying the candidate JSONL, per-record
MARCXML sidecars, and a manifest. They open it in
`gold/cataloguer-review.html` (a static HTML file they download
from this repo once and bookmark; no installation, no server, no
internet connection needed).

1. The cataloguer opens `cataloguer-review.html` in any modern
   browser (Firefox, Chrome, Safari).
2. They type their name once (persisted in browser local storage).
3. They click "Choose .zip" or drag-and-drop the bundle zip onto
   the dropzone. The tool parses the zip in-browser via the
   inlined fflate library — no network, no installation.
4. A tab strip appears across the top, one tab per LLM-using
   pipeline stage in the bundle. Today only the
   **M3 contrib extraction** tab is populated; future stages
   (M6 judge, M9 picker, M2 salvage, M3 title-lang) will surface
   as additional tabs when their candidate queues are wired.
   A **Manifest** tab shows bundle metadata.
5. For each candidate in the active tab, they:
   - Inspect the **full MARC record** (a compact tag/indicator/
     subfield view; the 245 field is highlighted, control + admin
     fields are collapsed behind a "Show admin fields" toggle).
   - Read the MARC 245$c (statement of responsibility) and the
     list of agents already in 100/700.
   - Fix the LLM's `vetted_contributions` — adjust names, roles,
     and `transliteration_of` pointers; remove hallucinations;
     add missed agents. The role dropdown lists the 20 MARC
     relator codes with friendly labels; the right-hand "raw"
     field accepts pipe-separated combos like `aft|aui|ctb` for
     genuinely ambiguous roles (matches bootstrap `cg-0002`).
   - **Split composite names**: if the LLM returned a single
     `name` like "John ja Jane" or "Eija Aarnio .. et al." the
     editor shows an amber banner with a "Split on separator"
     button; one click previews the N split names and (on
     confirm) replaces the single agent with N agents sharing
     the original's role. Cataloguers can ignore the warning for
     legitimate single-entity cases (editorial boards, et al.
     credited as a unit).
   - Pick a category (the canonical 4: `pure-new-agent`,
     `role-classification`, `within-record-typo`,
     `cyrillic-latin-transliteration`).
   - Tick "Hold out" for the 25–35 % the gold set holds out for
     evaluation (aim for ≥ 2 per category).
   - Click **KEEP** to mark the row for the gold file,
     **DISCARD** to drop it (e.g. when 245$c is too ambiguous to
     vet, or the LLM was fundamentally wrong), or
     **SKIP-FOR-NOW** to come back later.
   - Keyboard shortcuts: <kbd>K</kbd> / <kbd>D</kbd> /
     <kbd>S</kbd> for decisions, <kbd>←</kbd>/<kbd>→</kbd> to
     navigate. After a decision the tool auto-advances to the
     next candidate.
6. Progress is saved automatically in the browser. They can close
   the browser and come back days later — the same bundle zip
   restores their state from local storage (keyed by the zip's
   SHA-256 hash, so different bundles don't collide).
7. When done, they click **"Export results (zip)"** and email
   the downloaded `gold-review-results-<date>.zip` back to the
   operator. The results zip mirrors the bundle layout but
   carries per-stage `results.jsonl` files instead of
   `candidates.jsonl`.

**Operator-side workflow:**

```bash
# Build a review bundle from the candidate pool. Today the bundle
# contains just the contrib stage (M3); future stages plug in via
# stage handlers in bffi_pipeline.eval.review_bundle.stages.
uv run bffi-pipeline review-bundle-build \
    --operator "Mikko Vihonen" \
    --per-category 25 \
    --seed 2026-06-03

# Bundle lands at scratchpad/review-batch-<date>.zip — email or
# share via Caddy with the cataloguer. The HTML reviewer at
# gold/cataloguer-review.html is the second artefact they need;
# they bookmark it once and reuse across all future bundles.

# After the cataloguer emails back their results zip —
uv run bffi-pipeline review-bundle-import \
    --input ~/Downloads/gold-review-results-2026-06-04.zip
```

The import dispatches each stage's `results.jsonl` to its
registered handler. For contrib, KEEP rows are validated
(canonical category, non-empty `vetted_contributions`, relator
codes in `VALID_RELATOR_CODES` — pipe-separated `aft|aui|ctb`
is accepted for ambiguous roles), assigned sequential `cg-NNNN`
ids, and appended to `gold/contrib.jsonl` in the canonical
bootstrap schema. DISCARD rows are silently dropped;
SKIP-FOR-NOW rows are reported so the operator can re-send them
in a future bundle. The cataloguer's name lands in `added_by`
as `cataloguer:<name>` so bootstrap / cataloguer / tool sources
stay distinguishable across the gold set's history.

The summary also reports the **composite names retained** —
KEEP rows whose `name` field still contains a recognised
multi-author separator at import time (e.g. the cataloguer
chose to keep `"Bill Hailey and The Comets"` as a single
agent rather than split). The operator can follow up with the
cataloguer if any look genuinely composite; the import doesn't
block.

If a row fails validation, the import raises
`GoldReviewImportError` naming the offending row id + field —
the operator asks the cataloguer to fix that one row rather
than re-doing the whole bundle. **The import is two-pass and
atomic**: all rows are validated before any are appended, so a
single bad row can't leave `gold/contrib.jsonl` half-written.

For dry-runs and tests, pass `--contrib-gold /tmp/test.jsonl`
to redirect the contrib stage's appends away from the
production gold file. Unknown stages in the results zip surface
as `unknown stages` in the summary without aborting the known
ones — a forward-compatibility hatch for results bundles
produced by a newer HTML.

### M6 judge tab — same workflow, different card shape

The M6 judge writes `runs/<uuid>/judge-decisions.jsonl` per run
automatically (no flag needed — M6 is always-on in the canonical
chain). When M6 runs, the `cataloguer-bundle` stage auto-detects
the audit log and adds a **second tab** to the bundle alongside
contrib.

The cataloguer's view of a judge pair shows:

- **Two records side-by-side** (Work A | Work B) — each side
  carries the MARC record, the extracted creator / title /
  language / year, and the helmet bib id.
- **LLM decision banner** — the model's verdict
  (`same_work` / `different_work` / `uncertain`), its confidence,
  the rationale, and which fields the model thought matched vs
  diverged.
- **Decision row with 6 options**:
  - **AGREE** (`A`): keep the LLM's verdict as-is. Resolves
    `expected` to the LLM's `decision` for the gold row.
  - **FLIP → SAME** (`F`): cataloguer disagrees, marks the pair
    as `same_work`.
  - **FLIP → DIFFERENT** (`G`): cataloguer disagrees, marks the
    pair as `different_work`.
  - **STILL-UNCERTAIN** (`U`): cataloguer can't tell. Doesn't
    land in gold — `gold/gold.jsonl` requires binary verdicts.
  - **DISCARD** (`X`): not useful as a gold case (e.g. one side
    is corrupt). Doesn't land.
  - **SKIP-FOR-NOW** (`Z`): come back later.
- **Category dropdown** with the 11 canonical `GoldCategory`
  values (translation / transliteration / adaptation /
  abridgement / common-title-collision / etc.).
- **Holdout checkbox**, **notes textarea**.

Sampling is **stratified by `decision × confidence_band`**
(low / mid / high), so the cataloguer sees a balanced mix of
the most uncertain pairs (where the LLM benefits most from
human input) alongside high-confidence pairs (for spotting
over-confidence regressions).

The bundle's `judge/marc/` directory carries one MARCXML per
unique bib id across all pairs (deduped — many pairs share a
Work). The bundle's `judge/candidates.jsonl` hydrates each
side's `creator` / `title` / `language` / `year` /
`content_type` from the run's own `bffi/<bib>.ttl` files at
bundle-build time, so the cataloguer's results.jsonl carries
everything `import_results` needs to append a complete
`GoldCase` to `gold/gold.jsonl`.

The summary the importer prints calls out:

- **Cataloguer overrides** — pairs where the cataloguer flipped
  the LLM's verdict. Worth scanning before publishing the gold
  growth, since each flip is a signal for prompt-tuning or
  model upgrade.
- **STILL-UNCERTAIN count** — pairs the cataloguer flagged for
  follow-up; they stay in `judge-decisions.jsonl` for a future
  bundle.

### M9 picker tab — entity-centric KANTO URI review

The M9 reconciliation stage uses an LLM picker to choose a KANTO
authority URI from a candidate list when the lexical-similarity
tier can't commit. The picker writes
`runs/<uuid>/picker-decisions.jsonl` per run (one row per fire),
which the `cataloguer-bundle` stage auto-discovers and packs as
the bundle's **third tab** alongside contrib + judge.

The picker card is entity-centric, not record-centric:

- **Header banner** — entity literal (e.g. "Henrik Lönnrot"),
  entity kind chip (person / corporate_body / subject /
  genre_form / music_form / fictional_character), outcome class
  (`llm_pick` / `fallback` / `watchdog_aborted`), LLM
  confidence + rationale.
- **Originating record** — MARC view of the bib that originated
  this entity reconciliation request.
- **Candidate table** — every KANTO candidate the LLM saw:
  prefLabel, source vocabulary (finaf / yso / kauno / muso /
  viaf), lexical similarity score, URI (clickable). The LLM's
  pick row is highlighted with a ★.
- **6-way decision row**:
  - **AGREE** (`A`) — keep the LLM's chosen URI as `expected_uri`.
    For `fallback` / `watchdog_aborted` outcomes, AGREE means
    "agree with the fallback's highest-lexical pick".
  - **PICK CANDIDATE** (`P`) — enable an inline radio over the
    candidate table; the chosen candidate becomes `expected_uri`.
  - **NO-MATCH** (`N`) — lands a row with `expected_uri = null`.
    Useful signal: the picker should have said "uncertain"
    but didn't, or no KANTO entry exists for this entity. The
    operator's summary calls these out as a follow-up class.
  - **STILL-UNCERTAIN** (`U`) — cataloguer can't tell; doesn't
    land in gold.
  - **DISCARD** (`X`) — bad row.
  - **SKIP-FOR-NOW** (`Z`).
- **Notes textarea** + holdout checkbox.

Imported KEEP rows land in `gold/picker.jsonl` with the schema:

```json
{
  "id": "gp-NNNN",
  "entity_kind": "person",
  "entity_label": "Henrik Lönnrot",
  "expected_uri": "http://urn.fi/URN:NBN:fi:au:finaf:000123" | null,
  "originating_bib_id": "1002345",
  "originating_work_uri": "http://urn.fi/URN:NBN:fi:bib:work:...",
  "predicate_uri": "...",
  "holdout": false,
  "added": "2026-06-04",
  "added_by": "cataloguer-m9:Maija Mäkinen",
  "notes": "...",
  "candidates_seen": [
    {"uri": "...", "pref_label": "...",
     "source_vocabulary": "finaf", "lexical_similarity": 0.85}
  ]
}
```

`candidates_seen` is preserved so an eval harness can re-run
the picker against the historical candidate set and check
whether picker output matches `expected_uri`.

Sampling is **stratified by `entity_kind × outcome_stage ×
confidence_band`** (low / mid / high), surfacing the most
uncertain picks (where human judgement matters most) alongside
high-confidence picks (for spotting over-confidence
regressions).

The picker tab's audit log is populated automatically whenever
M9 runs in the canonical chain — no operator flag needed. The
cataloguer-bundle stage's auto-discovery handles four audit
logs simultaneously (`contrib-candidates.jsonl`,
`judge-decisions.jsonl`, `picker-decisions.jsonl`,
`salvage-candidates.jsonl`); runs that skipped a stage simply
produce a bundle without that tab.

### M2 salvage tab — bib-centric audit of LLM-extracted creators

The M2 stage's LLM salvage cascade (P-41) extracts a creator
from MARC 245$c when the deterministic regex tier can't parse
it and the record has no 1XX/7XX. With `--llm-salvage-cascade`
on, every salvage call writes to
`runs/<uuid>/salvage-candidates.jsonl` — both fresh LLM calls
and cache hits land in the audit log, with a `cache_hit: bool`
flag so the cataloguer can tell them apart. (Cache hits matter
because the decision still influenced this run's BIBFRAME
graph, regardless of when the LLM call was originally made.)

The salvage card is bib-centric like contrib:

- **MARC view** — the originating record, 245 field highlighted
- **LLM banner** — the LLM's free-text rationale + a "fresh
  LLM" / "cache-hit" badge
- **245$c text** (read-only) — the exact text the LLM parsed
- **Expected agents** (editable) — name + role per agent. The
  cataloguer can edit names in place, override roles
  (author / editor / translator / illustrator / compiler /
  unknown), add agents the LLM missed, or remove
  hallucinations. **A name that doesn't appear verbatim in the
  245$c text gets an amber-bordered input** with a tooltip
  pointing to P-41's verbatim-substring constraint; editing
  past the constraint is allowed (legitimate for typo-fixes)
  but the cataloguer should note it.
- **Decision row** (3 options, contrib-style): KEEP /
  DISCARD / SKIP-FOR-NOW

Imported KEEP rows land in `gold/salvage.jsonl` with the
schema:

```json
{
  "id": "sv-NNNN",
  "helmet_bib_id": "1000123",
  "c_subfield": "by John Smith",
  "expected_agents": [
    {"name": "John Smith", "role": "author"}
  ],
  "holdout": false,
  "added": "2026-06-04",
  "added_by": "cataloguer-m2:Maija Mäkinen",
  "notes": "..."
}
```

Sampling is **stratified by `cache_hit × agent_count_band`**
(empty / single / multi), surfacing the multi-agent salvages
where the LLM might have over-extracted from a capitalised
list, alongside an even mix of fresh vs cached decisions.

DISCARD is the right call when the LLM picked a non-creator
(e.g. a publisher name from 260$b/264$b) or hallucinated an
agent the cataloguer can't verify. The summary's
`cache_hit_kept` count surfaces how many gold rows came from
cache replays so the operator can spot stale-cache concerns
across reviews.

## What's NOT done yet

- `make eval` target (depends on M6 LLM judge).
- `eval/harness.py` (per-category accuracy, confusion matrix,
  high-confidence calibration — depends on M6).
- `eval/grow.py` (depends on Fuseki / M10).
- CI gate / PR-template integration.
- Gold-set growth to 50+ cases with strict per-category holdout
  stratification.

What **is** done is the embedding-model benchmark
(`bffi-pipeline embed-benchmark`), which is what M5 needed unblocked.
