# P-04 — M5 calibration: embedding-model bench + efSearch tuning

**Status**: draft.
**Source**: `docs/archived/BUILD_PLAN.md` M5 unfinished items (lines 274 + 283 at
plan-base). Not graduated from a proposal — these are milestone
follow-ups that need their own execution document because they
require a one-time benchmark on the M5 Max that the rest of the
pipeline depends on for committed defaults.
**Plan-base commit**: `fe0b8dd`. To gauge drift before executing,
run `git diff fe0b8dd..HEAD -- src/bffi_pipeline/stages/embeddings.py
src/bffi_pipeline/eval/embed_benchmark.py`.
**Phase commits**:

- Phase A (embedding-model benchmark): `<unfilled>`
- Phase B (`efSearch` sweep): `<unfilled>`

**Owner**: TBD (M5 Max-side benchmark — operator only).
**Estimated wall-time**: half a day for Phase A; another half a day
for Phase B (Phase B depends on a built FAISS index over the
production corpus, so it slots into the v2 pipeline's post-M5
window).

## Goal

Lock in the two M5 hyperparameters that the spec currently carries
as placeholder defaults:

1. **Embedding model**: confirm `BAAI/bge-m3` (the current default)
   beats `intfloat/multilingual-e5-large` and `jinaai/jina-embeddings-v3`
   on the gold set's same_work / different_work cosine-similarity
   gap. If a different model wins, switch the default and update
   any 1024-dim FAISS assumptions.
2. **`efSearch`**: pick the smallest value in `{32, 64, 128, 256}`
   that finds all known same_work pairs from the gold set's
   high-similarity band, measured against the *production-corpus*
   FAISS index (not a synthetic small index).

## Definition of done

- A docstring at the top of `src/bffi_pipeline/stages/embeddings.py`
  records the chosen model, the mean similarity gap on the gold
  set, and the chosen `efSearch` with the recall numbers per
  sweep value.
- If the winning model is not BGE-M3, `.env.example` is updated and
  any vector-dimension assumptions in the embed code are re-verified.
- Both numbers are reproducible from the harnesses that already
  exist (`bffi-pipeline embed-benchmark` and a small `efSearch`
  sweep driver added in Phase B).

## Current state

- `src/bffi_pipeline/eval/embed_benchmark.py` is committed and runnable
  via `bffi-pipeline embed-benchmark`; it walks gold pairs and
  reports per-model same_work vs different_work mean similarity.
- `src/bffi_pipeline/stages/embeddings.py` defaults to BGE-M3, 1024
  dimensions, HNSW `M=32 efConstruction=200 efSearch=64`.
- The gold set is currently 17 cases (`gold/gold.jsonl`) — small,
  but enough to differentiate models on the same_work / different_work
  gap. The `efSearch` sweep needs the v2 corpus FAISS index to be
  meaningful at scale.
- ML deps (`sentence-transformers`, `faiss-cpu`) already pinned in
  `pyproject.toml`.

---

## Phase A — Embedding-model benchmark

Estimated wall-time: half a day, dominated by the first model
download (~2.3 GB for BGE-M3; e5-large + jina-v3 are similar).

### A1. Run the harness

```bash
# From the repo root, on the M5 Max with internet on.
uv run bffi-pipeline embed-benchmark \
    --output-path eval-runs/embed-bench-2026-MM-DD.json
```

The harness logs one row per candidate model:

```
model                                  same_mean   diff_mean   gap
BAAI/bge-m3                            0.xx        0.yy        Δ1
intfloat/multilingual-e5-large         0.xx        0.yy        Δ2
jinaai/jina-embeddings-v3              0.xx        0.yy        Δ3
```

**Verification**: every gold-set case shows a non-zero similarity
under every candidate model; no NaN / negative rows. If a model
fails to load (e.g. version pin mismatch), the harness prints the
exception and skips that model — investigate before deciding.

### A2. Pick the winner

Winner = the model with the widest gap. Tie-breaker: prefer
multilingual coverage that matches Helmet's corpus (fi/sv/en/ru), so
prefer BGE-M3 or multilingual-e5-large over jina-v3 (English-leaning)
unless the gap difference is > 0.05.

### A3. Document and (if needed) switch the default

If the winner is BGE-M3:

- Edit `src/bffi_pipeline/stages/embeddings.py`'s module docstring
  to add the bench result table + the date + the gold-set size.
  No code change needed.

If the winner is not BGE-M3:

- Update `.env.example` so the default model env var is the winner.
- Verify the winner's vector dimension matches BGE-M3's 1024
  assumed by the HNSW config. If different, update the dim
  constant and re-justify the HNSW `M` / `efConstruction` values
  (they're tuned for 1024-dim space).
- Add an entry to `docs/runbook.md` § "Pinned versions" updating
  the embedding-model row.

### A4. Phase A acceptance

- [ ] All three candidate models ran cleanly on the gold set.
- [ ] Winner documented in `embeddings.py` docstring with the
      observed gap.
- [ ] If non-BGE-M3 winner: `.env.example`, HNSW config, and runbook
      updated; CI is green after the change.

### A5. Rollback

Revert the docstring (and the `.env.example` + config edits if any
were made) via `git revert <commit>`. No data-format changes; no
re-conversion required.

---

## Phase B — `efSearch` sweep against the production FAISS index

Estimated wall-time: half a day, gated on the v2 pipeline reaching
`STAGE_M5_DONE` (FAISS index built over the production corpus).

### B1. Run the sweep

Write a small driver (~30 LOC) that loads the production FAISS
index from `<BFFI_DATA_DIR>/embeddings.faiss`, iterates the gold
set's same_work cases, and queries each side for its 20 nearest
neighbors at `efSearch ∈ {32, 64, 128, 256}`. For each `efSearch`,
report:

- Recall@20 on gold same_work pairs (did the gold pair appear in
  the candidate set at all?).
- Mean per-query latency in ms.

Place the driver at `src/bffi_pipeline/eval/efsearch_sweep.py`,
expose it as `bffi-pipeline efsearch-sweep --corpus-dir <dir>`.
Tests: monkeypatch FAISS index to a small in-memory one, assert
the sweep reports correct recall counts.

### B2. Pick the chosen value

The smallest `efSearch` that achieves recall@20 = 1.0 on the
high-similarity (≥ 0.78) gold cases is the chosen value. If even
`efSearch = 256` doesn't get full recall, the gold set has a pair
the FAISS index genuinely doesn't surface — that's a separate
issue (likely a blocking-key disagreement), not a tuning problem.
Document the failing pair and proceed with `efSearch = 256` as
the chosen value.

### B3. Update the default

Edit `src/bffi_pipeline/stages/embeddings.py`'s HNSW config constant
to the chosen `efSearch`. Update the module docstring with the
sweep table:

```
efSearch    recall@20    median-ms
32          0.xx         x.x
64          0.xx         x.x
128         1.00         x.x  ← chosen
256         1.00         x.x
```

### B4. Phase B acceptance

- [ ] Sweep driver exists, has unit tests, is reachable via
      `bffi-pipeline efsearch-sweep`.
- [ ] `embeddings.py` docstring records the sweep table.
- [ ] HNSW config constant updated to the chosen value.
- [ ] Gold-set eval (`make eval`) shows no regression vs the
      pre-change baseline.

### B5. Rollback

Revert the constant change; the FAISS index itself doesn't need
rebuilding (only query-time `efSearch` changed).

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Gold set is too small (17 cases) to differentiate models meaningfully | Medium | Phase A reports the per-case raw cosines, not just the mean — manual inspection of any tied-mean cases. If models are functionally indistinguishable, stick with BGE-M3 (status quo, lower-risk option). |
| `efSearch` sweep finds no value with full recall | Low-medium | The gold pair causing the failure gets documented; the M4 blocking-key system may need adjustment. That's a separate plan, not a P-04 blocker. |
| Switching models invalidates downstream embedding caches | Medium | If the dimension changes, the FAISS index needs a rebuild — costly on 800 k records (~30-60 min on the M5 Max). Plan for this in the same window as Phase B if Phase A's winner needs a rebuild. |

## Open issues to close before / during execution

- Should the embedding model be benchmarked with the same gold
  cases the LLM judge is evaluated against, or with a *larger*
  unannotated sample? Spec § 9 says gold; but a 17-case sample
  is statistically noisy. Recommendation: stick with gold for
  reproducibility, document the noise floor, revisit when the
  gold set crosses 50 cases (see P-06).
- Is the `efSearch` recall metric appropriate, or should we
  instead measure end-to-end M5-band assignment stability (i.e.,
  does the same pair land in the same band at different
  `efSearch` values)? Stability is the operational property we
  actually care about. Decide at Phase B kickoff.

## Cross-references

- `docs/archived/BUILD_PLAN.md` M5 — origin checklist items.
- `docs/runbook.md` § "Pinned versions" — receives the embedding-
  model update if the winner is not BGE-M3.
- `src/bffi_pipeline/eval/embed_benchmark.py` — Phase A harness.
- P-06 — gold-set growth that improves Phase A's statistical power.
