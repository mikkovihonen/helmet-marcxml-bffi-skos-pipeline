# P-02 — Inference-stack tuning for the M6 cascade

**Status**: in-progress.
**Source proposals**:
[`docs/proposals/prop-02-inference-stack-tuning-for-M6.md`](../../proposals/prop-02-inference-stack-tuning-for-M6.md)
(introduced in commit `334294a` while still part of the combined
`performance-enhancements.md`; k-NN critique that fed back into P-01
landed in `9789c20`; per-proposal file split in the commit that also
introduced the initial plan) and
[`docs/proposals/prop-04-consolidate-on-mlx-lm.md`](../../proposals/prop-04-consolidate-on-mlx-lm.md)
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

**Phase commits** (filled in as phases ship; empty fields here are a
signal that the phase has not yet completed against the gold-set
acceptance criteria):

- Phase A (mlx-lm bring-up + parity): `<rollup unfilled>`
  - A1 (mlx-lm 0.31.3 installed in `~/.venvs/mlx-lm`): operator-side
    step, no commit; verified by `python -c "import mlx_lm;
    print(mlx_lm.__version__)"`.
  - A4 (`.env.ollama-baseline` + `.env.mlx-lm` written): operator-side
    step, both files local + gitignored. The committed plan + doc
    sweep that documented the rename and the upstream
    `python -m mlx_lm.<sub>` → `python -m mlx_lm <sub>` migration
    accompanies A1/A4: `7cde2bf`.
  - A2 (Qwen3-8B-Instruct-4bit + Qwen3-32B-Instruct-4bit converted): `<unfilled>`
  - A3 (`mlx_lm server` running on :8001 + :8002): `<unfilled>`
  - A5 (`scripts/p02-parity-bench.sh` parity verdict captured): `<unfilled>`
  - A6 (concurrency sweep run; chosen value recorded): `<unfilled>`
  - A7 (acceptance gate passed): `<unfilled>`
- Phase D1-D5 (dev-loop consolidation on mlx-lm): `<rollup unfilled>`
  - D1 (per-port routing in Settings + cascade): `f3c0bea`
  - D2 (model-pull wrapper): `d959bf6`
  - D3 (dev-machine throughput verification): `<unfilled>`
  - D4 (flip committed defaults): `<unfilled>`
  - D5 (label Ollama secondary): `<unfilled>`
- Phase B (prefix caching): `<unfilled>`
- Phase C (speculative decoding): `<unfilled>`
- Phase D6 (remove Ollama install paths): `<unfilled>`

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

### A2. Convert the two judge models to MLX 4-bit

The easiest path is to pull a pre-quantised checkpoint from the
`mlx-community` HF org if one exists; otherwise convert from the
raw HF weights via `mlx_lm.convert`.

```bash
mkdir -p ~/.mlx_models
# Primary (8B; ~5 GB on disk after 4-bit quant): ~30-45 min on M5 Max
python -m mlx_lm convert --hf-path Qwen/Qwen3-8B-Instruct -q --q-bits 4 \
    --mlx-path ~/.mlx_models/Qwen3-8B-Instruct-4bit
# Fallback (32B; ~18 GB on disk after 4-bit quant): ~1-2 h
python -m mlx_lm convert --hf-path Qwen/Qwen3-32B-Instruct -q --q-bits 4 \
    --mlx-path ~/.mlx_models/Qwen3-32B-Instruct-4bit
```

(Alternative: clone pre-quantised checkpoints from
`mlx-community/Qwen3-8B-Instruct-4bit` etc. directly via `huggingface-cli
download`; faster if the upstream re-quantisation matches what we'd
have produced locally.)

**Verification**: `ls -la ~/.mlx_models/Qwen3-8B-Instruct-4bit/` shows
the expected `weights.npz` (or sharded variant), `config.json`, and
tokenizer files. Total size on disk ≈ 5 GB for 8B, ≈ 18 GB for 32B.

**Fallback if HF download is unreliable**: pull the GGUF blobs Ollama
already has at `~/.ollama/models/blobs/`, identify them by manifest,
and convert via `mlx_lm.convert --hf-path` against the GGUF path.
Documented but flakier — only use if HF download repeatedly fails.

### A3. Start the mlx-lm server on a dedicated port

```bash
# In a separate terminal (or via nohup); keep Ollama running on :11434.
python -m mlx_lm server \
    --model ~/.mlx_models/Qwen3-8B-Instruct-4bit \
    --host 127.0.0.1 --port 8001 \
    > /tmp/mlx-lm-server-8b.log 2>&1 &
# Probe:
curl -s http://127.0.0.1:8001/v1/models | jq
```

**Verification**: the `/v1/models` response lists exactly the
loaded model. The pipeline's `LLM_MODEL_PRIMARY` env value must
match what mlx-lm reports — likely the model-path basename like
`Qwen3-8B-Instruct-4bit`, not `qwen3:8b-q4_K_M`. Update
`.env.mlx-lm` (see A4) accordingly.

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
#    explicitly so the cascade routes primary/fallback correctly:
#      LLM_BASE_URL=http://127.0.0.1:8001/v1
#      LLM_BASE_URL_PRIMARY=http://127.0.0.1:8001/v1
#      LLM_BASE_URL_FALLBACK=http://127.0.0.1:8002/v1
#      LLM_API_KEY=mlx-lm
#      LLM_MODEL_PRIMARY=Qwen3-8B-Instruct-4bit
#      LLM_MODEL_FALLBACK=Qwen3-32B-Instruct-4bit
#    Model names must match what `mlx_lm server` reports at /v1/models
#    (basename of the path you converted into during A2).

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

- [ ] mlx-lm server starts cleanly on port 8001.
- [ ] `make eval` against mlx-lm matches Ollama on all 17 gold
      cases (or only differs on documented numerical-noise pairs).
- [ ] `.env.ollama-baseline` exists for instant rollback.
- [ ] `--concurrency` sweep complete; chosen value documented in
      runbook and committed as the new `M6_CONCURRENCY` default.

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

- [ ] `_M6_PROMPT_PREFIX` factored out and pinned by unit test.
- [ ] Prefix cache active (via TTFT delta on the B4 bench; mlx-lm doesn't expose per-cache metrics).
- [ ] ≥ 3× TTFT speedup on the bench.
- [ ] Gold-set parity holds.

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

- [ ] Draft model converted and loaded by mlx-lm.
- [ ] Token-acceptance rate ≥ 70 % observed in the bench.
- [ ] ≥ 2× speedup vs Phase B baseline on fast-mode outputs.
- [ ] Gold-set parity holds.

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

- [ ] Grep for `ollama` in `docs/` and `.env.example` returns zero
      live references (archived BUILD_PLAN excluded).
- [ ] CI green with the simplified docs.
- [ ] First post-D6 PR from someone unfamiliar with the project
      bootstraps successfully against the simplified install.

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
- **Model name strings** — `LLM_MODEL_PRIMARY` and `LLM_MODEL_FALLBACK`
  must match what mlx-lm reports via `/v1/models`. Decide whether to
  rename Ollama's identifiers (`qwen3:8b-q4_K_M`) to match mlx-lm
  (`Qwen3-8B-Instruct-4bit` per the model-path basename), or vice
  versa, or keep them differing per backend in
  `.env.ollama-baseline` / `.env.mlx-lm`.
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

- `docs/proposals/performance-enhancements.md` § P-02 — origin proposal.
- `docs/local-inference.md` § "mlx-lm — production batches" —
  prerequisite documentation for the install commands.
- `docs/runbook.md` § "End-to-end command sequence" — updated as
  the documentation deliverable.
- `gold/gold.jsonl` — the 17-case held-out evaluation set the parity
  benches run against.
