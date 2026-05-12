# Proposals

Documents in this folder are **proposals**, not committed work. They
sketch directions we might take but haven't decided on. A proposal
graduates into [`docs/plans/`](../plans/) — a thorough plan of
record, with sequenced phases, verification checkpoints, and a
rollback procedure. The proposal stays in place here as a short stub
pointing at the plan. A plan's completion is recorded in its own
`Phase commits` field, not in any external checklist.

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

- **Status** — `proposed` / `planning (graduated)` /
  `merged into <plan>` / `done` / `rejected (reason)`. Read that
  first before treating anything in here as a plan of record.
  `merged into <plan>` means the proposal's content was absorbed
  into an existing plan rather than spawning its own — used when
  two proposals are tightly coupled enough to share one execution
  plan.
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
  — `planning (graduated)`. Migrate M6 from Ollama to mlx-lm and
  layer prompt-prefix caching + speculative decoding on top. Plan
  lives at [`docs/plans/completed/p-02-inference-stack-tuning.md`](../plans/completed/p-02-inference-stack-tuning.md).
- [`prop-03-m6-stall-watchdog.md`](prop-03-m6-stall-watchdog.md)
  — `planning (graduated)`. Detect M6 LLM calls that hang and retry
  the stuck pair so unattended overnight runs don't lose hours to a
  single transient Ollama wedge. Plan lives at
  [`docs/plans/completed/p-03-m6-stall-watchdog.md`](../plans/completed/p-03-m6-stall-watchdog.md).
- [`prop-04-consolidate-on-mlx-lm.md`](prop-04-consolidate-on-mlx-lm.md)
  — `merged into P-02 plan`. Dev-loop consolidation on mlx-lm
  (supervisor / pull wrapper / throughput verification / default
  flip / Ollama deprecation) absorbed into
  [`docs/plans/completed/p-02-inference-stack-tuning.md`](../plans/completed/p-02-inference-stack-tuning.md)
  as Phase D1-D5 (after A) and D6 (after C). Note: numbering
  collision with the unrelated `backlog/p-04-m5-calibration.md`
  plan — disambiguate by path in prose.
- [`prop-05-anonymous-work-canonicalisation.md`](prop-05-anonymous-work-canonicalisation.md)
  — `proposed`. M8 currently mints canonical Works only when a MARC
  100/110 → URI agent → prefLabel chain exists, sending the rest to
  `canonical-conflicts.jsonl`. On the preview-373 corpus that's
  365 / 372 unique work URIs held back from Skosmos. Proposes a
  fallback URI-minting policy for anonymous / secondary-creator-only
  records, with three options of increasing ambition.
- [`prop-06-structured-output-backend.md`](prop-06-structured-output-backend.md)
  — `proposed`. P-02 A5 found mlx-lm 0.31 has no constrained decoding
  for `response_format: json_schema`; the fix landed at the prompt
  layer via `src/bffi_pipeline/llm_json_mode.py`. This proposal
  weighs three server-side alternatives (outlines wrapper, vllm-mlx,
  fork mlx-lm) for the case where the prompt-layer approach proves
  insufficient. Stays `proposed` unless a concrete incident
  motivates action. Note: numbering collision with the unrelated
  `backlog/p-06-gold-set-growth.md` plan — disambiguate by path in
  prose.
- [`prop-07-bibframe-856-as-item.md`](prop-07-bibframe-856-as-item.md)
  — `proposed`. marc2bibframe2 lifts MARC 856 (Electronic Location
  and Access) as a separate `bf:Instance`, which is semantically
  closer to `bf:Item` for the typical Helmet usage (publisher PDF
  links, landing pages). P-02's 5k production-style run worked
  around the symptom in `549baa0` (URI-regex exclusion in the
  Boundary-2 shape + deterministic main-Instance pick in
  `_find_root_resources`). This proposal sketches three depth
  levels for the semantic fix — local M2 rewrite, configurable
  per-856 classifier, or an upstream PR to marc2bibframe2 — and
  documents what would have to be true for it to be worth shipping.
- [`prop-08-richer-rda-33x-synthesis.md`](prop-08-richer-rda-33x-synthesis.md)
  — `proposed`. The current Sierra-export RDA 336/337/338 synth
  cascades bib `material_code` → item `itype_code_num` (commits
  `3f92a09` + `46b0f8a`), which leaves bibs with no mapped signal
  still dropping on the M2 content-minimum gate. This proposal
  layers MARC's more precise signals — leader/06, 007 (deterministic
  carrier), 008 / 006 material positions, 245$h GMD regex, 300$a
  extent — into a slot-wise cascade above the existing tables,
  with a `$5 FI-HELME/synth-v<N>` provenance marker on synthesised
  datafields so downstream consumers can tell cataloguer-coded
  from synth-coded 33X apart.
