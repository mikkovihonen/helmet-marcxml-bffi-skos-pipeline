# P-04 — Consolidate on vllm-mlx (deprecate Ollama)

**Status**: merged into P-02 plan. See
[`docs/plans/backlog/p-02-inference-stack-tuning.md`](../plans/backlog/p-02-inference-stack-tuning.md)
§ Phase D1-D5 and § Phase D6 for the execution detail.
**Proposal-base commit**: `be5dbf6`. To gauge drift, run
`git diff be5dbf6..HEAD -- src/bffi_pipeline/config.py
src/bffi_pipeline/stages/judge.py docs/local-inference.md
docs/plans/backlog/p-02-inference-stack-tuning.md`.

Material updates since drafting:

- This commit — merged into the P-02 plan as Phase D. Motivation
  preserved here; the Approach / Prerequisites / Risks / Open-
  questions content moved into the plan, with the partial-order
  constraint **A → D1-D5 → B → C → D6** governing execution.

> **Numbering note**: the `P-04` shorthand here refers to *this
> proposal*. A separate plan with the same number (`backlog/p-04-m5-calibration.md`)
> covers M5 hyperparameter calibration — different concept. Cross-
> references in prose should be path-qualified
> (`docs/proposals/prop-04-...` vs `docs/plans/p-04-...`) to
> disambiguate.

## Motivation

P-02 introduces vllm-mlx alongside Ollama and explicitly keeps both:
Ollama as the dev / gold-set-eval default, vllm-mlx for production
batches. That's a defensible choice while the production switch is
unproven, but it locks in some recurring costs:

- **Divergence risk.** Sampler quirks, tokenizer edge cases, and
  per-backend prompt-template handling can cause the same model
  to behave subtly differently across the two backends. The
  preview-373 M6 stall (35 minutes silently waiting on Ollama,
  with a healthy TCP socket and CPU usage) is exactly the kind of
  incident that's hard to reason about when "is it the model or
  the server?" is on the table.
- **Two install paths.** Operators have to set up Ollama for dev
  and vllm-mlx for production. Each has its own model-conversion
  story (GGUF vs MLX 4-bit), its own model-management commands,
  and its own debug knobs.
- **Eval drift.** Gold-set evaluation runs on Ollama; production
  ships on vllm-mlx. If they ever drift, the gold-set's authority
  weakens (which is exactly the catch P-02 Phase A's parity bench
  protects against, but the protection has to be re-asserted every
  time models or backends update).
- **P-03 surface area.** The M6 stall watchdog has to handle hangs
  from whichever backend is in play. Reducing to one backend
  shrinks the failure-mode catalogue the watchdog has to cover.

If vllm-mlx is "good enough" for dev too — meaning the dev-loop
ergonomics don't kill iteration speed — consolidating on it removes
all four costs at once. The proposal is gated on answering that
ergonomics question.

## Merged into P-02 plan

Approach, prerequisites, risks, open questions, and the full
sequenced execution detail were absorbed into
[`docs/plans/backlog/p-02-inference-stack-tuning.md`](../plans/backlog/p-02-inference-stack-tuning.md)
as Phase D (the dev-loop consolidation work) and Phase D6 (the
eventual removal of Ollama install paths). The plan's partial
order is **A → D1-D5 → B → C → D6** — A is the vllm-mlx bring-up,
D1-D5 is everything this proposal called for short of removing
Ollama, and D6 is the deferred removal once Phase C has shipped
and 1-2 release cycles have passed without contributor
complaints.

This proposal stays in place as the historical record of the
consolidation argument; the actionable detail lives in the plan.
