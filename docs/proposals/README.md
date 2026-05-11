# Proposals

Documents in this folder are **proposals**, not committed work. They
sketch directions we might take but haven't decided on. A proposal
graduates by moving into one of two places:

- **`docs/plans/`** — a thorough plan of record, with sequenced
  steps, verification checkpoints, and a rollback procedure. The
  proposal stays in place here as a short stub pointing at the plan.
- **`docs/BUILD_PLAN.md`** — when the plan completes, the work shows
  up as a milestone here for the project-wide build narrative.

A proposal can also be marked `rejected` with a one-line reason and
left in place for the record.

## File-naming convention

One proposal per file. Filenames follow
`prop-<NN>-<slug>.md`, where `NN` is a zero-padded sequence number
and `<slug>` is a brief kebab-case summary of the proposal's intent
(e.g. `prop-01-llm-distillation-pre-screener-for-M6.md`). The H1
heading inside the file uses `P-<NN>` (no `prop-` prefix) so that
existing references like `§ P-01` and `§ P-02` keep resolving in
prose.

## Template

Each proposal carries these sections:

- **Motivation** — what the current pipeline does, and what's
  expensive about it.
- **Approach** — the proposed change, kept high-level.
- **Prerequisites** — what has to be true before we can start.
- **Risks** — what could go wrong, and how we'd notice.
- **Scope** — rough size (half-day / 1-2 days / 1-2 weeks / milestone).
- **Status** — `proposed` / `planning (graduated)` / `done` /
  `rejected (reason)`. Read that first before treating anything in
  here as a plan of record.
- **Open questions** — anything that should be settled before the
  proposal can graduate. Counterpoints and rejected alternatives
  also live here (with the reasoning) so the trade-off is on the
  record.

A proposal in the `planning (graduated)` state may collapse most of
this template to a short stub pointing at its corresponding plan
under `docs/plans/`.

## Current proposals

- [`prop-01-llm-distillation-pre-screener-for-M6.md`](prop-01-llm-distillation-pre-screener-for-M6.md)
  — `proposed`. Distil M6's structured LLM verdicts into a cheap
  classifier that short-circuits the obvious pairs on subsequent
  batches.
- [`prop-02-inference-stack-tuning-for-M6.md`](prop-02-inference-stack-tuning-for-M6.md)
  — `planning (graduated)`. Migrate M6 from Ollama to vllm-mlx and
  layer prompt-prefix caching + speculative decoding on top. Plan
  lives at [`docs/plans/p-02-inference-stack-tuning.md`](../plans/p-02-inference-stack-tuning.md).
