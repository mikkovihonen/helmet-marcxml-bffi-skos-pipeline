# P-04 — Consolidate on vllm-mlx (deprecate Ollama)

**Status**: proposed.
**Scope**: ~1-2 days if (and only if) the dev-loop ergonomics question
is answered positively — most of the work is a small wrapper around
`mlx_lm.server` and a documentation pass. Could expand to a week if
the dev-loop ergonomics turn out to need more than a thin wrapper.
**Proposal-base commit**: `be5dbf6`. To gauge drift, run
`git diff be5dbf6..HEAD -- src/bffi_pipeline/config.py
src/bffi_pipeline/stages/judge.py docs/local-inference.md
docs/plans/p-02-inference-stack-tuning.md`.

> **Numbering note**: the `P-04` shorthand here refers to *this
> proposal*. A separate plan with the same number (`p-04-m5-calibration.md`)
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

## Approach

Three sub-questions decide whether this proposal graduates into a
plan, and in what shape:

### Q1 — Can vllm-mlx swap models without restart?

Ollama's auto-swap (per-request model selection) is genuinely nice
for the dev loop where you flip between primary and fallback while
debugging. `mlx_lm.server` last I checked takes one `--model` at
startup; serving multiple models requires either:

- One server per model (different ports), with the calling code
  routing to the right port per request. The pipeline's
  `LLM_MODEL_PRIMARY` / `LLM_MODEL_FALLBACK` settings would need a
  per-model `LLM_BASE_URL` override.
- A small process supervisor (`mlx-model-pool` style) that owns
  multiple `mlx_lm.server` instances and exposes a single
  OpenAI-compatible endpoint that routes by model name.

The supervisor option matches Ollama's UX without the rest of
Ollama's footprint. If a small (~100 LOC) supervisor turns out to
be enough, the proposal stays cheap.

### Q2 — Can model acquisition match `ollama pull`?

`ollama pull qwen3:8b-q4_K_M` is one command. The vllm-mlx
equivalent (`python -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q
--q-bits 4 --mlx-path ...`) is one command too, just with more
parameters. Acceptable.

Either way: ship a `scripts/llm-pull.sh <model-tag>` wrapper that
hides the details so the dev-loop pull is back to one command.

### Q3 — Does the M5 Max (and dev machines below it) tolerate
vllm-mlx for the full dev loop?

vllm-mlx is targeted at server-class GPU machines; the MLX backend
is the Apple-Silicon variant and the M5 Max is its design target.
Smaller dev machines (M1 Pro, M2 Air) may struggle with the
continuous-batching overhead for the serial requests typical of dev
work. Validate against the actual dev-machine specs of the team
before assuming this is free.

### Migration path (if Q1-Q3 answers are favourable)

1. Bring up the model supervisor (if Q1 needs it). Ship it as
   `scripts/mlx-supervisor.sh` or a tiny Python script.
2. Update `.env.example` and `docs/local-inference.md` to make
   vllm-mlx the documented default for both dev and production.
   Ollama remains supported but no longer the default.
3. Run the gold-set eval under vllm-mlx-only and confirm parity
   with the historical Ollama baseline (the P-02 Phase A bench
   already pins this number for the production-model case;
   re-run it for the dev-model case).
4. Update `docs/local-inference.md` to label Ollama as
   "supported, not recommended" with a short rationale.
5. After ~1-2 release cycles with no complaints, remove the
   Ollama-specific paths from `local-inference.md` entirely and
   drop the Ollama-default fallback in `.env.example`.

## Prerequisites

- **P-02 Phase A has shipped.** vllm-mlx must be production-validated
  via the gold-set parity bench before we ask it to also replace the
  dev-loop default. Trying to consolidate before then risks landing
  a behavioural-divergence bug *and* removing the safety net of the
  Ollama fallback in the same change.
- **Q1-Q3 above answered**, each with a documented test rather than
  speculation:
  - Q1: a working multi-model setup (either supervisor or
    per-port) with a sub-second model-switch time.
  - Q2: a one-command pull wrapper that the dev loop is willing
    to use.
  - Q3: a benchmark of vllm-mlx serial throughput on the
    smallest dev machine the team uses; not just the M5 Max.
- **The model-supervisor design decision settled.** If we need a
  supervisor (likely yes per Q1), decide between writing our own
  thin one or adopting an upstream project that already does this
  for `mlx_lm.server`.

## Risks

- **Forced one-model-at-a-time discipline could break tests or
  scripts.** Some integration tests in `tests/integration/` exercise
  the cascade by flipping models mid-run; if these depend on
  Ollama's per-request model selection, they need refactoring. Audit
  before committing.
- **Higher install barrier for new contributors.** The current
  Ollama path is `brew install --cask ollama && ollama pull ...`.
  vllm-mlx is heavier: `mlx-lm` is a Python package with its own
  deps, model conversion is one extra step. Mitigation: make
  `make setup-llm` do the whole dance idempotently.
- **vllm-mlx upstream is younger and faster-moving than Ollama.**
  Version-pinning matters more. Mitigation: pin `mlx_lm` in
  `pyproject.toml`'s dev-tooling dependencies; treat upstream
  breaking changes as a "deal with it" task per release cycle.
- **Removing Ollama removes a fallback we currently use as a
  trivial smoke test** ("does Ollama work? then probably the
  pipeline can talk to *something*"). Mitigation: keep an
  Ollama-compatible path documented in `docs/local-inference.md`
  even after the default flips, so anyone debugging an LLM-stack
  issue can fall back manually.

## Open questions

- **Does Ollama actually need to go away, or just stop being the
  default?** A weaker form of this proposal — "vllm-mlx becomes the
  default for both dev and prod; Ollama support is unmaintained but
  not actively removed" — captures most of the consolidation win
  without forcing the question of removing the Ollama install
  instructions. Worth considering as the MVP form before the full
  deprecation.
- **Does Ollama gain prefix-caching support in the meantime?** If
  Ollama exposes prefix caching in its HTTP API at some point, much
  of P-02's motivation (and therefore P-04's) weakens. Roadmap-
  watch task before this proposal graduates.
- **Is there a `mlx_lm.server`-compatible upstream supervisor that
  matches Ollama's UX without us having to write one?** Worth a
  short literature search before committing.
- **Cost of debugging two backends in parallel during the
  transition.** While both backends are documented, contributors
  may run into "works on my Ollama, fails on your vllm-mlx" bugs.
  Mitigation: CI runs against one backend (vllm-mlx, once it's the
  default); the other backend gets a "best-effort" tag in docs.

## Cross-references

- [`docs/proposals/prop-02-inference-stack-tuning-for-M6.md`](prop-02-inference-stack-tuning-for-M6.md)
  + [`docs/plans/p-02-inference-stack-tuning.md`](../plans/p-02-inference-stack-tuning.md)
  — the proposal + plan that introduces vllm-mlx alongside Ollama.
  This proposal builds on whatever P-02 ships.
- [`docs/proposals/prop-03-m6-stall-watchdog.md`](prop-03-m6-stall-watchdog.md)
  — the watchdog whose failure-mode catalogue shrinks if we have
  one backend instead of two.
- [`docs/local-inference.md`](../local-inference.md) — the document
  that absorbs the default-flip if this proposal graduates.
