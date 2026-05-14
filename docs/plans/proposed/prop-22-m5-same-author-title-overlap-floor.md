# P-22 — M5 same-author title-overlap floor (different-works-same-author class)

**Status**: proposed.
**Scope**: 1-2 days.
**Proposal-base commit**: `6b6be25`. To gauge drift before acting, run
`git diff 6b6be25..HEAD -- src/bffi_pipeline/stages/embeddings.py src/bffi_pipeline/stages/merge.py sparql/`.
**Source data**: `scratchpad/merge-cluster-verdicts/verdicts.jsonl` (audit of the 2026-05-13 overnight 20 k bench).

## Motivation

The merge-cluster audit on the 2026-05-13 overnight bench surfaced
**different_works_same_author** as the **dominant false-positive class**:
**40 / 183 clusters (21.9 %)**. The pattern is one author whose
distinct books share enough lexical context — a series prefix, a
recurring character name, or simply a shared blocking key — that M5's
auto-merge band (sim ≥ 0.90) collapses them into one canonical Work.

Examples from the audit (all sit in the 0.90-0.95 auto-merge slice
*before* prop-20's threshold-tightening, and would survive even after
prop-20's subtitle-in-vector and year-distance fixes because the
subtitles match and the publication years are close):

- **b22897847 / b2428466x / b24524566** — Aron, Elaine N.: three
  distinct titles in the "Erityisherkkä..." (highly-sensitive person)
  catalog. Two records share "ihminen ja parisuhde", a third is
  "vanhempi". Author dominates the vector; titles diverge in the
  suffix only.
- **b19583163 / b22559528** — Veirto, Kalle: "Etsivätoimisto Henkka &
  Kivimutka **ja kadonnut koira**" vs "...**ja MM-tason tehtävä**".
  Two adventures in a children's detective series, ~80 % shared
  prefix, distinct stories.
- **b25257961 / b25257973** — Heikel, Henrik: "Lärobok i geometrin
  ... **första boken** af Euclides' Elementa" vs "...**sex böcker** af
  Euklides' Elementa". Same Swedish geometry textbook author, two
  editions with different scopes (1 vs 6 books of Euclid).

Forty cases × the linear-extrapolation factor (~40 in 20 k bench →
**~1 600 false merges projected** on the 800 k corpus) makes this the
single largest M5 quality issue surfaced by the audit. It is
disjoint from prop-20 (Schildt-on-Aalto / subtitle-divergence) and
prop-21 (Aalto-as-subject / LLM hallucinated translation): those
classes shared a main title; this class has *distinct* main titles
that nonetheless cosine-cluster.

### Why the embeddings collide

`src/bffi_pipeline/stages/embeddings.py:212` packs five
pipe-separated fields:

```
creator: Veirto, Kalle | title: Etsivätoimisto Henkka & Kivimutka ja kadonnut koira | language: fin | year: 2010 | type: txt
creator: Veirto, Kalle | title: Etsivätoimisto Henkka & Kivimutka ja MM-tason tehtävä   | language: fin | year: 2016 | type: txt
```

The `title` field varies in the *suffix*; the rest of the string is
identical. BGE-M3 treats the shared prefix as strong common signal
and lands these pairs at sim 0.91-0.94 — inside the auto-merge band.

## Approach

Add a **content-token overlap floor** to the auto-merge decision: when
both records share an author and the auto-merge band is hit, compute
stopword-filtered token overlap on the title field. Demote to
`escalate` if the *substantive* (non-stopword, len > 2) title-token
overlap is below a threshold.

The overlap heuristic is exactly the one the audit script uses to
classify this category (`_content_tokens` + min-pairwise overlap < 3
substantive shared tokens). Lifting it from the audit into the M5
cascade closes the audit-vs-production loop: clusters that the audit
flags as `different_works_same_author` would now escalate to M6 at
auto-merge time.

### A — Token-overlap floor (recommended)

At `classify_band` (or a new `cascade_decision` wrapper that has access
to the candidate pair's WorkEmbeddingInput objects), compute:

```python
def _substantive_tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[\wäöåÄÖÅüÜ]+", title.lower())
    return frozenset(w for w in words if len(w) > 2 and w not in _STOPWORDS)

shared = _substantive_tokens(a.title) & _substantive_tokens(b.title)
if (
    a.creator and b.creator
    and a.creator == b.creator
    and len(shared) < AUTO_MERGE_TITLE_OVERLAP_MIN  # default 3
):
    return "escalate"
```

`_STOPWORDS` is the multilingual set already used by
`scripts/audit-merge-clusters.py` (English/Swedish/Finnish + a small
Romance/Germanic core). Pull it into a shared module
(`src/bffi_pipeline/text/stopwords.py`) so production code and the
audit harness agree.

### B — Author-weighted similarity penalty (alternative, deferred)

A more principled alternative: when same-author pairs hit the
auto-merge band, multiply the similarity by `(1 - bonus_from_creator)`
to remove the author's contribution to cosine. Requires re-running
the embedder against a creator-masked input, which doubles M5
wall-time. Skip in the first phase; revisit if A's residual false-
positive rate stays high.

## Phases

**A.1 Extract `_STOPWORDS` and `_substantive_tokens` to
`src/bffi_pipeline/text/stopwords.py`.** ~30 lines + tests. The audit
script imports from the new module; current behaviour unchanged.

**A.2 Add token-overlap floor at the auto-merge decision point.**
~15 lines in `src/bffi_pipeline/stages/embeddings.py` (extend
`classify_band` signature or add a new `decide_with_context` that the
caller in `merge.py` uses). Default
`AUTO_MERGE_TITLE_OVERLAP_MIN = 3`, configurable via
`BFFI_M5_AUTO_MERGE_TITLE_OVERLAP_MIN`.

**A.3 Re-bench on `scratchpad/overnight-sample-2026-05-13/`.** Verify:
- The 40 audited `different_works_same_author` clusters now escalate
  to M6 at auto-merge time (and that M6 returns `different_work` on at
  least a spot-audit sample of 10).
- Legitimate same-author re-editions (64 `legitimate_reedition` cases)
  do *not* get over-escalated. The token-overlap floor is anchored on
  the title text *after* prop-20 ships `bffi:fullTitle`, so re-editions
  share their full normalised title and clear the floor trivially.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (graduated 2026-05-14; see ../in-progress/), and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; until the observability surfaces are verified non-misleading, downstream work that consumes bench numbers is faith-based. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- The 2026-05-13 overnight 20 k sample at
  `scratchpad/overnight-sample-2026-05-13/`. Treat as the regression
  corpus for the proposal.
- Audit baseline at `scratchpad/merge-cluster-verdicts/verdicts.jsonl`.
  The 40 `different_works_same_author` rows are the regression
  oracle: each should escalate after the change.
- Composes with prop-20: prop-20 adds `bffi:fullTitle` to the
  embedding, which strengthens this proposal's title-token signal
  (more tokens to overlap on). Order doesn't strictly matter, but
  shipping prop-20 first is preferable because it expands the title
  surface that this floor operates on.

## Risks

- **R1 — legitimate-translation false-escalation.** Translation pairs
  share zero substantive tokens across languages (the whole point of
  translation). They'd get caught by the floor and escalated. Two
  mitigations: (a) the floor only fires when *both* records have the
  same `language` field — translation pairs differ on `language` and
  skip the check; (b) M6 is the right place to confirm translations
  anyway. Net: low risk, mainly extra M6 cost.
- **R2 — short titles below the threshold.** A two-word
  cookbook title ("Salt fat") shares < 3 substantive tokens with a
  re-edition of itself if one record's title is recorded slightly
  differently. Mitigation: when `min(len(_substantive_tokens(a.title)),
  len(_substantive_tokens(b.title))) < AUTO_MERGE_TITLE_OVERLAP_MIN`,
  skip the floor — there aren't enough tokens to discriminate.
- **R3 — M6 wall growth.** Estimated +40 cases on 20 k bench → +1 600
  on 800 k → +1.3 hours at 3 s/pair. Acceptable; M6 is the correct
  place for these.
- **R4 — `_STOPWORDS` coverage drift.** The audit script's
  stopword list is hand-curated for the languages observed so far. If
  the corpus expands to Estonian, Polish, etc., the list needs
  extension. Mitigation: log a counter of "auto-merge demoted by
  token-overlap floor"; if it spikes for a new language pair, extend
  the list. Counter lives in M5 observability emitters.

## Open questions

- Should the floor apply across *any* shared field (publisher, year)
  rather than only `creator`? Probably no — the failure mode is
  specifically "same author writes multiple distinct works", and
  generalising weakens the precision.
- Where does the `_STOPWORDS` module live — `src/bffi_pipeline/text/`,
  `src/bffi_pipeline/stages/`, or a top-level utility? Suggest
  `src/bffi_pipeline/text/` (new package) since prop-23 will want it
  too.
- Should `AUTO_MERGE_TITLE_OVERLAP_MIN` be a proportion (e.g. ≥ 50 %
  of the smaller title's tokens) instead of an absolute count? The
  audit used absolute count and worked well; revisit if R2 cases
  surface.
- Compose with prop-16 (fallback-tier gating)? Both share an
  "auto-merge → escalate" demote pathway. No direct interaction; they
  trip on disjoint conditions.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `_STOPWORDS` and `_substantive_tokens` live in a shared module,
      imported by both the audit script and M5.
- [ ] M5 auto-merge band demotes to `escalate` when same-author pairs
      share fewer than `AUTO_MERGE_TITLE_OVERLAP_MIN` (default 3)
      substantive title tokens.
- [ ] Re-bench on `scratchpad/overnight-sample-2026-05-13/`: the 40
      `different_works_same_author` clusters no longer auto-merge.
- [ ] Spot-audit 20 of the M6-escalated cases; ≥ 80 % return
      `different_work` (the rest stay `same_work` as expected for
      ambiguous cases).
- [ ] Legitimate-reedition regression check: of the 64
      `legitimate_reedition` audit rows, ≥ 95 % continue to
      auto-merge.

## What this proposal does NOT do

- Doesn't add a new ML model or embedding step.
- Doesn't touch M6 prompts (that's prop-21 territory).
- Doesn't change M8 union-find semantics.
- Doesn't propose an author-disambiguation step (prop-24's territory).
