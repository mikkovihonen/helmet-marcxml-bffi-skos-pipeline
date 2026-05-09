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

- **13 cases** committed (target per spec § 9: 50–100; bootstrap is
  intentionally smaller).
- **31% holdout** (4 of 13), hand-marked per case via the `holdout`
  field — *not* hash-derived.
- **7 categories** covered: translation, transliteration, adaptation,
  abridgement, music-recording-vs-notated, cross-genre, and
  same-author-different-titles.
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

Spec § 9 asks for at least 2–3 holdouts per category. The bootstrap
set does **not** yet satisfy this — the loader's
`assert_holdout_stratification(min_per_category=2)` helper will raise
on this set. The check should be enforced once the gold set grows
past ~50 cases (post-M6 / first production run with human overrides).

## Sources

- **Real-record pairs** (10 of 13) cite their `helmet_bib_id` on each
  side. Most are drawn from the cataloguer-curated dev sample
  (`tests/data/sample-marcxml/curated/`) plus three additional
  Pushkin and Morton records (1690010, 2080863, 2297829, 2099930,
  2293686) sourced from the production corpus at
  `helmet-sierra-data-tools/output/marcxml/` to anchor the
  `same_work` and `same-author-different-titles` cases.
- **Synthesized records** are tagged `"synthesized": true` on the
  affected side. Used only where the corresponding real bib was not
  found in the corpus — replace with real records as they appear.
  Three cases currently carry a synthesized side: `gs-0003`
  (transliteration), `gs-0004` (Morton English original), `gs-0005`
  (Závada source novel for the *Natural light* film), `gs-0006`
  (Tove Jansson source novel for the Moomin children's-book
  abridgement).

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
