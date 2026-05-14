# P-26 — M5 IDF-weighted title discrimination

**Status**: proposed.
**Scope**: 2-4 days end-to-end. Phase A (IDF table build at M3 finalisation): 1-2 days. Phase B (M5 cascade integration): 1-2 days. Can ship sequentially but graduates together as one plan.
**Proposal-base commit**: `6b6be25`.
**Source data**: `scratchpad/merge-cluster-verdicts/verdicts.jsonl` (audit of the 2026-05-13 overnight 20 k bench).

## Motivation

Two near-identical titles carry information unevenly: the shared
words tell you nothing about whether the records describe the same
Work; the *differing* words carry essentially all of the
discriminating signal. The intuition is information-theoretic — a
token's contribution to "are these the same Work?" scales with its
**surprise** under the corpus distribution. Shared common words ("the",
"and", "a") are low-surprise and discriminate nothing. Shared proper
nouns ("Aalto", "Naruto") are higher-surprise but, being shared,
still discriminate nothing for *this* pair. Tokens in the *symmetric
difference* — words present in one title but not the other — are the
only ones that can resolve the same-Work question.

Concrete examples from the audit:

- **"Att välja katt"** / **"Att välja hund"** (Alderton, audited as
  `different_works_same_author`). Shared tokens: `{att, välja}`.
  Symdiff: `{katt, hund}`. The shared pair tells us nothing — many
  Swedish books start with "Att välja". The pair `{katt, hund}` is
  what decides this is two distinct children's books.
- **"The Art and beauty in the Middle Ages"** / **"Art and beauty
  in the Middle Ages"** (Eco, audited as `legitimate_reedition`).
  Shared tokens: `{art, beauty, middle, ages}`. Symdiff: `{the}`.
  Symdiff is a single stopword — low information content → same Work,
  auto-merge is correct.
- **"Nuuksio, Luukki"** / **"Pallas, Hetta, Olos"** (anonymous outdoor
  maps, audit `uncertain`). Shared substantive tokens: `{}`. Symdiff:
  `{nuuksio, luukki, pallas, hetta, olos}` — all proper-noun place
  names, all discriminating → clearly distinct Works.
- **"Etsivätoimisto Henkka & Kivimutka ja kadonnut koira"** /
  **"Etsivätoimisto Henkka & Kivimutka ja MM-tason tehtävä"** (Veirto,
  audit `different_works_same_author`). Shared:
  `{etsivätoimisto, henkka, kivimutka}` (three substantive tokens
  worth of series prefix). Symdiff: `{kadonnut, koira, tason,
  tehtävä}` — the actual plot.

In each case the symdiff *alone* answers the same-Work question more
reliably than the intersection size does. This proposal lifts that
insight into M5's auto-merge gate.

### Relationship to P-22

P-22 proposes an **absolute overlap floor**: same-author pairs
with fewer than 3 substantive shared tokens demote to `escalate`.
The Alderton case ("att välja katt" / "att välja hund") has overlap
= 1 → escalate (correct). But a genuine re-edition of "Att välja
katt" with overlap = 1 → also escalate (incorrect — false escalation
for short titles, P-22's R2 risk).

P-26's IDF-weighted signal handles both cleanly:
- Alderton: symdiff = `{katt, hund}`, mass ≈ 2 × log(N / d_animal-noun)
  → above threshold → escalate.
- Same-title reedition: symdiff = `{}`, mass = 0 → auto-merge.
- Eco "The Art and beauty" / "Art and beauty": symdiff = `{the}`,
  mass ≈ log(N / d_"the") ≈ 0 → auto-merge.

The IDF approach is **strictly more powerful** than absolute overlap,
extends naturally to anonymous-work clusters where P-22 can't
fire (no author precondition), and is self-tuning per language (no
hand-curated stopword list to maintain across Estonian / Polish /
Korean as the corpus expands). P-26 ships as a *replacement* for
P-22; my recommendation is to mark P-22 `rejected (superseded
by P-26)` on P-26 graduation.

### Why M5 doesn't already do this

`embedding_input_string` (`src/bffi_pipeline/stages/embeddings.py:212`)
hands the title as one opaque string to BGE-M3, which embeds the
*whole* sequence. Cosine similarity weights every position roughly
equally — it has no "which tokens differ" signal at the cascade
layer. The audit script's `_substantive_tokens` already extracts the
machinery; this proposal lifts it into the cascade decision.

## Approach

The signal is **discriminator mass**: the sum of IDF weights of the
tokens that appear in one title but not the other. Common
template-words contribute near-zero (they appear in many titles, so
their IDF is low); rare proper nouns / numerals / topic-specific
words contribute heavily (they appear in few titles, so their IDF is
high). The pair is auto-merge-safe iff the discriminator mass is
below a corpus-calibrated threshold.

The whole thing is *per-language* by accident-of-construction: a
Swedish common word ("att") naturally lands in the bottom IDF tier
of the Swedish title corpus; we don't have to hand-curate a Swedish
stopword list, and the same holds for Estonian / Polish / Korean as
the corpus expands.

### Phase A — IDF table build at M3 finalisation

Iterate every `bffi:fullTitle` literal in the corpus once and write a
serialised IDF table:

```python
df: Counter[str] = Counter()
n_titles = 0
for title in iter_all_full_titles():  # bffi:fullTitle from P-20
    n_titles += 1
    df.update(set(_tokenise(title)))  # set() — each title counts once per token

idf: dict[str, float] = {t: log(n_titles / d) for t, d in df.items()}
```

`_tokenise` is NFKD-fold + casefold + `[\wäöåÄÖÅüÜ]+` regex + filter
to `len > 2` (length filter as a sanity floor, *not* a stopword
filter — IDF replaces stopwords). Lifts to
`src/bffi_pipeline/text/tokens.py`, shared between audit, M5 cascade,
and the IDF builder.

Persist as `<BFFI_DATA_DIR>/title-token-idf.parquet`. Expected size
~5 MB for ~50 k unique tokens on the 800 k corpus. Two columns:
`token: str`, `idf: float32`. Idempotent rebuild when
`bffi-corpus.ttl` (per P-19) is newer than the parquet.

Wire as a new M3 finalisation step in
`src/bffi_pipeline/stages/bf_to_bffi.py` (or a new
`stages/idf_index.py` for cleanliness — preferred, the build is a
self-contained pass). Emit standard `start` / `end` observability
events with counters `{n_titles, n_unique_tokens, max_idf, min_idf}`
so the run gets a Grafana row.

### Phase B — IDF-weighted discrimination at M5

At the M5 auto-merge band (similarity ≥ 0.90), compute discriminator
mass on the title symmetric difference and demote to `escalate` if
the mass exceeds threshold:

```python
def _discriminator_mass(a_title: str, b_title: str, idf: dict[str, float]) -> float:
    a_tokens = _tokenise(a_title)
    b_tokens = _tokenise(b_title)
    symdiff = a_tokens ^ b_tokens
    return sum(idf.get(t, _IDF_UNKNOWN) for t in symdiff)

def _idf_escalation(a: WorkEmbeddingInput, b: WorkEmbeddingInput, idf) -> bool:
    return _discriminator_mass(a.title or "", b.title or "", idf) \
        >= AUTO_MERGE_DISCRIMINATOR_MIN
```

`_IDF_UNKNOWN` is the IDF assigned to out-of-vocabulary tokens.
Reasonable choice: the maximum observed IDF (i.e. treat unknowns as
*maximally* discriminating — a one-off proper noun is exactly the
kind of token we want to weight high). Configurable.

**Threshold calibration.** `AUTO_MERGE_DISCRIMINATOR_MIN` is fit
against the audit baseline: pick the value that escalates ≥ 95 % of
audit-flagged FP rows (`different_works_same_author` +
`series_volumes_collapsed` + `subtitle_divergence` etc.) while
keeping ≤ 2 % of `legitimate_reedition` rows over-escalated. Expected
band: `log(N / d)` where the average discriminating token appears in
0.1-1 % of titles, i.e. ~5-7 per token, so threshold ~5 is a sensible
starting point. The script that fits the threshold lives at
`scripts/fit-discriminator-threshold.py` and operates over
`scratchpad/merge-cluster-verdicts/verdicts.jsonl`.

**Cascade integration.** Add a `cascade_decide(a, b, similarity, idf)`
helper in `embeddings.py` that wraps `classify_band` and chains all
auto-merge-band vetoes (P-20 year-distance, P-23 numeric
markers, P-24 distinct authors, P-25 anthology scope, P-26
IDF discriminator) cheapest-first. Short-circuit on first hit. M5's
per-pair loop calls `cascade_decide` instead of `classify_band`
directly.

### Cold-start fallback

When the IDF table is missing (first run on a fresh `BFFI_DATA_DIR`,
or before Phase A has shipped), fall back to a boolean symdiff check:
non-empty substantive symdiff → escalate. This is strictly weaker
than IDF weighting — it over-escalates short same-title re-editions
("Tutu" / "Tutu") and any case where the symdiff is one common word
— but it's better than auto-merging blindly. Emit a `idf_cold_start`
counter on M5's observability sidecar so operators see when the
fallback is in effect.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (completed 2026-05-14; see ../completed/), and P-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove P-27 around a false premise; until the observability surfaces are verified non-misleading, downstream work that consumes bench numbers is faith-based. See [`P-30`](p-30-observability-audit-trail-critical-audit.md).
- **P-20 (recommended)** — the IDF table should be built over
  `bffi:fullTitle` (main title + subtitle, P-20's output) rather
  than `bf:mainTitle` alone, so that the discriminator mass reflects
  the full title surface. Phase A can ship before P-20 by reading
  `skos:prefLabel`, but the threshold will need recalibration when
  P-20 lands.
- **P-19 (recommended)** — the IDF build needs a corpus-level
  title iterator. Without P-19's concat file the build has to
  scan the per-record `.ttl` store at ~3-4 hours wall on 800 k; with
  P-19's `bffi-corpus.ttl` it's a one-pass parse → ~1 min.
  Workable without P-19 if necessary (the build runs at M3
  finalisation regardless, just slower).
- **`scratchpad/merge-cluster-verdicts/verdicts.jsonl`** — the
  2026-05-13 audit baseline is the regression corpus AND the
  threshold-calibration corpus.

## Risks

- **R1 — IDF cold start.** On a first run against a fresh
  `BFFI_DATA_DIR`, the IDF table doesn't exist yet. Cold-start
  fallback (boolean symdiff check) keeps M5 conservative until the
  table is built. Logged via the `idf_cold_start` counter so the
  operator sees the degraded-mode period.
- **R2 — Stale IDF after corpus growth.** The IDF table is computed
  from the corpus *at build time*; if the corpus grows materially
  (say from 800 k to 1 M records as more Helmet exports flow in), the
  IDF values drift slightly. Mitigation: idempotent rebuild — the
  table re-builds whenever `bffi-corpus.ttl` is newer than the parquet.
  Threshold is set on `log(N/d)` so absolute IDF values are stable
  under proportional corpus growth.
- **R3 — OOV proper nouns over-escalate.** A pair of records whose
  titles share a rare proper noun ("Hesburger franchise history"
  edition 1 vs edition 2) — the proper noun has IDF near the max
  observed value. Symdiff = `{}` → mass = 0 → auto-merge.
  Correct. The R3 case is harder: a pair where the symdiff contains
  one OOV proper noun ("Hesburger Helsinki" vs "Hesburger Tampere").
  Mass = `_IDF_UNKNOWN` ≈ max-IDF → escalate. Correct: two distinct
  Works.
- **R4 — Threshold sensitivity.** The single
  `AUTO_MERGE_DISCRIMINATOR_MIN` constant has to balance FP (auto-
  merging two distinct Works) against FE (over-escalating a real re-
  edition to M6 and paying an LLM call). Calibration script runs over
  the audit baseline and emits a precision-recall curve; ship at the
  knee. Operator can adjust via env var without code change.
- **R5 — Multilingual NFKD edge cases.** "Vägkarta" / "Vagkarta"
  (Swedish with/without ä diacritic) should collapse to the same
  token under NFKD + non-alphanumeric strip. The audit's
  `_norm_author` does this; the title tokeniser must match — fixture
  test: load a Swedish title with combining diacritic + a Finnish
  title with å and assert stable tokens.
- **R6 — Composability with sibling vetoes.** P-20 / P-23 /
  P-24 / P-25 / P-26 all demote `auto-merge` → `escalate`
  on disjoint conditions. Implementation: a single `cascade_decide`
  chains them cheapest-first (P-24 author-mismatch first → P-23
  marker veto → P-26 IDF mass → P-20 year-distance → P-25
  scope veto). Short-circuit on first hit. Each veto is
  independently testable.
- **R7 — Audit / production drift.** If the audit script and the
  production cascade use different tokenisers, they'll disagree.
  Mitigation: both import `_tokenise` and `_IDF_UNKNOWN` from
  `src/bffi_pipeline/text/tokens.py`; a lint check (or a smoke test
  on a fixed input) asserts identical output across audit + M5.

## Open questions

- **Sum vs max IDF.** `sum(idf[t] for t in symdiff)` aggregates the
  mass; an alternative is `max(idf[t] for t in symdiff)` — "does the
  symdiff contain *any* highly-discriminating token". Sum is more
  robust against short titles (a single rare word sets the verdict);
  max is more interpretable. Default to sum; revisit if calibration
  shows max-IDF gives a tighter precision-recall curve.
- **IDF coverage scope.** Only titles, or also authors / publishers /
  years? Only titles for now — the M5 cascade already gates author
  (P-24) and year (P-20) separately. Adding publisher might
  help on cataloguing-template-driven collisions but is out of scope
  here.
- **Position-aware variant.** Would Smith-Waterman alignment on
  tokens (catching "att välja **katt**" vs "att välja **hund**" as a
  one-position substitution) carry more signal than set-based
  symdiff? Probably no — Helmet titles average 5-12 tokens and word
  order is broadly stable across re-editions, so set-based and
  position-based should agree on > 95 % of pairs. Skip unless
  residual errors show a position-driven pattern.
- **Token n-grams.** "world war ii" vs "world war i" — symdiff at
  the unigram level is `{ii, i}`, both length-2 → filtered out, mass
  0 → auto-merge (incorrect). One mitigation: drop the `len > 2`
  filter for tokens with high IDF (a length-2 token that *is* in the
  IDF table and has IDF above threshold is meaningful). Another:
  consider bigrams. Length-2 fix is simpler; ship that, revisit
  bigrams if titles like the WWI case actually appear in the corpus.
- **IDF on `bffi:fullTitle` requires P-20.** Without P-20, the
  IDF table is built over `skos:prefLabel` (main title only), losing
  the subtitle. Sequence: ship P-20 first, then P-26, so the
  IDF table sees the richer surface.

## Acceptance criteria (drafted; refine on graduation)

**Phase A — IDF table build**
- [ ] `src/bffi_pipeline/text/tokens.py` exports `_tokenise` (NFKD
      + casefold + regex + `len > 2`) and `_IDF_UNKNOWN`. Audit
      script imports from there; production cascade imports from
      there; smoke test asserts both produce identical output on a
      fixed fixture.
- [ ] New stage (or M3 finalisation step) writes
      `<BFFI_DATA_DIR>/title-token-idf.parquet` from
      `bffi:fullTitle` (or `skos:prefLabel` pre-P-20). Idempotent
      rebuild when `bffi-corpus.ttl` is newer than the parquet.
- [ ] Standard `start` / `end` observability events with counters
      `{n_titles, n_unique_tokens, max_idf, min_idf}`.
- [ ] Build wall-time on 20 k bench < 30 s; on 800 k corpus < 5 min
      (given P-19's concat file).

**Phase B — M5 IDF-weighted discrimination**
- [ ] M5 reads the IDF table at startup; falls back to boolean
      symdiff (with an `idf_cold_start` counter) when the table is
      absent.
- [ ] `cascade_decide` helper chains the five auto-merge-band
      vetoes (P-20 / 23 / 24 / 25 / 26) cheapest-first, short-
      circuiting on first hit.
- [ ] `AUTO_MERGE_DISCRIMINATOR_MIN` calibrated against the audit
      baseline; the calibration script
      (`scripts/fit-discriminator-threshold.py`) emits a precision-
      recall curve and a recommended threshold. Ship at the curve's
      knee.
- [ ] Re-bench on `scratchpad/overnight-sample-2026-05-13/`:
      - ≥ 95 % of audit-flagged FP rows (40
        `different_works_same_author` + 34 `series_volumes_collapsed`
        + 11 `subtitle_divergence` + 2 `different_scope_same_canon`)
        escalate at the calibrated threshold.
      - ≤ 2 % of `legitimate_reedition` rows over-escalate.
- [ ] M6 wall-growth measured + documented in the snapshot's
      "Implications" section.
- [ ] [`docs/performance/<date>-m5-idf-discrimination.md`](../../performance/)
      snapshot committed.

## What this proposal does NOT do

- Doesn't change BGE-M3 or any other embedding model. The fix is in
  the *post-similarity* cascade decision.
- Doesn't propose a separate per-token embedder for the symdiff
  tokens (option 3 from the design discussion was deferred —
  marginal lift vs IDF, adds an encoder call per pair).
- Doesn't replace the LLM judge at M6 — symdiff escalates *to* M6,
  not *past* it.
- Doesn't subsume P-23 (numeric markers) or P-24 (distinct
  authors): those vetoes trip on conditions the symdiff alone
  doesn't see (volume numbers may share IDF with rare words; author
  mismatch is on the `creator:` field, not the title). The five
  vetoes (P-20, P-23, P-24, P-25, P-26) compose
  cleanly.

## Why this supersedes P-22

P-22 ships a same-author absolute-overlap floor (`min_overlap < 3`).
P-26's IDF-weighted discrimination dominates it on every axis:

- **Same-author false positives** — IDF catches every case P-22's
  floor catches (same-author pairs whose distinct titles share only
  template prefixes). The discriminator-mass score lights up because
  the distinguishing tokens have high IDF.
- **Anonymous-work false positives** — P-22 skips clusters with
  `len(authors) != 1` (no author or two different authors); P-26
  fires on title-text alone, catching the Karttakeskus outdoor-map
  case (`Nuuksio, Luukki` vs `Pallas, Hetta, Olos`) that P-22 can't
  see.
- **Short-title re-edition false escalations** — P-22's
  `min_overlap < 3` over-escalates "Tutu" / "Tutu" (overlap = 1).
  P-26's symdiff = `{}` → mass = 0 → correctly auto-merges.
- **Multilingual coverage** — P-22 depends on a hand-curated
  multilingual stopword list. P-26 derives stop-tokens from the
  corpus itself; each new language tunes in automatically.

If both ship, P-22's floor becomes dead code. Cleanest path:
graduate P-26 first; mark P-22 as `rejected (superseded by
P-26)` when P-26 graduates.
