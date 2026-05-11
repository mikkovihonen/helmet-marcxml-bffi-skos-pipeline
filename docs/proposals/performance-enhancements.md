# Performance enhancement proposals

A running list of ideas to reduce wall-time, LLM volume, or compute
cost in the BFFI pipeline. Each entry is a **proposal**, not a
committed milestone — promote one into `docs/BUILD_PLAN.md` when it
graduates from "interesting" to "next on the list".

Each section follows the same template:

- **Motivation** — what the current pipeline does, and what's
  expensive about it.
- **Approach** — the proposed change, kept high-level.
- **Prerequisites** — what has to be true before we can start.
- **Risks** — what could go wrong, and how we'd notice.
- **Scope** — rough size (half-day / 1-2 days / 1-2 weeks / milestone).
- **Status** — `proposed` / `accepted` / `in-progress` / `done` /
  `rejected (reason)`.

---

## P-01 — LLM-distillation pre-screener for M6

**Status**: proposed.
**Scope**: 1-2 days for the MVP (Option 1 below); milestone-sized if
we also want Options 2 + 3.

### Motivation

M6 is the wall-time and compute bottleneck of the whole pipeline.
A spec-tightened cascade still takes hours to days per 50 k escalate
pairs even on local Apple-Silicon inference. The judge produces rich
structured output (decision, confidence, matching_fields,
diverging_fields, rationale) — that output is currently used once,
written to provenance, and then forgotten. We could instead treat the
historical LLM verdicts as a **training set for a cheap classifier**
that handles the obvious cases on subsequent batches, leaving the LLM
only for the ambiguous tail.

The shape of "obvious cases" is already empirically visible: most
M6 outcomes are either confidently `same_work` (shared creator +
near-identical title + matching language) or confidently
`different_work` (different creator or no creator + different
century + different language). Those decisions are *learnable* from
features the LLM already considers — embedding cosine, title token
overlap, creator distance, language match, date proximity,
identifier overlap — without needing to invoke a 32B-parameter model
to re-derive them.

### Approach

Three options of increasing ambition. Start with **Option 1**; the
other two are listed for completeness so we know the ceiling.

#### Option 1 — Gradient-boosted pre-screener inside the cascade

1. **Feature logging during M6.** For every escalate pair the LLM
   judges, persist a feature row to a new artifact
   (`<BFFI_DATA_DIR>/judge-features.jsonl`) carrying:
   - `pair_id`, `work_a`, `work_b`, `block_key`
   - `embedding_cosine` (already computed in M5)
   - `title_bigram_jaccard`, `title_levenshtein_normalised`
   - `creator_string_distance` (Jaro-Winkler), `creator_set_overlap`
   - `language_match` (boolean)
   - `date_year_difference` (int, or `None` if either side missing)
   - `identifier_overlap` (ISBN / OCN / etc. — boolean per scheme)
   - `block_key_family` (categorical: "anon|title|lang", "creator|title|lang", …)
   - `llm_decision`, `llm_confidence`, `llm_stage`
     (primary / fallback / auto-merge)

2. **Offline training.** A new CLI: `bffi-pipeline judge-distill-train
   --features <path> --gold gold/gold.jsonl --output models/judge-distill.json`.
   Trains a GBDT (LightGBM or XGBoost) on (features → LLM-decision).
   The gold set is **held-out** — we report precision / recall /
   coverage on gold, not on LLM-agreement.

3. **Cascade insertion.** When `models/judge-distill.json` is present,
   `cascade_judge` calls the classifier *before* the LLM:
   - If `classifier_proba > threshold_high` for `same_work` →
     short-circuit, no LLM call. Tag provenance with
     `stage="distilled-classifier"`, log the classifier's confidence
     + the model hash.
   - If `classifier_proba > threshold_high` for `different_work` →
     short-circuit, no LLM call.
   - Otherwise → escalate to LLM as today.
   `threshold_high` is calibrated against gold-set precision; the
   default refuses to short-circuit unless gold-set precision on
   high-confidence predictions is ≥ 99 %.

4. **Provenance.** Distilled decisions live in the provenance graph
   just like LLM decisions, with the new
   `bffi-prov:stage = "distilled-classifier"` value and a
   `bffi-prov:model_hash` triple pointing at the trained model
   artifact.

#### Option 2 — k-NN over judged history

A simpler intermediate: index every LLM-judged pair by its feature
vector; for each new pair, retrieve the top-k nearest judged pairs;
if they unanimously agree at high LLM confidence within a small
feature-space distance, reuse the verdict. Memorization, not
extrapolation. Half-day to ship, lower ceiling than Option 1, but
zero risk of mis-generalising to unseen feature combinations.

#### Option 3 — Fine-tuned BGE-M3 contrastive head

Train a small contrastive head on top of the M5 embeddings using LLM
verdicts as supervision. The similarity score itself becomes a
calibrated decision boundary — fewer pairs land in the "escalate"
band in the first place. High ceiling but invasive: changes the M5
contract, needs GPU training cycles, and the M5 → M6 boundary
becomes fuzzier in the spec.

### Prerequisites

- **Sufficient training data.** ~10 k LLM-judged escalate pairs at
  minimum; the v2 full-corpus run will produce roughly that.
- **Grown gold set.** The current ~15 gold cases are too few for a
  meaningful held-out evaluation. Need 50-100 cataloguer-vetted pairs
  covering the bib-type diversity (music, fiction, non-fiction,
  serials, multilingual editions) the corpus actually carries.
- **Feature-extraction module** factored out of M6: today the LLM
  prompt builder computes these features inline as strings; we'd
  need them as a typed `PairFeatures` dataclass with a single source
  of truth for both the prompt and the classifier.

### Risks

- **LLM bias propagates.** If the LLM is systematically wrong on
  some bib class (e.g. false-merges on similarly-titled music
  records), the classifier inherits that bias and amplifies it
  because it short-circuits the LLM. Mitigation: gold-set coverage
  for the failure modes, and a continuous "LLM-disagreement on the
  held-out gold set" metric that triggers retraining when it drifts.
- **Distribution shift across batches.** New acquisitions over time
  (e.g. a board-game collection) may not match the training
  distribution. Mitigation: per-batch eval against gold; retrain
  trigger when held-out gold precision drops below a threshold.
- **Provenance audit obligations.** A reviewer must be able to
  reconstruct *why* a particular distilled-classifier decision was
  made — that means logging the feature vector AND the classifier
  weights at decision time. The model hash + the persisted feature
  row should be enough.
- **Threshold calibration sensitivity.** Setting
  `threshold_high` too low → false short-circuits hit production.
  Too high → no LLM-volume reduction. Default to "no short-circuit
  unless gold-set precision at this threshold is ≥ 99 %" and ship
  with the threshold disabled until the cataloguer review approves
  the model.

### Open questions

- Does the auto-merge band (M5 sim ≥ 0.90 → spec § 6 → synthetic
  `same_work` without LLM) already capture most of the "easy"
  decisions? If yes, the distilled classifier mostly intercepts the
  `[0.78, 0.90)` escalate band — modest LLM-volume reduction.
  If no, the classifier could intercept significantly more.
  Empirical question, answerable after v2 finishes by looking at
  the M6 cascade's auto-merged-vs-LLM-decision ratio.
- Is there value in distilling **just the LLM rationale** rather
  than the verdict? A small classifier trained to predict
  `matching_fields` + `diverging_fields` might be useful as a
  feature-engineering aid for the LLM prompt itself (a kind of
  retrieval-augmented prompt). Lower priority.

---

<!-- Add new proposals below as ## P-02, P-03, … -->
