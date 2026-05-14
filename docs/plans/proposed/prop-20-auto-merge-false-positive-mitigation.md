# P-20 — Reduce M5 auto-merge false positives on series-of-books-by-same-author

**Status**: proposed.
**Scope**: 1-3 days depending on which mitigation paths ship; can be split.
**Proposal-base commit**: `f630b83`. To gauge drift before acting, run
`git diff f630b83..HEAD --
src/bffi_pipeline/stages/embeddings.py
src/bffi_pipeline/stages/merge.py
sparql/`.

## Motivation

The 2026-05-13 20 k overnight bench surfaced a clean false-positive merge that the operator caught on inspection:

- **b1499110x** — "Alvar Aalto : **mestariteoksia**" — by Göran Schildt, Otava, 1998 (Finnish translation)
- **b18086238** — "Alvar Aalto : **his life**" — by Göran Schildt, Alvar Aalto Museum, 2007 (English version)

Two **distinct books** in the multi-volume Schildt-on-Aalto bibliography, merged by the pipeline as one canonical Work. M5's auto-merge band (embedding similarity ≥ 0.90) caught them at 0.9061, skipped the M6 LLM judge entirely (auto-merge band is designed to bypass M6), and M8 union-find collapsed them.

```json
{
  "decision": "same_work",
  "similarity": 0.9061,
  "block_a": "schildt|alvar|txt",
  "block_b": "schildt|alvar|txt",
  "rationale": "M5 auto-merge band: embedding similarity 0.906 ≥ 0.90
                (spec § 6 ceiling). Same blocking key
                (schildt|alvar|txt); LLM judge skipped — same_work
                signal is unambiguous at this similarity."
}
```

The rationale is wrong — the similarity is *not* unambiguous at 0.906 when the records are book-series volumes. The class of false positive is **same-author × same-subject × series structure × subtitle-only differentiation**. Schildt's Alvar Aalto biographies are the prototype; Helmet's catalog will have many more (artist monographs, "Selected works of X", reference volumes, etc.).

### Why the embeddings collide

The current `embedding_input_string` (per `src/bffi_pipeline/stages/embeddings.py`) packs five fields, pipe-separated:

```
creator: Schildt, Göran | title: Alvar Aalto | language: fin/eng | year: 1998/2007 | type: txt
```

The `title` field comes from `skos:prefLabel`, which M3 sets from `bf:mainTitle` — **the 245$a part only, without 245$b**. So both records' title strings are literally `"Alvar Aalto"`. Of the 5 fields, only `language` and `year` differ between the two records, and neither dominates the BGE-M3 vector enough to drop similarity below the 0.90 ceiling. The structural blindness is at the field-shape level, not the model.

### Scale of the problem

A back-of-envelope estimate: the cataloguer-tagged 1XX `(FI-ASTERI-N)` set in the bench is 13 % of the corpus; among those, multi-volume series + translations are a meaningful subset. On the 800 k full corpus, this could be **hundreds to low thousands of similar false merges** flowing through M5's auto-merge gate without LLM verification.

## Approach — four candidates

Each addresses a different layer of the failure mode; they compose.

### A. Tighten the auto-merge threshold

Raise `BAND_AUTO_MERGE` from `0.90` to `0.95` (or higher). Borderline pairs in `[0.90, 0.95)` now fall back to M6's LLM judge instead of auto-merging.

**Pros:**
- Single-constant change. Minimal code surface (~3 lines + one threshold rationale comment).
- Catches the Alvar Aalto case (0.9061 < 0.95).
- Conservative — the bar for auto-merging without LLM verification is "near-identical input strings".

**Cons:**
- More M6 calls. The 2026-05-13 bench had 988 M6 pairs; raising the threshold to 0.95 might add maybe 1 500-3 000 pairs to M6 (rough estimate from the embed-candidates similarity distribution). M6 wall would grow proportionally (~3 s/pair → 1-3 hours extra on full corpus).
- Doesn't fix the underlying field-shape issue — if a future series clusters at 0.96+, the auto-merge band still misses it.

### B. Include `245$b` (subtitle) in the title field

Extend M3's SPARQL CONSTRUCT to pull `bf:subtitle` and concatenate `mainTitle + " : " + subtitle` into `skos:prefLabel` (or, alternately, into a separate `bffi:fullTitle` property the embedding extraction reads).

**Pros:**
- Fixes the root cause: the embedding vector now distinguishes "Alvar Aalto : mestariteoksia" from "Alvar Aalto : his life" by ~5-10 lexical units, which BGE-M3 reflects in cosine drop.
- One-time M5 re-embed re-runs the index without further changes.
- Composes with A — even if the new vectors still cluster at 0.92 (say), the raised auto-merge threshold catches them.

**Cons:**
- Changes the FAISS index — needs M5 re-build. Operator workflow: `make clean-caches` + re-run M5+M6 (already idempotent).
- The same-work pair "Alvar Aalto : a Finnish architect" (Swedish) vs "Alvar Aalto : suomalainen arkkitehti" (Finnish) — *legitimate* translation — would *lose* similarity because subtitles differ across languages. Need to confirm BGE-M3's multilingual subtitle alignment is robust enough; if not, drop similarity but rely on the title-prefix overlap + author + content-type triplet to stay above the rejection threshold (0.78). Worth a fixture-level check before shipping.

### C. Year-distance veto on auto-merge

In the auto-merge cascade decision, check `abs(year_a - year_b)`. If two records over the threshold are also more than N years apart (e.g. ≥ 5 years), demote from `auto-merge` to `escalate` (forward to M6 LLM judge).

**Pros:**
- Cheap heuristic — a single int comparison per candidate pair.
- Catches the Alvar Aalto case (2007 − 1998 = 9 years, ≥ 5).
- Documents an operator-tunable threshold so cataloguers can adjust if they observe a real re-edition pattern (the rare same-work-decades-apart case).

**Cons:**
- False-negative risk on *legitimate* re-editions decades apart (think Tolstoy translations across 50 years). Mitigated by escalating to M6 rather than rejecting outright — the LLM gets a chance to confirm.
- Requires the year field to be present on both records. Records without 008-derived `originDate` would skip the check; falls back to current auto-merge behaviour.

### D. Disable auto-merge entirely

Remove the auto-merge band — every candidate pair goes through M6 regardless of embedding similarity. Maximum quality bar at the cost of every pair paying an LLM call.

**Pros:**
- No surprise merges. Every same-work decision is LLM-verified.

**Cons:**
- M6 wall scales with candidate-pair count, not the LLM-judge subset. On the 5 k Phase B.1 bench M6 ran ~50 min for 988 pairs; without auto-merge it'd be ~50-90 hours on the same sample. Wholly impractical on the 800 k corpus.
- Throws out the spec's well-motivated auto-merge optimisation (BGE-M3 + same-block similarity ≥ 0.90 is a strong same-work signal on most records — Alvar Aalto is the exception, not the rule).

## Recommendation

Ship **B + C as a paired Phase A**, leave A and D as future-rollback knobs.

- B closes the structural blindness (subtitle is now part of the vector).
- C catches the residual cases where subtitle-extended vectors still cluster (e.g. legitimate-looking subtitle variants).
- A is a tunable safety knob — bump if the audit shows residual false positives even after B+C.
- D stays unimplemented; the spec's auto-merge optimisation is a real wall-time win that B+C preserves.

### Phase A — subtitle in the title vector + year-distance veto

**A.1 M3 SPARQL CONSTRUCT.** Pull `bf:subtitle` from the `bf:Title` node and concatenate into a `bffi:fullTitle` property. ~5 lines in `sparql/bf_to_bffi_work.rq`.

**A.2 Embedding extraction.** `_first_pref_label` becomes a 2-source fallback: prefer `bffi:fullTitle`, else `skos:prefLabel`. ~10 lines in `src/bffi_pipeline/stages/embeddings.py`.

**A.3 M5 / M6 cascade — year-distance veto.** In the auto-merge decision branch (`stages/embeddings.py` or wherever the cascade picks `same_work` at sim ≥ 0.90), add:

```python
if abs((year_a or 0) - (year_b or 0)) >= AUTO_MERGE_YEAR_GAP:
    # Escalate to M6 LLM judge; subtitle-extended embedding wasn't
    # enough on its own.
    return "escalate"
```

Default `AUTO_MERGE_YEAR_GAP = 5`; configurable via `BFFI_M5_AUTO_MERGE_YEAR_GAP` env var so the operator can tune without code change.

**A.4 Re-bench on the 20 k overnight sample.** Both metrics:
- Did the Alvar Aalto case (b1499110x ↔ b18086238) escalate to M6 (which would presumably say `different_work`)?
- Did the total `same_work` count drop without dropping legitimate merges? Quick sanity-check: spot-audit 50 random `same_work` decisions; expect ≥ 95 % to stay `same_work`.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (graduated 2026-05-14; see ../in-progress/), and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; until the observability surfaces are verified non-misleading, downstream work that consumes bench numbers is faith-based. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- A reproducible 20 k sample exists at `scratchpad/overnight-sample-2026-05-13/` (the bench from tonight). Pin this as the regression-test corpus for the proposal.
- The pinned-cataloguer-pin records in `gold/cataloguer-feedback-2026-05-13.jsonl` provide hand-verified outcomes for nineteen records (none of them flagged as auto-merge false positives by the cataloguer — they exercise reconcile, not merge); supplemental.

## Risks

- **R1 — translation pairs lose similarity** (B's main risk). Legitimate Finnish-Swedish translation pairs of the same Work would have *different* subtitles across languages; B would lower their embedding similarity. Mitigation: the FAISS index is recall-focused (top-k = 20), and the auto-merge band (now 0.95) is high enough that translation pairs typically clear it on author + main-title + content-type overlap alone. Fixture-level test on a known translation pair before shipping.
- **R2 — year-field absence** (C's edge case). Records without `originDate` skip the year-distance check. Currently the spec backfills `originDate` from MARC 008 at M2 time; coverage should be near 100 %. If not, document the fall-through and accept residual auto-merge.
- **R3 — Phase A regresses on legitimate re-editions** (B + C in combination). A 1995 first edition + 2020 reprint of the same book would now escalate to M6 instead of auto-merging. M6's job is exactly to handle that — it should say `same_work`. The wall-time cost is one extra LLM call per such pair; on the 20 k bench that's maybe tens of cases, on the full corpus maybe hundreds. Acceptable.
- **R4 — vector-space change invalidates the cache** (B). M5's FAISS index needs rebuild after the embedding-input-string format changes; M6's judge-cache might also have stale entries keyed on the prior embedding bands. Mitigation: `make clean-caches` documented as a prerequisite for Phase A's first run; cache regenerates on the next M6 invocation.

## Open questions

- Should `bffi:fullTitle` live in the BFFI vocabulary as a first-class property, or should `skos:prefLabel` itself be extended to include the subtitle? The former preserves the prefLabel-as-display-label semantic (Skosmos displays prefLabel; bare "Alvar Aalto" is more useful for UI than "Alvar Aalto : mestariteoksia"); the latter is simpler. Probably go with the former.
- Does B compose with [P-15](prop-15-bilingual-subject-reconciliation.md)? P-15 fixed authority-URI propagation through M3; B extends a different M3 CONSTRUCT clause. No interaction expected — both can ship independently.
- Should A (tighter threshold) ship at all, or is B+C enough? Initial recommendation: skip A in Phase A. Re-evaluate after Phase A's re-bench surfaces residual false-positive rate.

## Acceptance criteria (drafted; refine on graduation)

- [ ] M3 SPARQL emits `bffi:fullTitle` from `bf:Title.bf:mainTitle + " : " + bf:subtitle` (with a `" : "` separator only when subtitle is present).
- [ ] `embedding_input_string` uses `bffi:fullTitle` (fallback to `skos:prefLabel` when absent).
- [ ] Year-distance veto: pairs with `abs(year_a - year_b) >= BFFI_M5_AUTO_MERGE_YEAR_GAP` (default 5) escalate to M6 instead of auto-merging.
- [ ] Unit test: synthetic fixture with two Works ("Alvar Aalto : mestariteoksia" 1998 vs "Alvar Aalto : his life" 2007) — assert that the year-distance veto fires AND that the M6 cascade returns `different_work` on hand-supplied stub.
- [ ] Re-bench on `scratchpad/overnight-sample-2026-05-13/` shows the Alvar Aalto pair no longer auto-merging; canonical-map.jsonl has b1499110x and b18086238 in separate canonical Work URIs.
- [ ] Spot-audit 50 random `same_work` decisions from the same re-bench; ≥ 95 % preservation of legitimate merges.
- [ ] M6 wall-time growth measured + documented in the snapshot's "Implications" section.
- [ ] [`docs/performance/<date>-m5-auto-merge-mitigation.md`](../../performance/) snapshot committed.

## What this proposal does NOT do

- Doesn't redesign the M5 → M6 cascade. Auto-merge stays as the fast path; B+C just narrow its trigger.
- Doesn't change BGE-M3 or any other embedding model. The fix is in the input-string shape and the cascade gating, not the vector encoder.
- Doesn't address M9 reconciliation. M9 outcomes are unaffected — the bug is upstream at M5/M8.
- Doesn't propose a "cataloguer post-merge audit UI" — that's P-06 territory (gold-set growth) and orthogonal.
