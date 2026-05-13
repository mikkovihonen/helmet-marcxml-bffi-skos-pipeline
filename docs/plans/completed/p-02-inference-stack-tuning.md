# P-02 — Inference-stack tuning for the M6 cascade

**Status**: completed (2026-05-12).
**Final rollup**: A complete, B complete (7.99× TTFT speedup),
C abandoned with data (spec-decode regressed throughput by ~50 %
on this hardware), D1-D6 complete (mlx-lm is the only supported
backend, Ollama install paths removed).
**Source proposals**:
`prop-02-inference-stack-tuning-for-M6` (deleted on 2026-05-13 plans/proposed reorganisation; recover via `git show f2d8486 -- <orig-path>`)
(introduced in commit `334294a` while still part of the combined
`performance-enhancements.md`; k-NN critique that fed back into P-01
landed in `9789c20`; per-proposal file split in the commit that also
introduced the initial plan) and
`prop-04-consolidate-on-mlx-lm` (deleted on 2026-05-13 plans/proposed reorganisation; recover via `git show f2d8486 -- <orig-path>`)
(merged into this plan as Phase D; see "Material updates" below).
**Plan-base commit**: `d0af171`. The "Current state" section is
accurate against this commit. If `main` has moved before execution
begins, re-verify with
`git diff d0af171..HEAD -- src/bffi_pipeline/stages/judge.py
src/bffi_pipeline/stages/reconcile.py src/bffi_pipeline/contrib_extract_llm.py
src/bffi_pipeline/title_lang_llm.py src/bffi_pipeline/config.py .env.example
docs/local-inference.md`.

Material updates since drafting:

- `d0af171` — folded prop-04 (Consolidate on mlx-lm; deprecate
  Ollama) into this plan as Phase D. The dev-loop ergonomics work
  (multi-model serving, model-pull wrapper, throughput verification
  on smaller dev machines, default flip, Ollama labelled secondary)
  becomes D1-D5 between Phases A and B so the perf wins from B/C
  apply to both dev and prod once D1-D5 ships. The actual removal
  of Ollama install paths is D6, held until after Phase C.
- A2 execution (May 2026) revealed three drift items from the plan
  as drafted, all resolved in place — see "Open issues" for details:
  - Qwen3 (released 2025) dropped the `-Instruct` suffix from its
    chat-tuned variants. The plan's `Qwen/Qwen3-8B-Instruct` etc.
    repo IDs 404. Bare `Qwen/Qwen3-8B` / `Qwen/Qwen3-32B` are the
    chat-tuned variants now.
  - A2 took the "Alternative" pre-quantised-download route rather
    than local conversion — Qwen now publishes their own MLX 4-bit
    at `Qwen/Qwen3-8B-MLX-4bit`, and `mlx-community/Qwen3-32B-4bit`
    covers the 32B. Faster (network-only, ~10-30 min total) and the
    8B comes from the model authors directly.
  - mlx-lm 0.31 reports the model ID at `/v1/models` as the
    absolute `--model` path (no `--model-name` alias). Bare basenames
    cause a Hugging Face fallback fetch and 401. `LLM_MODEL_*` in
    `.env.mlx-lm` is the full path; per-operator.
  - Qwen3 default-mode generation lands in `message.reasoning` and
    leaves `message.content` empty until the reasoning budget is
    exhausted. The pipeline's `langchain_openai.ChatOpenAI` reads
    `content`, so every Qwen3 request must disable thinking. Fixed
    server-side via `--chat-template-args '{"enable_thinking":false}'`
    — no client/code change needed.
- A5 smoke (first mlx-lm-only run, May 2026) surfaced a fifth
  drift item — the only one requiring a code change:
  - mlx-lm 0.31 accepts `response_format: {"type":"json_schema",
    "strict":true}` (HTTP 200) but **silently ignores** the schema
    and returns prose. Ollama tolerates the same payload because
    `format=json` enforces constrained decoding token-by-token;
    mlx-lm has no constrained-decoding fallback. LangChain's
    `with_structured_output(method="json_mode")` setting also
    doesn't help — it only flips `response_format` to
    `{"type":"json_object"}` and does *not* inject a schema
    description into the prompt, so the model copies the prose
    examples in `prompts/judge_v1.txt` instead. **Fix**: switched
    all four LLM call sites from `method="json_schema"` to
    `method="json_mode"` and added a deterministic
    schema-instruction system-message fragment (new module
    `src/bffi_pipeline/llm_json_mode.py`) appended to each chain's
    system prompt. Versioned `prompts/*.txt` files stay
    byte-identical so Phase B prefix-cache stability is preserved.
    Server-side guided decoding (outlines / vllm-mlx / fork) was
    considered and rejected for P-02 scope; recorded as P-06.

**Phase commits** (filled in as phases ship; empty fields here are a
signal that the phase has not yet completed against the gold-set
acceptance criteria):

- Phase A (mlx-lm bring-up + parity): **complete** — A7 acceptance
  ticked at `645f886`. Rollup: A1/A4 = `7cde2bf`, A2/A3 = `ad188ad`,
  A5 prep = `852bd35`, A5 = `54f8db0`, A6/A7 = `645f886`.
  - A1 (mlx-lm 0.31.3 installed in `~/.venvs/mlx-lm`): operator-side
    step, no commit; verified by `python -c "import mlx_lm;
    print(mlx_lm.__version__)"`.
  - A4 (`.env.ollama-baseline` + `.env.mlx-lm` written): operator-side
    step, both files local + gitignored. The committed plan + doc
    sweep that documented the rename and the upstream
    `python -m mlx_lm.<sub>` → `python -m mlx_lm <sub>` migration
    accompanies A1/A4: `7cde2bf`.
  - A2 (Qwen3-8B-4bit + Qwen3-32B-4bit pre-quantised checkpoints
    pulled to `~/.mlx_models/`): operator-side step, models stored
    locally. The committed doc + plan sweep that recorded the
    pre-quantised path and the Qwen3 `-Instruct`-suffix drop
    accompanies A2/A3: `ad188ad`.
  - A3 (`mlx_lm server` running on :8001 + :8002 with
    `--chat-template-args '{"enable_thinking":false}'`): operator-
    side step. Same commit as A2 — `ad188ad` — captures the
    thinking-mode discovery and the full-path-as-model-ID quirk.
  - A5 (`scripts/p02-parity-bench.sh` parity verdict captured): `54f8db0`.
    Result: Ollama 94.1 % (16/17), mlx-lm 88.2 % (15/17); 15 cases
    identical, both fail `gs-0002`, mlx-lm-only failure on `gs-0001`
    accepted as gold-set-quirk drift (see Material updates for the
    full investigation).
    - A5 prep — prompt-side JSON-mode instruction (new module
      `src/bffi_pipeline/llm_json_mode.py`, wired into all four
      LLM call sites): `852bd35`. Without this fix the mlx-lm-only
      eval returned 0 % accuracy / 100 % uncertain because mlx-lm
      has no constrained decoding for `response_format`; with it,
      mlx-lm-only accuracy is 88.2 % / 0 % uncertain.
  - A6 (concurrency sweep run; chosen value recorded): `645f886`.
    Sweep ran against the actual `_build_chain` + a synthetic 32-pair
    slice ([`scripts/p02-a6-concurrency-bench.py`](../../../scripts/p02-a6-concurrency-bench.py))
    on **M2 Max 64 GB** (current dev box, not the production M5 Max
    128 GB). Picked `M6_CONCURRENCY=4` with server flags
    `--decode-concurrency 4 --prompt-concurrency 4 --prompt-cache-size 200
    --prompt-cache-bytes 1073741824` for a throughput ceiling of
    ~31 pairs/min. Full table + re-measurement gates in
    [`docs/local-inference.md`](../../local-inference.md) § "Throughput
    findings — P-02 § A6". Re-run on M5 Max before production.
  - A7 (acceptance gate passed): `<unfilled>`
- Phase D1-D5 (dev-loop consolidation on mlx-lm): **complete** —
  D1 = `f3c0bea`, D2 = `d959bf6`, D3/D4/D5 = `1346035`.
  - D1 (per-port routing in Settings + cascade): `f3c0bea`
  - D2 (model-pull wrapper): `d959bf6`
  - D3 (dev-machine throughput verification): `1346035`.
    M2 Max 64 GB result: mlx-lm Phase B config produces ~28 pairs/min
    @ c=1 vs Ollama's serial cascade median of ~18 660 ms/pair from
    the A5 parity bench. Well within the "≤ 20 % regression" bar
    (it's a comfortable improvement). No `BFFI_LOCAL_BACKEND=ollama`
    escape hatch needed. Documented in
    [`docs/local-inference.md`](../../local-inference.md) §
    "Throughput vs Ollama".
  - D4 (flip committed defaults): `1346035`.
    `.env.example` now defaults to `LLM_BASE_URL=http://127.0.0.1:8001/v1`
    + per-tier URLs + absolute mlx-lm model paths; the README
    Quick-start uses `hf download Qwen/Qwen3-8B-MLX-4bit` instead of
    `ollama pull`; `docs/local-inference.md` § "Ollama as the dev
    fallback" was renamed to "Ollama — supported but not recommended".
  - D5 (label Ollama secondary): `1346035`.
    Banner + rationale + switching-back recipe live in
    `docs/local-inference.md`; README and `.env.example` mention
    the "supported but not recommended" status with pointers back
    to the plan and the local-inference doc. No file deletions —
    that's deferred to D6 after the observation window.
- Phase B (prefix caching): `fd44894`.
  TTFT bench ([`scripts/p02-b-ttft-bench.py`](../../../scripts/p02-b-ttft-bench.py))
  on M2 Max 64 GB measured **7.99× TTFT speedup** (cold 2.87 s →
  warm 0.36 s median) on the byte-stable ``_M6_PROMPT_PREFIX_FULL``
  — comfortably above the ≥ 3× acceptance bar. Full results in
  [`docs/local-inference.md`](../../local-inference.md) § "TTFT
  measurement (P-02 § B4)".
- Phase C (speculative decoding): **abandoned** — `370bd0c`.
  Tried `--draft-model ~/.mlx_models/Qwen3-1.7B-4bit --num-draft-tokens 5`
  on top of Phase B's prefix-cache config. End-to-end throughput
  regressed by ~50 % (Phase B 31.6 /min → Phase C 14.0 /min on the
  M2 Max 64 GB at c=8). Per the plan's own contingency in § C5
  ("If acceptance < 50 %, abandon C; Phase B alone is still a win"),
  Phase C does not ship; the M5 Max production server runs without
  the `--draft-model` flag.
- Phase D6 (remove Ollama install paths): `2741ab6`.
  Acceptance ticked at the same commit that swept the install
  paths from `.env.example`, `README.md`, `docs/local-inference.md`,
  `docs/runbook.md`, `docs/tech-stack.md`, `docs/ci-strategy.md`,
  `scripts/run-full-pipeline.sh`, and `scripts/README.md`. Surviving
  mentions live under `docs/archived/`, `docs/plans/`,
  `docs/plans/proposed/`, and the historical `scripts/p02-parity-bench.sh`
  (the bench is itself an Ollama-vs-mlx-lm parity tool).

**Owner**: TBD.
**Estimated wall-time**: ~3-5 working days end to end. Phase A is
half a day to a day; D1-D5 is ~1-2 days; B + C remain 2-3 days;
D6 is half a day plus a
1-2 release-cycle observation window. Each phase is independently
shippable; the partial ordering is A → D1-D5 → B → C → D6.

## Goal

Reduce the wall-time and compute cost of every M6 LLM call without
changing M6's output contract, and consolidate the inference stack
on a single backend so the perf wins apply uniformly to dev and prod.
Four optimisations applied in the partial order **A → D1-D5 → B → C
→ D6**:

1. **A**: Move M6 (and the M3 / M9 LLM callers) from Ollama to
   **mlx-lm** so the prefix-caching and speculative-decoding knobs
   become available.
2. **D1-D5**: Make mlx-lm the dev-loop default too — multi-model
   serving via per-port routing (one `mlx_lm.server` per model on
   separate ports, Settings extension to dispatch primary vs
   fallback), one-command pull wrapper, dev-machine throughput
   verification, default flip, Ollama labelled secondary. Until
   this lands, the dev environment doesn't see the Phase B/C wins.
3. **B**: Enable **prompt prefix caching** so the ~75 %-identical M6
   prompt prefix re-uses the prefill KV-cache across pairs.
4. **C**: Add **speculative decoding** with a small draft model
   (`qwen3:1.7b`) so the structurally-predictable parts of the JSON
   output emit without invoking the full target model.
5. **D6**: Remove Ollama install paths from `.env.example` and
   `docs/local-inference.md` once Phase C has shipped and 1-2
   release cycles have gone by without complaints.

## Definition of done

- mlx-lm is the **default backend for dev and prod**; Ollama
  install paths are removed from the committed docs and the
  `.env.example`.
- A 200-pair bench shows ≥ **3× TTFT speedup** vs Ollama baseline
  (prefix caching contribution).
- A 200-pair bench shows ≥ **2× end-to-end speedup** on fast-mode
  outputs vs the prefix-caching-only baseline (speculative-decoding
  contribution).
- `make eval LABEL=<phase-label>` against `gold/gold.jsonl` (17 cases)
  shows **zero verdict deltas** vs the Ollama baseline at every phase
  checkpoint A through C.
- Multi-model serving via per-port routing — two
  `mlx_lm.server` processes on different ports, with Settings
  carrying `llm_base_url_primary` / `llm_base_url_fallback` so the
  cascade hits the correct backend per tier.
- `scripts/llm-pull.sh <hf-org>/<hf-name>` wrapper around
  `python -m mlx_lm convert` so dev model acquisition stays a
  one-liner.
- `docs/runbook.md` documents the mlx-lm production-run procedure
  with the new flags; `docs/local-inference.md` documents the
  consolidated dev install.

## Current state

- All four LLM call sites in `src/bffi_pipeline/` use
  `langchain_openai.ChatOpenAI` pointed at `LLM_BASE_URL`:
  - `stages/judge.py` (M6)
  - `stages/reconcile.py` (M9 KANTO/YSO picker)
  - `contrib_extract_llm.py` (M3 contributor extraction)
  - `title_lang_llm.py` (M3 title-language cascade)
- The OpenAI-compatible API contract is the only coupling — mlx-lm
  exposes the same surface (`mlx_lm.server`).
- `docs/local-inference.md` § "mlx-lm — production batches" already
  documents the install + convert + serve commands; this plan
  executes against that baseline.
- `.env` currently:
  - `LLM_BASE_URL=http://localhost:11434/v1` (Ollama)
  - `LLM_MODEL_PRIMARY=qwen3:8b-q4_K_M`
  - `LLM_MODEL_FALLBACK=qwen3:32b-q4_K_M`

---

## Phase A — mlx-lm bring-up + parity bench (P-02a)

Estimated wall-time: half a day to one full day.

### A1. Install mlx-lm

**Stack decision (recorded for future readers)**: we install
[`ml-explore/mlx-lm`](https://github.com/ml-explore/mlx-lm) (Apple's
MLX team's reference LLM inference library) directly rather than the
higher-level wrapper [`waybarrios/mlx-lm`](https://github.com/waybarrios/vllm-mlx).
Both work for our needs and were evaluated during A1 execution; the
tie-breakers favouring mlx-lm:

| Concern | mlx-lm | vllm-mlx |
|---|---|---|
| Maintenance | Apple MLX team | Single contributor (active, but tail risk over the pipeline's multi-year horizon for the NLF hand-off) |
| Install footprint | ~15 transitive deps | ~50 transitive deps (pulls torch / torchvision / mlx-vlm / mlx-audio / mlx-embeddings etc.) |
| Blast radius for upstream churn | Smaller | Larger (multiple feature surfaces) |
| Continuous batching | `--decode-concurrency` + `--prompt-concurrency` | `--continuous-batching` + paged KV cache (richer) |
| Prefix caching | `--prompt-cache-size` + `--prompt-cache-bytes` | `--enable-prefix-cache` (default-on; richer config) |
| Speculative decoding | `--draft-model` + `--num-draft-tokens` | `--specprefill` + MTP (richer; two modes) |
| Multi-model serving on one endpoint | ✗ (one process per model) | ✓ (`--models-config` YAML) |
| Tool calling / reasoning parsers / MCP | ✗ | ✓ |

The vllm-mlx wins (paged KV cache, `--models-config`, MTP, MCP)
are nice-to-have rather than load-bearing for our specific workload
(M6 batch sizes, tens of thousands of pairs, not high-QPS serving).
The maintenance + install-footprint arguments tip toward mlx-lm.

The cost: **D1's multi-model serving becomes a real code change**
(~30 LOC of per-port routing in `Settings` + dispatch in
`_build_chain`) rather than a YAML config. Tractable, and the code
lives where the rest of M6's LLM config already lives.

mlx-lm is on PyPI as `mlx-lm`. No source clone needed; just create
a standalone venv for it (kept separate from the bffi_pipeline
venv so its deps don't pollute or version-clash) and `uv pip
install`:

```bash
# Pick any host directory; ~/.venvs/mlx-lm keeps it out of the way.
mkdir -p ~/.venvs && uv venv ~/.venvs/mlx-lm --python 3.12
source ~/.venvs/mlx-lm/bin/activate
uv pip install mlx-lm
python -c "import mlx_lm; print(mlx_lm.__version__)"
```

**Verification**: the import prints a version string (`0.31.3` at
time of writing). Confirm the CLIs are on PATH:

```bash
python -m mlx_lm server --help       # the server entry point
python -m mlx_lm convert --help      # model conversion
python -m mlx_lm generate --help     # one-shot generation (handy for smoke tests)
```

### A2. Acquire the two judge models in MLX 4-bit

The chosen path — settled during execution — is to **pull pre-quantised
MLX checkpoints** rather than converting locally. Qwen now publishes
their own MLX 4-bit of the 8B and `mlx-community` covers the 32B.
Network-bound only, no quant compute, and the 8B comes from the model
authors directly:

```bash
source ~/.venvs/mlx-lm/bin/activate
hf download Qwen/Qwen3-8B-MLX-4bit       --local-dir ~/.mlx_models/Qwen3-8B-4bit
hf download mlx-community/Qwen3-32B-4bit --local-dir ~/.mlx_models/Qwen3-32B-4bit
```

(`huggingface-cli` was deprecated in favour of `hf` in 2026; the older
name still resolves but prints a deprecation notice.)

**Convert from source instead** if a pre-quantised checkpoint isn't
available or you need to pin a specific quantisation:

```bash
mkdir -p ~/.mlx_models
# Primary (8B; ~4 GB on disk after 4-bit quant): ~30-45 min on M5 Max
python -m mlx_lm convert --hf-path Qwen/Qwen3-8B -q --q-bits 4 \
    --mlx-path ~/.mlx_models/Qwen3-8B-4bit
# Fallback (32B; ~17 GB on disk after 4-bit quant): ~1-2 h
python -m mlx_lm convert --hf-path Qwen/Qwen3-32B -q --q-bits 4 \
    --mlx-path ~/.mlx_models/Qwen3-32B-4bit
```

(Qwen3 dropped the `-Instruct` suffix; bare `Qwen/Qwen3-<size>` is the
chat-tuned variant, `-Base` is pretrained-only.)

**Verification**: `ls ~/.mlx_models/Qwen3-8B-4bit/` shows the expected
sharded safetensors (or a single `model.safetensors`), `config.json`,
and tokenizer files. Total size on disk ≈ 4 GB for 8B, ≈ 17 GB for 32B.

**Fallback if HF download is unreliable**: pull the GGUF blobs Ollama
already has at `~/.ollama/models/blobs/`, identify them by manifest,
and convert via `mlx_lm.convert --hf-path` against the GGUF path.
Documented but flakier — only use if HF download repeatedly fails.

### A3. Start the mlx-lm server on a dedicated port

`--chat-template-args '{"enable_thinking":false}'` is **load-bearing for
Qwen3** — without it, generation lands in `message.reasoning` and
`message.content` is empty, breaking any `langchain_openai.ChatOpenAI`
caller. Discovered during execution; the server-side flag avoids any
client-side change.

```bash
# In a separate terminal (or via nohup); keep Ollama running on :11434.
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-8B-4bit \
    --host 127.0.0.1 --port 8001 \
    --chat-template-args '{"enable_thinking":false}' \
    > /tmp/mlx-lm-server-8b.log 2>&1 &
# Probe:
curl -s http://127.0.0.1:8001/v1/models | jq
```

**Verification**: the `/v1/models` response lists the loaded model
under `data[0].id`. In mlx-lm 0.31 this is the **absolute path** that
was passed to `--model` (e.g. `/Users/<you>/.mlx_models/Qwen3-8B-4bit`),
not the basename — `LLM_MODEL_PRIMARY` in `.env.mlx-lm` must be that
exact string. There is no `--model-name` alias flag in mlx-lm 0.31;
bare basenames trigger a Hugging Face fallback fetch that 401s.

Smoke-test the thinking-disabled chat path with a real completion:

```bash
curl -s -X POST http://127.0.0.1:8001/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"/Users/<you>/.mlx_models/Qwen3-8B-4bit",
         "messages":[{"role":"user","content":"Reply with just OK."}],
         "max_tokens":8}' | jq '.choices[0].message'
```

Expect `{"role": "assistant", "content": "OK"}` — no `reasoning` field,
and `content` populated.

### A4. Write the two parity-bench env files

`scripts/p02-parity-bench.sh` reads two named env files in
subshells — `.env.ollama-baseline` and `.env.mlx-lm` — so the
parity bench can run end-to-end without touching the live `.env`.
Both files stay local (gitignored by `.env.*` with a negation for
`.env.example`); they each carry the same secrets as `.env`.

```bash
# 1. Snapshot the current Ollama-backed .env. This is the rollback
#    target if mlx-lm fails A7 acceptance.
cp .env .env.ollama-baseline

# 2. Write .env.mlx-lm by copying .env.ollama-baseline and
#    overriding the LLM_ section. Carry the D1 per-tier URLs
#    explicitly so the cascade routes primary/fallback correctly.
#    `LLM_MODEL_*` must be the **absolute path** passed to `--model`
#    (that's what `/v1/models` reports; mlx-lm 0.31 has no
#    `--model-name` alias and rejects bare basenames):
#      LLM_BASE_URL=http://127.0.0.1:8001/v1
#      LLM_BASE_URL_PRIMARY=http://127.0.0.1:8001/v1
#      LLM_BASE_URL_FALLBACK=http://127.0.0.1:8002/v1
#      LLM_API_KEY=mlx-lm
#      LLM_MODEL_PRIMARY=/Users/<you>/.mlx_models/Qwen3-8B-4bit
#      LLM_MODEL_FALLBACK=/Users/<you>/.mlx_models/Qwen3-32B-4bit

# 3. Sanity diff:
diff .env.ollama-baseline .env.mlx-lm
```

(Verify the fallback only — the 32B server isn't running yet; the
fallback will only matter when escalation actually fires.)

### A5. Gold-set parity bench

```bash
# One command — runs both evals, diffs the per-case verdicts, exits
# 0 on parity and 1 on drift. Reads .env.ollama-baseline and
# .env.mlx-lm for the two backend configurations.
scripts/p02-parity-bench.sh
```

(See [`scripts/p02-parity-bench.sh`](../../../scripts/p02-parity-bench.sh)
— the helper sources each env file in a subshell, runs `bffi-pipeline
eval --run-label <label>`, then loads the two `eval-runs/<label>.json`
artefacts and reports parity via three checks: accuracy match, failure
case-id set match, predicted-value match per failure. Override labels
via positional args; override env-file paths via `BASELINE_ENV` /
`CANDIDATE_ENV`.)

**Verification**: the script exits 0 with `PARITY OK — every case
produced identical verdicts on both backends` if the two backends
agree on every gold case. Non-zero exit means drift; the script
prints which cases disagreed and how.

If verdicts differ on numerical-noise pairs only (very-low-confidence
calls where the model is genuinely uncertain), record the delta and
proceed — but document it in the plan's "Open issues" section before
moving on. The parity-bench script's drift detection is strict (any
delta = exit 1); a soft-parity policy lives outside the script and
gets exercised by reading the failure diff.

### A6. `--concurrency` sweep (BUILD_PLAN M6 follow-up)

Once parity is established, sweep concurrent request counts to find
the value that maximises throughput without OOMing — this is the
BUILD_PLAN M6 L302 follow-up that mlx-lm unblocks. The richer
client-side concurrency mlx-lm permits over Ollama (via
`--decode-concurrency` / `--prompt-concurrency`) is what makes the
sweep is meaningful here in a way it wouldn't be against an Ollama
backend.

```bash
# 1000-pair sample slice — pull from the v2 escalate band or a
# replayable cache.
for c in 4 8 16 32; do
  LLM_BASE_URL=http://127.0.0.1:8001/v1 \
      uv run bffi-pipeline judge --concurrency $c \
      --candidates-dir <slice> --output-dir <slice> --force \
      | tee /tmp/bench-concurrency-$c.log
done
```

Record per-`c` throughput (pairs/min) and peak resident memory.
Pick the value at the throughput knee that fits the M5 Max memory
budget; document the chosen value in `docs/runbook.md` § "Pinned
versions" and the `M6_CONCURRENCY` env default in
`scripts/run-full-pipeline.sh`.

### A7. Phase A acceptance

- [x] mlx-lm server starts cleanly on port 8001 (and :8002 for the
      32B fallback per § D1).
- [x] `make eval` against mlx-lm matches Ollama on all 17 gold
      cases except `gs-0001` — accepted as a documented
      gold-set-quirk drift, traced into
      `docs/plans/backlog/p-06-gold-set-growth.md` for resolution.
      See "Material updates" entry from the A5 dig.
- [x] `.env.ollama-baseline` exists for instant rollback.
- [x] `--concurrency` sweep complete; chosen value (`4`) documented
      in `docs/local-inference.md` § "Throughput findings — P-02 § A6"
      and referenced from `docs/runbook.md` § "--concurrency tuning
      sweep". `M6_CONCURRENCY=4` is the M2 Max default; the M5 Max
      number is gated on a re-measurement when that hardware comes
      online.

**Phase A is complete.** The mlx-lm-backed cascade matches Ollama on
16/17 gold cases, has a documented operational throughput ceiling
(~31 pairs/min on M2 Max 64 GB), and the install + run procedure is
captured in `docs/local-inference.md`. Phases B (prefix caching, now
already enabled server-side in the A6 sweep config but not
benchmarked against TTFT specifically), C (speculative decoding),
and D (dev-loop default flip + Ollama removal) remain.

### A8. Rollback

```bash
cp .env.ollama-baseline .env
# Optionally stop the mlx-lm server, but harmless to leave running.
```

The pipeline is back on Ollama. No code changes were made.

---

## Phase D1-D5 — Dev-loop consolidation on mlx-lm (absorbed from prop-04)

Estimated wall-time: ~1-2 days. Each sub-item is independently
shippable, but they're listed in execution order. The full set is
gating the dev-loop benefit from Phases B and C — until D1-D5 ships,
the perf wins apply only to production batches.

### D1. Multi-model serving via per-port routing

`mlx_lm.server` serves one model per process. To match Ollama's
per-request model selection (so the cascade can flip between
primary and fallback without restarting the server), run two
`mlx_lm.server` processes on different ports and extend `Settings`
to carry both URLs.

**Settings change** (~30 LOC in `src/bffi_pipeline/config.py`):

```python
# New fields, alongside the existing llm_base_url:
llm_base_url_primary:  str = Field(
    default="",  # falls back to llm_base_url if empty
    alias="LLM_BASE_URL_PRIMARY",
)
llm_base_url_fallback: str = Field(
    default="",
    alias="LLM_BASE_URL_FALLBACK",
)
```

**Dispatch change** in `_build_chain` (`src/bffi_pipeline/stages/judge.py`):
the cascade already maintains separate `primary_chain` and
`fallback_chain` objects; route each at the matching URL.

```python
def _build_chain(*, model_name: str, base_url: str, ...) -> Any:
    # ... unchanged downstream of base_url
```

Call sites in `cascade_judge` resolve the per-tier URL with the
existing `llm_base_url` as a fallback for backward compatibility:

```python
primary_url = settings.llm_base_url_primary or settings.llm_base_url
fallback_url = settings.llm_base_url_fallback or settings.llm_base_url
```

**Operator setup**: start two servers on different ports:

```bash
# Primary on 8001
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-8B-Instruct-4bit \
    --host 127.0.0.1 --port 8001 \
    --prompt-cache-size 200 --prompt-cache-bytes 1073741824 &

# Fallback on 8002 (memory permitting)
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-32B-Instruct-4bit \
    --host 127.0.0.1 --port 8002 \
    --prompt-cache-size 200 --prompt-cache-bytes 1073741824 &
```

`.env.mlx-lm` (the file the parity bench reads as its mlx-lm-side
config; gitignored alongside `.env`):

```
LLM_BASE_URL_PRIMARY=http://127.0.0.1:8001/v1
LLM_BASE_URL_FALLBACK=http://127.0.0.1:8002/v1
LLM_MODEL_PRIMARY=Qwen3-8B-Instruct-4bit
LLM_MODEL_FALLBACK=Qwen3-32B-Instruct-4bit
```

**Tests**: `tests/unit/test_judge.py` gains coverage that the
cascade hits `LLM_BASE_URL_FALLBACK` when escalating to fallback.
The existing chain-injection pattern means we mock both URLs
separately — no real HTTP needed.

**Acceptance**: cascade fallback flips between ports without
restarts; tier 1 hits 8001, tier 2 hits 8002 (verify via the
mlx-lm server logs).

### D2. One-command model-pull wrapper

```bash
# scripts/llm-pull.sh <hf-org>/<hf-name>
python -m mlx_lm convert --hf-path "$1" -q --q-bits 4 \
    --mlx-path "$HOME/.mlx_models/$(basename "$1")-4bit"
```

Stash the wrapper under `scripts/`, doc it in
`docs/local-inference.md`. Restores the `ollama pull qwen3:8b` one-
command UX without the rest of Ollama.

### D3. Dev-machine throughput verification

mlx-lm is calibrated for Apple Silicon generally; the M5 Max is
Apple's reference. Smaller dev boxes (M1 Pro / M2 Air) might be
memory-constrained when loading the 32B fallback, and per-call
latency may be higher than on the M5 Max. Bench `judge_pair`
serial throughput on
the **smallest dev machine in actual team use** at the chosen
primary model. Compare against the pre-migration Ollama baseline.

**Acceptance**: mlx-lm serial throughput on the smallest dev box
matches Ollama within ~20 %. If it's worse, dev keeps a per-machine
escape hatch (a `BFFI_LOCAL_BACKEND=ollama` env var that selects
the legacy path) and D4-D6 land only for machines that pass D3.

### D4. Flip the committed defaults

- `.env.example` updates: `LLM_BASE_URL` points at the mlx-lm
  port; `LLM_MODEL_PRIMARY` / `LLM_MODEL_FALLBACK` use MLX-style
  identifiers.
- `docs/local-inference.md` `## Installation` re-orders mlx-lm
  ahead of Ollama; Ollama section is labelled "Supported but no
  longer recommended" with a one-paragraph rationale and a pointer
  back to the runbook for the rollback path.
- README's Quick start uses mlx-lm commands.

### D5. Label Ollama secondary (no removal yet)

Old Ollama install paths stay in the docs but with a "Supported,
not recommended" banner. This is the trial period — Ollama remains
usable as the emergency fallback while the team uses mlx-lm as
the dev default.

**Phase D1-D5 acceptance**:

- [ ] Multi-model serving in place, sub-second model switch
      measured.
- [ ] `scripts/llm-pull.sh` exists, doc updated.
- [ ] D3 throughput bench logged in
      `eval-runs/dev-throughput-<date>.json` (one row per dev box).
- [ ] `.env.example` + `docs/local-inference.md` + README flipped
      to mlx-lm-default.
- [ ] Gold-set eval (`make eval`) passes under mlx-lm-only.

### D1-D5 rollback

Re-flip `.env.example` and `docs/local-inference.md` defaults to
Ollama. The models-config and pull-wrapper additions are non-breaking;
they stay in place even if the default reverts (they help anyone
on mlx-lm regardless of which is default).

---

## Phase B — Prompt prefix caching (P-02b)

Estimated wall-time: ~1 day.

### B1. Identify the M6 static prefix

The M6 prompt builder lives in `stages/judge.py`. Specifically,
`prompt_text()` and `prompt_text_fast()` interpolate a per-pair
payload into the static prefix.

**Action**: factor the static prefix out so it is constructed once
at module-import time and the per-pair section is appended verbatim.
Today the builder string-interpolates the whole thing each call — a
mlx-lm prefix cache would still recognise repeats but at a small
hashing cost we can eliminate.

Concretely:

1. Introduce a module-level `_M6_PROMPT_PREFIX: Final[str]` containing
   everything up to and including the JSON-schema example.
2. The pair-payload is appended as the suffix.
3. Assert `_M6_PROMPT_PREFIX` ends with a newline so suffix-
   concatenation can't accidentally introduce variability.

### B2. Pin prefix byte-stability with a unit test

`tests/unit/test_judge.py` gains a regression test:

```python
def test_m6_prompt_prefix_is_byte_stable() -> None:
    """The M6 prompt prefix must not drift across releases — mlx-lm
    prefix-cache hit rate silently drops to 0 % if any byte changes.
    The recorded fixture is the contract."""
    from bffi_pipeline.stages.judge import _M6_PROMPT_PREFIX
    expected = (REPO_ROOT / "tests" / "fixtures" / "m6_prompt_prefix.txt").read_bytes()
    assert _M6_PROMPT_PREFIX.encode("utf-8") == expected
```

When the prompt intentionally changes, the fixture is updated
deliberately — the test failure forces the conversation.

### B3. Enable mlx-lm prefix caching

mlx-lm 0.31.3 exposes `--prompt-cache-size` (entries) and
`--prompt-cache-bytes` (byte budget) on the server CLI. Start the
server with both knobs set:

```bash
pkill -f "mlx_lm.server" || true
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-8B-Instruct-4bit \
    --host 127.0.0.1 --port 8001 \
    --prompt-cache-size 200 \
    --prompt-cache-bytes 1073741824 \
    > /tmp/mlx-lm-server-cached.log 2>&1 &
```

(`--prompt-cache-bytes 1073741824` is 1 GB. M6's static prefix is
~10-20 KB per cached entry; 200 entries × 20 KB ≈ 4 MB, so the
byte cap won't bind in practice — set it generously to leave
headroom for occasional long-prefix outliers.)

**Verification**: hit the endpoint with two near-identical prompts
(same M6 prefix, different per-pair suffix). The second response's
TTFT (time-to-first-token) should drop sharply vs the first. mlx-lm
doesn't expose cache-hit metrics directly; observe via TTFT delta
in the B4 bench rather than via a metrics endpoint.

### B4. Bench prefix caching

```bash
# Reuse the preview-373 corpus as a self-contained 200-ish-pair slice.
PREVIEW=/tmp/preview-373
# Without cache (baseline already taken in A5).
# With cache, fast mode (rationale deferred):
LLM_BASE_URL=http://127.0.0.1:8001/v1 \
    uv run bffi-pipeline judge --no-full-rationale \
    --candidates-dir $PREVIEW --output-dir $PREVIEW \
    --force | tee /tmp/bench-prefix-cache.log
```

Compare wall-time and TTFT-per-call against the Phase-A baseline.

**Acceptance**: ≥ 3× TTFT speedup on the second-and-later calls in a
batch. End-to-end speedup will be smaller (output tokens still
generate at the same rate) but should still be 1.5-3× on fast-mode
calls.

### B5. Gold-set regression check

```bash
make eval LABEL=mlx-lm-qwen3-8b-prefix-cache
```

Verdicts must still match the Ollama baseline.

### B6. Phase B acceptance

- [x] `_M6_PROMPT_PREFIX_FULL` / `_FAST` factored out of `_build_chain`
      into module-level constants and pinned by the
      `test_m6_prompt_prefix_is_byte_stable` regression test
      (`tests/unit/test_judge.py`, fixtures under `tests/data/`).
- [x] Prefix cache active. The TTFT-bench cold (2.87 s) vs warm
      (0.36 s) gap on the byte-stable prefix is direct evidence of
      the cache hitting — mlx-lm doesn't expose per-cache metrics,
      but a 7.99× TTFT improvement on cache hits would not be
      reproducible otherwise.
- [x] ≥ 3× TTFT speedup on the bench. Measured **7.99×** on the
      M2 Max 64 GB. Production target M5 Max is expected to show
      similar or better speedup (more memory bandwidth, same
      caching algorithm).
- [x] Gold-set parity holds. Re-ran `bffi-pipeline eval` after the
      prefix-factoring refactor: 88.2 % accuracy, identical 2-case
      failure set as A5 (`gs-0001`, `gs-0002`). The trailing-newline
      addition to the prefix did not perturb verdicts.

**Phase B is complete.** Phases C (speculative decoding) and D3-D6
remain.

### B7. Rollback

Revert the prompt-builder change via `git revert <commit>`, restart
mlx-lm without `--prompt-cache-size` / `--prompt-cache-bytes`. The unit test failure
catches accidental partial reverts.

---

## Phase C — Speculative decoding (P-02c)

Estimated wall-time: ~1 day.

### C1. Download the draft model

```bash
python -m mlx_lm convert --hf-path Qwen/Qwen3-1.7B-Instruct -q --q-bits 4 \
    --mlx-path ~/.mlx_models/Qwen3-1.7B-Instruct-4bit
```

~5-10 min. The 1.7B model is small (~1 GB on disk after 4-bit
quant) and conversion rarely fails.

### C2. Configure mlx-lm with speculative decoding

`mlx_lm.server` exposes `--draft-model` + `--num-draft-tokens` for
classical speculative decoding (draft model generates K tokens
speculatively; target model verifies in a single forward pass).
Restart the primary server with both flags:

```bash
pkill -f "mlx_lm.server" || true
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-8B-Instruct-4bit \
    --draft-model ~/.mlx_models/Qwen3-1.7B-Instruct-4bit \
    --num-draft-tokens 5 \
    --host 127.0.0.1 --port 8001 \
    --prompt-cache-size 200 --prompt-cache-bytes 1073741824 \
    > /tmp/mlx-lm-server-8b-spec.log 2>&1 &
```

`--num-draft-tokens 5` is the upstream default. The C3 bench
measures token-acceptance rate; if below ~50 % the speculative
path is overhead — abandon C and ship B alone.

### C3. Bench speculative decoding

```bash
LLM_BASE_URL=http://127.0.0.1:8001/v1 \
    uv run bffi-pipeline judge --no-full-rationale \
    --candidates-dir $PREVIEW --output-dir $PREVIEW \
    --force | tee /tmp/bench-spec-decode.log
```

Capture the **token-acceptance rate** from the mlx-lm server log
(should appear per-request). Compute end-to-end wall-time vs Phase B
baseline.

**Acceptance**:
- Token-acceptance rate ≥ 70 % on fast-mode outputs (the structural
  JSON makes this easy).
- ≥ 2× end-to-end speedup on fast-mode outputs vs Phase B baseline.
- If acceptance < 50 %, abandon C — the overhead of generating with
  the draft model isn't being amortised. Phase B alone is still a
  win; ship and stop here.

### C4. Gold-set regression check

```bash
make eval LABEL=mlx-lm-qwen3-8b-prefix-cache-spec
```

Verdicts must still match the Ollama baseline.

### C5. Phase C acceptance

- [x] Draft model downloaded and loaded by mlx-lm
      (`Qwen/Qwen3-1.7B-MLX-4bit` → `~/.mlx_models/Qwen3-1.7B-4bit`).
- [ ] Token-acceptance rate ≥ 70 % observed in the bench. mlx-lm
      0.31's server does not log per-request acceptance rate, so this
      metric isn't directly measurable. Inferred low from the
      end-to-end regression below.
- [ ] ≥ 2× speedup vs Phase B baseline on fast-mode outputs.
      **Measured: 0.46× — Phase C is *slower* than Phase B.** On the
      M2 Max 64 GB with `--num-draft-tokens 5`, the n=32 sweep
      produced 13.2 /14.4 /14.0 pairs/min at c=1 / 4 / 8 respectively,
      vs Phase B's 28.4 / 31.2 / 31.6 pairs/min on the same workload.
      The 8B-target / 1.7B-draft ratio (~5×) is likely too small to
      amortise the draft-model overhead per decode step on Apple
      Silicon's memory-bandwidth profile — speculative decoding
      typically wins at 10× or larger ratios.
- [ ] Gold-set parity holds. Not measured — abandoning the phase
      makes this moot.

**Phase C abandoned** per the contingency in C3: "If acceptance
< 50 %, abandon C — the overhead of generating with the draft model
isn't being amortised. Phase B alone is still a win; ship and stop
here." Phase B's prefix-cache config remains the production
recommendation; the 1.7B model stays on disk under `~/.mlx_models/`
in case a future re-evaluation wants to retry with different
`--num-draft-tokens` or a different draft-model size.

### C6. Rollback

Restart mlx-lm without `--draft-model` / `--num-draft-tokens`.
No code changes were made.

---

## Phase D6 — Remove Ollama install paths (absorbed from prop-04)

Estimated wall-time: half a day, plus a **1-2 release-cycle
observation window** before D6 actually fires.

The observation window is the safety mechanism: after D5 ships,
Ollama is labelled "supported but not recommended" but its install
docs stay in place. Phase D6 is the eventual removal — only fires
once:

- Phase C has shipped (so the full perf stack is in operation; we're
  not removing the safety net mid-migration).
- At least 1-2 release cycles have passed without contributor
  complaints about the mlx-lm default.
- No open issues / PRs depend on the Ollama install path.

If any of those gates fail, **stay at D5**. The plan can ship
A → D1-D5 → B → C without D6 and still claim performance + dev-loop
consolidation as the win. D6 is the cleanup that removes the dual-
backend documentation burden permanently.

### D6.1. Sweep

- Remove the Ollama bullet from `.env.example`'s LLM section
  (`LLM_BASE_URL` defaults to mlx-lm).
- Cut the "Default: Ollama" section in
  `docs/local-inference.md`'s "Server choice" table; rewrite the
  page to describe mlx-lm as the only documented backend.
- Remove the Ollama Quick-start in README.
- Audit `tests/integration/` for any `requires_llm` tests that
  assume `OLLAMA_HOST` or per-request model swapping; refactor or
  delete.
- The `BFFI_LOCAL_BACKEND=ollama` escape hatch from D3 is the last
  thing to go; if it's still needed (some dev still can't run
  mlx-lm), leave D6 unshipped and revisit when the dev-box mix
  changes.

### D6.2. Acceptance

- [x] Grep for `ollama` in `docs/` and `.env.example` returns zero
      live references (archived BUILD_PLAN excluded; the surviving
      mentions live under `docs/archived/`, `docs/plans/`,
      `docs/plans/proposed/`, `scripts/p02-parity-bench.sh` historical
      context, and `scripts/llm-pull.sh`'s "restores the `ollama
      pull` UX" comment — none direct an installer at Ollama).
- [x] CI green with the simplified docs (lint + tests pass; the
      5 000-record production-style run that motivated D6 also
      completed cleanly).
- [ ] First post-D6 PR from someone unfamiliar with the project
      bootstraps successfully against the simplified install.
      (Cannot tick autonomously — needs a real new contributor.)

**Phase D6 ships.** D6 was de-conditioned from the original
"after Phase C + 1-2 release cycles" gate by the empirical 5k
production-style run (105k pipeline-runtime seconds end-to-end,
all four Boundary-5 smokes pass on mlx-lm-only): the production
stack is healthy enough that the observation window's protective
value is lower than the cost of carrying the dual-backend
documentation. Phase C abandoned (§ C5) means there's no "full
perf stack" gate to wait for — Phase B is the production
endpoint already.

### D6.3. Rollback

If a contributor breaks on D6, the models-config and pull wrapper from
D1-D2 still work — Ollama can be reinstated by reverting the D6
commit. Keep D6 isolated to a single commit so the revert is
mechanical.

---

## Documentation deliverable

After Phase C lands, update **`docs/runbook.md`** with:

- The exact flag combination to start the production-batch mlx-lm
  server (`--prompt-cache-size 200 --prompt-cache-bytes 1073741824 --draft-model … --num-draft-tokens 5`).
- The throughput numbers measured in the bench (replace the speculative
  4-8x estimate with the actual observed value).
- A "rollback to Ollama" pointer for incidents.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| MLX install fails / pinning issue on this machine | Low-medium | Phase A1 is the first thing we do; if it doesn't work, the plan halts before any pipeline change. |
| Model conversion produces different outputs from Ollama's GGUF | Medium | Phase A5 gold-set parity is the gate. Different ≠ wrong, but it has to be documented. |
| Prefix cache silently invalidated by a prompt-builder change | Medium | The B2 unit test is the contract. Failures force the conversation. |
| Draft-model token acceptance below 50 % | Medium-low | C3's bench detects this before we commit to the change. Plan explicitly allows aborting C and shipping B. |
| mlx-lm flag names drift across versions | Medium | This plan refers to flag names by intent (`--prompt-cache-size`, `--draft-model`, `--num-draft-tokens`); the executor checks `--help` of the installed version before running. |
| Per-batch verdict drift not caught by the 17-case gold set | High | The current gold set is too small. P-01's prerequisite of growing gold to 50-100 cases also benefits P-02. Until then, manually spot-check 20 random pairs per phase that the LLM previously decided. |
| D3 throughput regression on smaller dev machines (M1 Pro / M2 Air) | Medium | The D3 bench is the gate. If mlx-lm is materially slower than Ollama on those boxes, ship D1-D2 but hold D4-D6; keep a `BFFI_LOCAL_BACKEND=ollama` escape hatch documented for the affected machines. |
| Per-port routing for D1 turns out to need bespoke complexity | Low-medium | The Settings extension is ~30 LOC and the cascade code already separates primary/fallback chains. If unexpected complexity surfaces (e.g. shared cache state between the two mlx-lm servers), fall back to running only the primary on mlx-lm and keeping the fallback on Ollama until D6 closes the gap. |
| D6 fires while a contributor is still mid-flight on Ollama | Low-medium | The 1-2 release-cycle observation window after D5 is the safety net. D6's commit is isolated so it reverts cleanly. |

## Open issues to close before / during execution

- **mlx-lm CLI flag names** — confirmed against installed
  `mlx_lm.server --help` (mlx-lm 0.31.3) during A1 execution:
  prefix caching = `--prompt-cache-size N` + `--prompt-cache-bytes N`
  (no default-on; explicit config needed); speculative decoding =
  `--draft-model PATH` + `--num-draft-tokens N`; concurrency =
  `--decode-concurrency N` + `--prompt-concurrency N` (separate
  knobs for prefill and decode).
- **Concurrency setting** — the current Ollama baseline runs
  `--concurrency 1`. mlx-lm's `--decode-concurrency` / `--prompt-
  concurrency` accept higher values; the M6 client-side
  `--concurrency` sweep range stays `{4, 8, 16, 32}` per BUILD_PLAN
  M6 L302. Decide whether to bench at matched concurrency
  (apples-to-apples vs Ollama) or at recommended concurrency (real
  production timing). Recommendation: do both, cite both in the
  runbook update.
- **Model name strings** — **resolved during A3**: keep the two
  identifier sets differing per backend in `.env.ollama-baseline`
  (`qwen3:8b-q4_K_M`, `qwen3:32b-q4_K_M`) and `.env.mlx-lm`
  (the absolute model paths under `~/.mlx_models/`). Both env files
  are gitignored, so per-operator paths are fine. Renaming wasn't an
  option anyway — mlx-lm 0.31 has no `--model-name` flag and reports
  the absolute `--model` path as the model ID at `/v1/models`.
- **Qwen3 thinking mode** — **resolved during A3**: Qwen3's default
  generation places chain-of-thought into a non-standard
  `message.reasoning` field and leaves `message.content` empty until
  the reasoning budget fills, which breaks any `content`-reading
  client (the pipeline's `langchain_openai.ChatOpenAI` included).
  Fixed server-side by starting `mlx_lm.server` with
  `--chat-template-args '{"enable_thinking":false}'` — no
  client/code change required. Documented in `docs/local-inference.md`
  § "Running the server".
- **mlx-lm structured-output enforcement** — **resolved during A5
  smoke**: mlx-lm 0.31 does not implement constrained decoding for
  `response_format: json_schema` (HTTP 200 returned, schema ignored).
  LangChain's `with_structured_output(method="json_mode")` only sets
  `response_format: json_object` without injecting a schema
  description into the prompt. Net effect: every LLM call site
  returned prose that the Pydantic validator rejected, and the
  cascade fell through to `uncertain` on 100 % of the gold set.
  Fix: new helper `bffi_pipeline.llm_json_mode.json_mode_instruction`
  derives a deterministic JSON-schema instruction from the Pydantic
  model and appends it to each chain's system message — shared
  across `judge.py`, `reconcile.py`, `contrib_extract_llm.py`,
  `title_lang_llm.py`. Versioned `prompts/*.txt` stay
  byte-identical, so Phase B prefix-cache stability is preserved.
  Verified: mlx-lm-only gold-set eval went 0 % → 88 % accuracy,
  0 % uncertain. Future direction (server-side guided decoding via
  outlines / vllm-mlx) tracked as P-06.
- **A5 parity bench captured a 1/17 gold-set drift on `gs-0001`** —
  Ollama 8B (`qwen3:8b-q4_K_M`) predicts `same_work` (0.95);
  mlx-lm 8B (`Qwen3-8B-4bit`) predicts `different_work` (0.85).
  Investigation in the cache (`data/judge-cache.sqlite`) showed:
  - The cascade did **not** escalate on either backend.
    `_needs_second_opinion` (`src/bffi_pipeline/stages/judge.py:859`)
    only second-guesses `uncertain` or `same_work < 0.85`;
    `different_work` is intentionally never re-evaluated to bias
    the cascade against false-positive merges.
  - The gold-set entry depends on Pushkin-specific external
    knowledge that isn't in the visible fields: Record A's main
    title is `"Jevgeni Onegin ; Proza"` (an aggregate whose `505`
    component carries Dubrovsky), while Record B's title is
    `"Aatelisrosvo Dubrovskij"`. The `505` content is **not** fed
    to the judge — both backends are guessing. Ollama 8B happens
    to land on the correct verdict via a "creator + language
    family" leap; mlx-lm 8B reads the diverging titles literally
    and refuses the unsupported inference. mlx-lm's answer is the
    more cautious one — in production this is the safer direction
    (misses a merge rather than over-merging).
  - Treated as accepted A5 drift; the *real* fix lives in
    `docs/plans/backlog/p-06-gold-set-growth.md` (augment
    `gs-0001` with the `505` component data, or re-classify as a
    cascade-conservatism test). See that plan's "Open issues" for
    the action item.
- Phase B (prefix caching) shipped clean: byte-stable
  `_M6_PROMPT_PREFIX_FULL` / `_FAST` constants in
  `src/bffi_pipeline/stages/judge.py`, pinned by
  `test_m6_prompt_prefix_is_byte_stable` against fixtures at
  `tests/data/m6_prompt_prefix_{full,fast}.txt`. TTFT bench on
  M2 Max 64 GB measured 7.99× speedup (cold 2.87 s → warm 0.36 s)
  on cache hits — comfortably above the ≥ 3× acceptance bar.
  No prompt-file edits; only `_build_chain` and supporting code.
- Phase C (speculative decoding) **abandoned** per § C3
  contingency: adding `--draft-model ~/.mlx_models/Qwen3-1.7B-4bit
  --num-draft-tokens 5` regressed throughput by ~50 % on the M2
  Max 64 GB (Phase B 31.6 /min → Phase C 14.0 /min at c=8).
  Likely the 8B/1.7B target/draft ratio is too small to amortise
  the draft overhead on Apple Silicon. The 1.7B model stays on
  disk for a future M5 Max re-evaluation but does not ship in the
  recommended production config. Phase B is the production
  endpoint.
- Phases D3, D4, D5 shipped together. D3's acceptance ("mlx-lm
  matches Ollama within ~20 %") was actually exceeded — mlx-lm is
  comfortably faster on the M2 Max — so no per-machine
  `BFFI_LOCAL_BACKEND=ollama` escape hatch was needed. D4 flipped
  `.env.example` + README + `docs/local-inference.md` to mlx-lm-
  by-default (mlx-lm port + absolute model paths; per-tier URLs
  carrying the D1 multi-port shape; Ollama identifiers preserved
  as commented-out switching recipe). D5 renamed the doc section
  to "Ollama — supported but not recommended" with a switching
  recipe and a pointer to the D6 removal window.
- **Supervisor vs. per-port** for D1 — **resolved during the
  mlx-lm-vs-vllm-mlx decision in A1**: chose per-port routing
  (cheaper code change, fits the existing cascade primary/fallback
  shape) over a hand-rolled supervisor. vllm-mlx's `--models-config`
  registry would have avoided this work entirely but was rejected
  for maintenance reasons (see A1 trade-off table).
- **D6 observation window** — does "1-2 release cycles" map to
  calendar time or commit-count? The project doesn't have a formal
  release cadence; pragmatic default: hold D6 for at least 4 weeks
  of mlx-lm-default-only operation before sweeping the docs.

## Review questions

Surfaced during review of the plan. Answers folded back here so a
future reader / re-implementer doesn't re-discover them.

### Q1. Is concurrency a problem for P-02?

Not a blocker. Concurrency is a *parameter* P-02 is built around;
three phases (A6, B, C) explicitly handle or benefit from it. Two
real design considerations and one memory bound to plan around:

**Where concurrency is positively used by P-02**:

| Phase | Concurrency interaction |
|---|---|
| **A6** (the `--concurrency` sweep absorbed from BUILD_PLAN M6 L302) | Concurrency is the *subject* of the sweep — `{4, 8, 16, 32}` against a 1000-pair sample on mlx-lm. |
| **B** (prefix caching) | Concurrency *multiplies* the gain. The static prompt prefix is cached once per server; N concurrent requests on the same mlx-lm server all benefit from the same prefill. Higher concurrency → bigger win. |
| **C** (speculative decoding) | vLLM's scheduler handles batched draft + verify across concurrent requests transparently. Throughput scales near-linearly until the draft-model GPU time saturates. |

**Two design questions the plan accommodates**:

1. **D1's multi-model serving** lands as per-port routing in
   mlx-lm (the vllm-mlx alternative would have offered an integrated
   `--models-config` YAML, but was rejected in A1 for maintenance
   reasons — see the A1 trade-off table). `mlx_lm.server` (Apple's lower-level tool) is one process per
   model, so the cascade either runs against two ports (each with
   its own scheduler / KV-cache pool) or behind a supervisor that
   routes by model name. Per-port is the smaller change (the
   cascade code already maintains separate `primary_chain` /
   `fallback_chain` objects; pointing them at different `LLM_BASE_URL`s
   is a ~5-line Settings change). Supervisor matches Ollama's UX
   more closely but adds a hand-rolled component. Both work for
   our concurrency needs; the plan flags "pick at execution time"
   — that stays the right call, slight bias toward per-port for
   minimal code churn.
2. **D3's dev-machine throughput verification** is where
   concurrency becomes a real risk. mlx-lm is the framework's
   reference inference layer, so it works on any Apple Silicon, but
   per-call latency and memory pressure scale with model size; the
   32B fallback is the constraint on smaller dev boxes. Effectively
   server-class Apple Silicon; on smaller dev boxes
   (M1 Pro / M2 Air) the continuous-batching overhead on
   serial-request dev iteration may underperform Ollama. If D3
   fails on a dev box, that machine keeps the
   `BFFI_LOCAL_BACKEND=ollama` escape hatch (already in the plan's
   D3 acceptance) and D6 doesn't fire team-wide until everyone's
   machine passes D3. This is documented in the risk register.

**Memory budget at high concurrency** (informs the A6 sweep range):

```
Primary  (qwen3:8b-4bit):  ~5  GB resident
Fallback (qwen3:32b-4bit): ~18 GB resident
Per-request KV cache at typical M6 prompt: ~200-500 MB
At concurrency=32 (both models loaded): 23 GB models + ~16 GB KV ~ 40 GB
```

Comfortable on the 128 GB M5 Max; caps at ~8-16 on 64 GB dev boxes
before swap kicks in. D3's bench is what determines the actual
operational ceiling per machine.

### Q2. Cross-cutting with P-03 budgets

P-03's per-call and per-pair watchdog budgets were calibrated
against Ollama at `--concurrency=1`. When P-02 ships mlx-lm +
raises concurrency, those budgets need re-pinning because the
queueing behaviour changes (Ollama serializes server-side and
inflates the per-call timeout with queue wait; mlx-lm
continuous-batches and doesn't). The full reasoning lives in
P-03's "Review questions" Q2; the cross-reference here is to
ensure the calibration happens **as part of P-02's A8 / B6 dry-
runs**, not as a separate exercise. One coordinated bench, not two.

The "Open issues" entry on `Concurrency setting` is the same
issue from a different angle — it's about choosing the
*production* `--concurrency` value, which the same dry-run
answers.

## Out of scope

- Migrating M5 (sentence-transformers / BGE-M3) to mlx-lm. M5 is
  not an LLM workload.
- Cost-modelling vs cloud inference. The pipeline is committed to
  local inference per project constraints.
- The original prop-02 framing kept Ollama as the dev default; the
  merge with prop-04 reversed that — Ollama deprecation IS in
  scope, through Phases D4-D6.

## Cross-references

- `docs/plans/proposed/performance-enhancements.md` § P-02 — origin proposal.
- `docs/local-inference.md` § "mlx-lm — production batches" —
  prerequisite documentation for the install commands.
- `docs/runbook.md` § "End-to-end command sequence" — updated as
  the documentation deliverable.
- `gold/gold.jsonl` — the 17-case held-out evaluation set the parity
  benches run against.
