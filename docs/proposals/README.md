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

- **Status** — `proposed` / `planning (graduated)` / `done` /
  `rejected (reason)`. Read that first before treating anything in
  here as a plan of record.
- **Scope** — rough size (half-day / 1-2 days / 1-2 weeks / milestone).
- **Proposal-base commit** — see "Tying proposals to version
  control" below.
- **Motivation** — what the current pipeline does, and what's
  expensive about it.
- **Approach** — the proposed change, kept high-level.
- **Prerequisites** — what has to be true before we can start.
- **Risks** — what could go wrong, and how we'd notice.
- **Open questions** — anything that should be settled before the
  proposal can graduate. Counterpoints and rejected alternatives
  also live here (with the reasoning) so the trade-off is on the
  record.

A proposal in the `planning (graduated)` state may collapse most of
this template to a short stub pointing at its corresponding plan
under `docs/plans/`.

## Tying proposals to version control

A proposal is a snapshot. Its "Motivation" and "Approach" reason
about the code as it stood when the proposal was drafted; if `main`
moves a lot before the proposal is acted on, parts can quietly go
stale. To make that drift detectable, each proposal carries a
**Proposal-base commit** field near the top:

- The base commit is the commit the proposal was drafted against
  (usually the commit that introduced the proposal file, or its
  parent if the proposal was reasoning about a state just before
  its own landing).
- **Material updates** are listed as a short bullet list under the
  base — each entry pairs a commit hash with one phrase describing
  what changed in the proposal text (e.g. "added counterpoint", "
  prerequisites tightened"). These give a fast way to scan the
  proposal's intellectual history without `git log -p` archaeology.
- Before acting on a proposal, run `git diff <base>..HEAD --
  <relevant paths>` to confirm the section the proposal touches has
  not been refactored out from under it. The proposal should
  explicitly name those relevant paths.

When a proposal graduates into a plan, the plan picks up its own
**Plan-base commit** field (see [`docs/plans/README.md`](../plans/README.md)).
The proposal's commit-hash trail and the plan's are independent
records — the proposal documents *when the idea was conceived*; the
plan documents *when the execution was scheduled*.

## Current proposals

- [`prop-01-llm-distillation-pre-screener-for-M6.md`](prop-01-llm-distillation-pre-screener-for-M6.md)
  — `proposed`. Distil M6's structured LLM verdicts into a cheap
  classifier that short-circuits the obvious pairs on subsequent
  batches.
- [`prop-02-inference-stack-tuning-for-M6.md`](prop-02-inference-stack-tuning-for-M6.md)
  — `planning (graduated)`. Migrate M6 from Ollama to vllm-mlx and
  layer prompt-prefix caching + speculative decoding on top. Plan
  lives at [`docs/plans/p-02-inference-stack-tuning.md`](../plans/p-02-inference-stack-tuning.md).
- [`prop-03-m6-stall-watchdog.md`](prop-03-m6-stall-watchdog.md)
  — `proposed`. Detect M6 LLM calls that hang and abandon the stuck
  pair so unattended pipeline runs don't lose hours to a single
  pathological pair. Two options: per-call timeout (recommended MVP)
  or out-of-process heartbeat watchdog.
