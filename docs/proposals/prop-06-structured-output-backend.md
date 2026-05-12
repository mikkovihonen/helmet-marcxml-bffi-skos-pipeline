# P-06 — Server-side structured-output backend for the M6 / M9 cascade

**Status**: proposed.
**Scope**: 1-2 weeks.
**Proposal-base commit**: `e28bb9d`. The "Motivation" reasons about
the state of the pipeline immediately after P-02 A2/A3 shipped and
the first mlx-lm-only gold-set eval surfaced the structured-output
gap. If `main` has moved before this is acted on, re-verify with
`git diff e28bb9d..HEAD --
src/bffi_pipeline/llm_json_mode.py
src/bffi_pipeline/stages/judge.py
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/contrib_extract_llm.py
src/bffi_pipeline/title_lang_llm.py`.

## Motivation

The pipeline's four LLM call sites (M6 judge, M9 picker, M3
contributor extraction, M3 title-language cascade) all use
`langchain_openai.ChatOpenAI(...).with_structured_output(schema,
method="json_mode")`. Under Ollama this works because `format=json`
performs constrained decoding token-by-token — the server cannot
emit a token that would diverge from the schema. Under mlx-lm 0.31
neither `response_format: json_schema` nor `response_format:
json_object` does any constrained decoding; both are accepted
(HTTP 200) and ignored. The model then copies the few-shot prose
exemplars in `prompts/judge_v1.txt` and the cascade fell through to
`uncertain` on 100 % of the gold set during P-02 A5 smoke.

P-02 A5 resolved this **at the prompt layer**: a new helper module
`src/bffi_pipeline/llm_json_mode.py` derives a deterministic schema
instruction from the Pydantic model and appends it to each chain's
system message. Gold-set accuracy recovered to 88 % (mlx-lm-only,
0 % uncertain) without modifying the versioned `prompts/*.txt`
files.

The fix works but **leans on the model's instruction-following
rather than on a structural guarantee from the inference layer**.
That trade-off is fine for our Qwen3-8B / Qwen3-32B targets and
typical M6 / M9 prompt loads, but it does mean every model swap,
prompt tweak, and temperature setting needs a fresh gold-set check
to confirm structural validity. A server-side guarantee would let
us treat the JSON contract as load-bearing the same way Ollama did.

## Approach

Three candidate backends, none zero-cost. Each replaces or
supplements mlx-lm 0.31's `mlx_lm.server`:

1. **outlines + custom mlx-lm-backed HTTP server**: `outlines`
   (https://github.com/dottxt-ai/outlines) is a Python library for
   structured generation; `outlines.from_mlxlm(...)` wraps an
   mlx-lm model and exposes constrained generation against a
   Pydantic schema. Wrap that in a thin FastAPI server that
   honours OpenAI-style `response_format: json_schema`. Keeps
   the pipeline's `langchain_openai.ChatOpenAI` clients
   unchanged.
2. **vllm-mlx**: bundles outlines / lmformatenforcer-style
   guided decoding behind an OpenAI-compatible
   `response_format: json_schema` on a server that also has
   continuous batching, prefix caching, MTP-style speculative
   decoding. Considered and *rejected* during P-02 A1 on
   maintenance + dep-footprint grounds; this proposal is partly
   the re-evaluation question.
3. **Fork or upstream-PR mlx_lm.server** to honour
   `response_format: json_schema` via outlines internally.
   Structurally right; maintenance burden of keeping the fork in
   sync with upstream mlx-lm.

## Prerequisites

- P-02 fully shipped (at least through Phase C). The
  prompt-instruction approach has to actually work in production
  for a release cycle or two before we know whether we have a real
  problem to solve.
- A concrete incident that the prompt-instruction approach failed
  to catch — e.g. a model swap or temperature tweak that produced
  invalid JSON without the gold-set eval flagging it. If we never
  see that, the prompt-layer fix is sufficient and this proposal
  stays `proposed` indefinitely.

## Risks

- **Phase B / C regression**: any move off `mlx_lm.server` deletes
  the `--prompt-cache-size` / `--draft-model` knobs P-02 Phase B
  and C are built around. Option (1) means we'd re-implement
  prefix caching against outlines' generation interface (outlines
  does cache prefixes but the integration surface is different).
  Option (2) brings those features back in different flag names.
  Option (3) keeps them in place by construction.
- **OpenAI-compatibility drift**: options (1) and (3) each give
  us *our own* OpenAI-compatible surface. Subtle behavioural drift
  vs the canonical OpenAI server is possible — fewer clients in
  the wild exercising the corner cases.
- **Maintenance horizon**: P-02 A1's table flagged this for
  vllm-mlx specifically. Forking mlx-lm carries the same risk
  with sharper edges (no upstream churn margin).

## Open questions

- Does the prompt-instruction approach in
  `src/bffi_pipeline/llm_json_mode.py` actually hold up across a
  model swap (e.g. when Phase D6 ships and we trial a smaller
  primary)? If the helper produces ≥ 99 % structurally-valid JSON
  on the gold set under reasonable temperature and prompt
  variations, P-06 stays `proposed` — there's no incident
  motivating action.
- If we do act: which option? (1) is smallest blast radius and
  preserves the A1 mlx-lm decision. (2) is largest blast radius
  and re-opens A1. (3) is medium blast radius with permanent fork
  costs.
- Could `lm-format-enforcer` or `guidance` substitute for
  outlines? Both target the same constrained-decoding family.
  Open until we benchmark.
- Counterpoint: **don't do this**. The prompt-instruction fix is
  industry-standard for OpenAI-compatible clients in 2026;
  structured outputs were originally a workaround for older models
  that couldn't follow JSON instructions reliably, and Qwen3 / GPT-
  4o-class models can. If the gold-set eval keeps catching real
  drift, the prompt layer might be the right permanent home for
  the JSON contract.

## Cross-references

- [`docs/plans/in-progress/p-02-inference-stack-tuning.md`](../plans/in-progress/p-02-inference-stack-tuning.md)
  § "Open issues" — the resolved A5 entry that motivated this
  proposal; same section names the prompt-layer fix.
- [`src/bffi_pipeline/llm_json_mode.py`](../../src/bffi_pipeline/llm_json_mode.py)
  — the current fix this proposal would supersede.
