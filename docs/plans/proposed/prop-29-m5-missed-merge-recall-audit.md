# P-29 — M5 missed-merge audit (the recall side)

**Status**: proposed.
**Scope**: 3-4 days (gold-set construction + audit script + initial run).
**Proposal-base commit**: `6b6be25`.
**Source data**: `scratchpad/overnight-sample-2026-05-13/`, `gold/cataloguer-feedback-2026-05-13.jsonl`, `gold/gold.jsonl`.

## Motivation

Every audit we've run looks at **what merged**: prop-22 / 23 / 24 /
25 / 26 / 27 all sift through `canonical-map.jsonl` and ask "is this
merge wrong?" That's the **false-positive surface** — pairs that
auto-merged when they shouldn't have. We have 96 % audit-classified
coverage there.

The **false-negative surface** is invisible: pairs that *should*
have merged but didn't. Three sub-cases, each silent in different
ways:

1. **M6 `different_work` verdicts on legitimate same-Work pairs.**
   The 2026-05-13 bench produced 905 M6 `different_work` verdicts
   (`judge-cache.sqlite`). If even 5 % are wrong, that's ~45 missed
   merges per 20 k → ~1 800 on the 800 k corpus. Some of these
   would be auditable by the heuristic classifier (prop-27 sub-
   audit B is a 50-pair spot-check; prop-29 systematises it).

2. **Never-candidate pairs.** Records that *should* be a same-Work
   pair but landed in different blocking keys (different
   normalised-author × normalised-title × content-type triples).
   The two records are never compared. M5's blocker has zero
   visibility into them. Estimated incidence unknown — that's the
   point of the audit.

3. **Cross-block pairs above the escalate threshold.** M5 has a
   `cross_block: bool` flag on each pair; some embedder
   configurations cross blocking-key boundaries when similarity
   alone is strong. Helmet records with diacritic variants
   ("Le Carré, John" vs "Le Carre, John") or transliteration
   variants ("Mo Xiang Tong Xiu" vs "墨香铜臭") block differently
   despite being the same author. Cross-block recall is currently
   measured nowhere.

Cataloguer feedback for the 2026-05-13 sample
(`gold/cataloguer-feedback-2026-05-13.jsonl`) listed 19 reconcile-
focused records but flagged *zero* merge-recall concerns — not
because the recall is perfect, but because the cataloguer's
attention was elsewhere. **There is no current measurement of M5
recall at all.**

Without a recall audit, the pipeline could be silently fragmenting
the canonical Work graph: every Finnish-Swedish translation pair
where M3 picked different blocking keys is one missed canonical Work
URI. On the 800 k corpus, this could be in the **low thousands** —
of comparable magnitude to the false-positive surface we've been
focused on.

## Approach

Bootstrap a small gold set of "should have merged" pairs, then
measure whether M5 found them. Three sources of gold pairs, in
order of cost-per-pair:

### A — Bootstrap from existing same-work decisions

Walk `canonical-map.jsonl`'s union-find groups; for each group with
≥ 3 records, generate all pairs and check which ones M5 *separately
generated as candidates* vs which only ended up co-clustered through
transitivity. The non-candidate pairs in the same group are *de
facto* missed direct-match opportunities — they were saved by
transitivity but M5's blocker didn't see them as candidates.

```python
for group in canonical_groups:
    if len(group) < 3:
        continue
    direct_candidates = m5_candidate_pairs[group]
    for a, b in combinations(group, 2):
        if (a, b) not in direct_candidates:
            yield ("transitivity-only", a, b)
```

**Why this is cheap:** uses existing data; no cataloguer input
needed. **Why it's limited:** finds only the missed pairs whose
endpoints are in some same-work group. Records that ended up alone
because M5 missed *every* candidate are invisible to this method.

### B — KANTO/VIAF authority-driven gold pairs

For records carrying a 100$0 → KANTO URI, group by KANTO URI + 245$a
normalised. Two records with the same authority-resolved creator and
similar title across language variants are a strong same-work signal
*regardless* of M5's verdict. Cross-reference these candidate pairs
against `canonical-map.jsonl`: any pair the authority data says is
the same Work, but the canonical map splits, is a missed merge.

**Why this is medium cost:** requires that KANTO URIs are populated
on a meaningful fraction of records (the cataloguer-pinned subset
confirms ≥ 13 % have FI-ASTERI-N). **Why it's stronger:** the
authority resolution is independent of M5's logic — it can't be
"saved" by the same blocker that produced the miss.

### C — Cataloguer-pinned gold pairs

Ask the cataloguer to label N records (~50-100) for known
same-work relationships across the bench: translations, re-editions,
art-book reissues, etc. Records the audit script's
`legitimate_translation` and `legitimate_reedition` heuristics
already classified are good seed candidates — the cataloguer
validates a sample.

**Why this is high cost:** human time. **Why it's necessary:** A
and B are derivable from the corpus; they can miss cases the
corpus structure itself hides (e.g. a re-edition that was indexed
incorrectly and never blocked together at all).

## Audit script

`scripts/audit-missed-merges.py`. Inputs:
- `--gold` — JSONL of `{work_a_uri, work_b_uri, expected: same_work,
  source: A|B|C}` rows.
- `--canonical-map` — frozen M8 output.
- `--candidate-pairs` — M5's blocker output (the list of pairs that
  entered the cascade).
- `--marcxml-dir` — for context.

Outputs:
- `verdicts.jsonl` — per-gold-pair: `(found, similarity, decision)`
  or `(missed, reason)`.
- `summary.md` — distribution of recall failures by mode (auto-
  rejected, escalate-rejected, never-candidate), with examples per
  mode.

Key metric: **recall@candidate** (of gold pairs M5 generated as
candidates, what fraction reached `same_work`?) and **recall@blocker**
(of gold pairs total, what fraction M5 even generated as
candidates?). Today both are unmeasured; after Phase A we'll have at
least an order-of-magnitude estimate.

## Phases

**A — Bootstrap gold set from transitivity (zero cataloguer time).**
Write the transitivity-pair extractor; produce ~50-100 pairs from the
20 k bench. Run the audit.

**B — Bootstrap gold set from KANTO URIs.** Parametric on KANTO
coverage; should yield another ~50-100 pairs on the 20 k bench.

**C — Cataloguer-pinned gold.** Ask Helmet for review of a
~50-pair sample seeded from A + B. Defer until A + B's results
motivate the request (don't burn cataloguer time before we have
something to show).

Each phase outputs to `scratchpad/missed-merge-audit-<date>/`.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** prop-17, prop-18, and prop-19 must be implemented, and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; recall audits read bench candidate-pair and decision data, both of which need to be verified non-misleading first. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- `scratchpad/overnight-sample-2026-05-13/judge-decisions.jsonl` +
  the corresponding M5 candidate-pair file (locate or regenerate).
  The candidate-pair list is what tells us *which* pairs M5 even
  considered — required for the recall@blocker metric.
- prop-09 (library-agnostic source) — *not* a hard prerequisite, but
  if Phase B's KANTO logic is going to expand to other libraries
  later, the authority-URI extraction should be source-agnostic.

## Risks

- **R1 — Transitivity bootstrap is biased.** The gold pairs produced
  by Phase A are pairs M5 *did* identify (just not directly). They
  systematically miss the cases where the blocker fails completely.
  Phases B and C are what closes that gap.
- **R2 — Authority-driven gold pairs have false positives.** Two
  records citing the same KANTO author URI + similar titles aren't
  *necessarily* the same Work — they could be two different books by
  the same author. Mitigation: filter Phase B's candidates through
  the existing audit script's `legitimate_reedition` /
  `legitimate_translation` heuristics first; what remains is
  high-confidence gold.
- **R3 — Cataloguer time is precious.** Don't burn it on cases A and
  B can already answer. Save Phase C for the residual uncertainty
  the bootstrap methods leave.
- **R4 — Missed-merge fixes are not in this proposal's scope.**
  Phase A may reveal that, say, 200 / 800 k pairs are missed because
  M5's blocker is too narrow. The *fix* (broaden the blocker,
  add a fuzzy-match pre-pass, etc.) is downstream work — likely a
  prop-30 motivated by this proposal's findings.
- **R5 — Block-key changes risk cascade-FP increases.** Loosening
  the blocker to catch missed merges generates more candidate pairs
  → more auto-merges → more FPs. Any blocker-broadening proposal
  needs to compose with the FP veto stack (prop-20 through 26).

## Open questions

- Should the recall audit run on the full 20 k bench or just the
  ~400-record FP-cluster subset prop-28 pins? Recommend full 20 k
  for Phase A so we get meaningful recall numerators; the FP subset
  is too small.
- Is "M5 candidate pair" the right baseline for recall@blocker, or
  should we measure against "M5 + M9 reconcile" (M9 also sometimes
  collapses records)? Start with M5 alone — measuring the M5
  blocker in isolation is the first signal; M9's contribution to
  recall is a separate measurement.
- How does this interact with prop-27's sub-audit B? prop-27 B
  systematically classifies all 905 M6 `different_work` verdicts.
  prop-29 expands the scope to *never-candidate* pairs (which
  prop-27 can't see — they never reached M6 at all). They compose:
  prop-27 B covers the M6-reached-but-rejected slice; prop-29
  covers the M5-blocker-never-saw-them slice.
- Should Phase A also pull from M9's reconcile-output's same-Work
  decisions? Same question — start with M5 only.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `scripts/audit-missed-merges.py` committed.
- [ ] Phase A: ≥ 50 transitivity-derived gold pairs extracted;
      recall@candidate measured.
- [ ] Phase B: ≥ 50 KANTO-derived gold pairs extracted (if KANTO
      coverage allows); recall@blocker measured.
- [ ] `docs/performance/<date>-missed-merge-audit.md` snapshot
      with: recall@candidate, recall@blocker, top-3 failure modes
      by frequency.
- [ ] Open follow-up proposal (prop-30) for whichever failure mode
      dominates Phase A + B output.

## What this proposal does NOT do

- Doesn't fix any missed merges. It's a measurement proposal that
  motivates future fix proposals.
- Doesn't redesign M5's blocking key. Block-key design is a separate
  decision once we know which records are missing each other.
- Doesn't replace cataloguer feedback. Phase C uses it sparingly
  for cases the bootstrap methods can't resolve.
- Doesn't touch M6 (prop-27) or production code (prop-28). Pure
  offline audit.

## Composition with sibling proposals

- **Independent of the FP veto stack.** prop-20 / 23 / 24 / 25 / 26
  tighten precision; prop-29 measures recall. Both can ship
  independently. Eventually they trade off: a recall-driven blocker
  loosening (post-prop-29) increases candidate pairs and stresses
  the veto stack more.
- **Composes with prop-27.** prop-27's sub-audit B is a 50-row
  spot-check of the auto-reject band; prop-29 Phase A scales that
  to the full bench plus the never-candidate population.
- **Feeds prop-28.** Once a missed-merge fixture exists, prop-28's
  CI pattern can pin it as a recall regression baseline alongside
  the FP regression baseline.
