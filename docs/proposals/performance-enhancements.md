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

## P-02 — Inference-stack tuning for the M6 cascade

**Status**: proposed.
**Scope**: milestone-sized, but the two sub-items are independent —
prefix caching alone is ~2-3 days once the stack moves to vllm-mlx;
speculative decoding adds another 1-2 days of model-bench + integration
work.

### Motivation

P-01 reduces *how many* LLM calls M6 makes. This proposal makes each
remaining call *cheaper*. The current M6 backend is Ollama, which is
fine for gold-set runs and few-hundred-pair sweeps but leaves
significant local-inference performance on the table. Two specific
optimisations match the M6 workload shape:

1. **Prompt prefix caching.** The M6 prompt is ~75 % identical across
   pairs — system instructions, spec § 6 decision rules, JSON schema
   example, optionally a small set of few-shot exemplars. Only the
   per-pair payload differs (creator strings, titles, languages,
   dates, similarity score). A backend that recognises the shared
   prefix and reuses the prefill KV-cache turns each subsequent call
   into "encode only the new suffix" — a typical 3-10× TTFT speedup
   on this kind of workload.
2. **Speculative decoding** with a small draft model. Most tokens M6
   emits are structurally predictable: the JSON keys
   (`"decision"`, `"confidence"`, `"matching_fields"`), the bracket
   scaffolding, the closing braces. A 1.7B draft model would accept
   nearly all of those without target-model invocation, and only the
   *decision-bearing* tokens (`same_work` vs `different_work`, the
   confidence number, the actual field-list contents) trigger the
   8B/32B verifier path. Net: 2-4× wall-time reduction for verbose
   structured outputs on local Apple Silicon.

Crucially, both optimisations are pure infrastructure — they don't
change M6's contract (input pair → `WorkMatchDecision`), don't change
gold-set scoring, and don't change provenance shape. The change is
visible only in `judge_batch` throughput numbers and in the inference
backend config.

### Approach

The natural unblocking step is moving M6's inference backend from
Ollama to **vllm-mlx**, which the runbook already nominates as the
production-batch backend on the M5 Max. vllm-mlx exposes both knobs
as first-class config; Ollama (despite running llama.cpp under the
hood, which supports both internally) does not surface them via its
HTTP API.

Three independent sub-items:

#### P-02a — Migrate M6 to vllm-mlx for production runs

- Bring up vllm-mlx as a sibling local OpenAI-compatible server on a
  different port; gold-set bench at parity vs. the Ollama baseline
  to confirm the model behaves identically.
- Switch `.env` `LLM_BASE_URL` to the vllm-mlx port for production
  batches; keep Ollama as the dev/eval default.
- Prerequisite for both P-02b and P-02c.

#### P-02b — Prompt prefix caching

- Identify the static prefix of the M6 prompt (system + decision
  rules + schema). Lift it into a constant so it's byte-identical
  across pairs — today the prompt builder string-interpolates the
  whole thing each call, which is fine but means the backend has to
  hash the full prompt to recognise prefix re-use.
- Enable vllm-mlx's `--enable-prefix-caching`.
- Bench: TTFT and end-to-end pair-latency on a 200-pair slice with
  vs. without prefix caching, primary model only. Expectation: 3-10×
  TTFT speedup, 1.5-3× end-to-end on the typical short-decision
  fast-mode path.

#### P-02c — Speculative decoding

- Choose the draft model. `qwen3:1.7b` is the obvious candidate
  (same tokenizer family as our primary, small enough to amortise).
- Configure vllm-mlx with `--speculative-model` + `--num-speculative-tokens`
  (probably 4-6 for our short-decision outputs).
- Bench: token-acceptance rate and net wall-time on the same 200-pair
  slice. Expectation: ~80 % token-acceptance rate, 2-3× speedup on
  fast-mode outputs (less for `--full-rationale` runs where the
  rationale text is less template-y).

### Prerequisites

- vllm-mlx installed and configured on the M5 Max (the runbook
  treats this as the production-batch path; this proposal forces the
  install schedule).
- Gold-set bench in place (`make eval`) — we need an authoritative
  before/after to prove neither optimisation changes verdict
  precision/recall on the 17-case gold set.

### Risks

- **Backend divergence between dev and prod.** Running Ollama for
  development eval and vllm-mlx for production batches creates two
  inference stacks. Risk: a behavioural difference (sampler quirk,
  tokenizer edge case) shows up only at scale. Mitigation: gold-set
  bench under both backends; CI-equivalent eval run before each
  production batch.
- **Speculative decoding token-acceptance lower than projected.**
  If the draft model's outputs diverge too often, the speculative
  path becomes overhead. Mitigation: the bench step measures this
  before we commit; if acceptance is below 50 %, ship P-02b only
  and shelve P-02c.
- **Prefix-cache invalidation footgun.** If the prompt builder ever
  accidentally varies a byte of the supposedly-static prefix (e.g.
  a timestamp injection for provenance), cache hit rate silently
  drops to 0 %. Mitigation: a unit test asserting prompt-prefix
  byte-stability against a recorded fixture.

### Scope

| Item | Estimate |
|---|---|
| P-02a vllm-mlx bring-up + parity bench | ~2 days |
| P-02b prefix caching + prompt-builder refactor + benches | ~2-3 days |
| P-02c speculative decoding + draft-model bench | ~1-2 days |

P-02b is the cheapest standalone win and a strict prerequisite for
P-02c's speedup math (speculative decoding *with* prefix caching is
multiplicative, not additive — the gains compound). Order should be
P-02a → P-02b → P-02c, but a/b alone is already worth shipping.

### Open questions

- Is `qwen3:1.7b` the right draft model, or does a non-instruct-tuned
  base model (smaller, less aligned, faster) give better token
  acceptance on a structured-output workload? Answerable by a half-
  day bench.
- Does Ollama gain prefix-caching support before we finish the
  vllm-mlx migration? (Roadmap-watch task — if yes, P-02a's
  motivation weakens for the dev-loop case, but the production-batch
  case for vllm-mlx remains.)
- How do P-01 (distilled classifier) and P-02 interact? They're
  multiplicative: P-01 reduces *N*, P-02 reduces per-call latency.
  Combined expected gain: order-of-magnitude on multi-batch
  refresh workloads. Worth a joint bench after both ship.

### Provenance

Origin: external feedback from a colleague reviewing the public repo.
The deferred-rationale half of the same feedback was already shipped
as `--full-rationale` (commit `491c1b5`); the speculative-decoding and
prefix-caching halves became this proposal.

---

<!-- Add new proposals below as ## P-03, P-04, … -->
