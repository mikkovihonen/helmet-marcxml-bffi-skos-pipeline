# P-24 — M5 distinct-author veto (different-authors-collapsed class)

**Status**: proposed.
**Scope**: half-day.
**Proposal-base commit**: `6b6be25`.
**Source data**: `scratchpad/merge-cluster-verdicts/verdicts.jsonl`.

## Motivation

The merge-cluster audit found **2 / 183 clusters (1.1 %)** where the
M5 auto-merge band merged records with **distinct MARC-100
authors** — a class FRBR rules out by definition. While the absolute
count is small, the failure mode is qualitatively worst-case: any
such merge erases an authorship signal that cataloguers explicitly
encoded.

Linear-extrapolation to 800 k: ~80 false merges projected, each
silently dropping an author from the canonical Work graph.

This proposal is the **cheapest insurance policy** of the four
overnight-audit-derived proposals (prop-22 / prop-23 / prop-24 /
prop-25) and the highest qualitative payoff per line of code.

### Root cause

The embedding-input string concatenates the *first* primary
contributor's label into `creator:` (`embeddings.py:212`). When two
records carry different primary contributors but share enough title
signal to clear 0.90 cosine, M5 auto-merges without checking that the
creator strings actually match.

Audit examples are anonymised in the JSONL because the audit script
reports `marc_authors` as a set rather than per-record. Re-running
with `--debug` would surface the specific pairs; both fall into the
pattern *"two scholars with similar-prefix titles, M5 doesn't notice
the creator difference"*.

## Approach

Single-line veto at the auto-merge decision: if the auto-merge band
is hit AND `a.creator` and `b.creator` are both non-empty AND
`norm(a.creator) != norm(b.creator)`, demote to `escalate`.

```python
def _author_mismatch(a: WorkEmbeddingInput, b: WorkEmbeddingInput) -> bool:
    if not (a.creator and b.creator):
        return False  # one-sided: don't second-guess M5
    return _norm_author(a.creator) != _norm_author(b.creator)
```

`_norm_author` is the same NFKD-strip + casefold + non-alphanumeric
strip already used by the audit script (handles "Le Carré, John" ≡
"Le Carre, John", "Eco, Umberto" ≡ "ECO, UMBERTO"). Lifts to the
shared `src/bffi_pipeline/text/normalize.py` module.

The one-sided case (one record has a creator, the other doesn't) is
*not* vetoed — that's the anonymous-work merge path covered by
prop-05.

### Phases

**A.1 Lift `_norm_author` to `src/bffi_pipeline/text/normalize.py`.**
~10 lines + tests.

**A.2 Add the veto** at the auto-merge decision point. ~5 lines.
Configurable via `BFFI_M5_AUTHOR_VETO=0` for rollback.

**A.3 Re-bench.** The 2 audit rows must escalate. Spot-check 5
post-veto same_work decisions to confirm no over-escalation on
co-authorship records (a Work with 700-field secondary creators
should not be affected because `creator:` only carries the primary
contribution).

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (graduated 2026-05-14; see ../in-progress/), and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; until the observability surfaces are verified non-misleading, downstream work that consumes bench numbers is faith-based. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- 2026-05-13 audit baseline.
- No interaction with prop-20 / prop-22 / prop-23 — the four vetoes
  short-circuit on disjoint conditions and can ship independently.

## Risks

- **R1 — Diacritic / case variants of the same author.** "Le Carré,
  John" vs "Le Carre, John" must collapse. Mitigated by NFKD + casefold
  + non-alphanumeric strip; the audit script has been using exactly
  this normaliser and produced zero false positives on the bench.
- **R2 — Truncated / aliased forms.** "Schubert, F." vs "Schubert,
  Franz" would mismatch. Catalogue conventions strongly prefer the
  full form; if this surfaces, fall back to last-name comparison
  before vetoing. Defer until observed.
- **R3 — `creator:` masks co-authorship.** If two records list a
  different *primary* contributor for what is actually the same
  multi-author Work, this veto correctly escalates (which is what M6
  is for).

## Open questions

- Should the veto run on the *raw* creator string (matching how M5's
  embedder sees it) or on the normalised form (matching the audit)?
  Normalised — diacritic/case variants of the same person must not
  trigger the veto.
- Co-authorship: should we compare against the *set* of primary
  contributors on each record rather than just the first one? Probably
  not in this proposal — M3 currently picks the first primary
  contribution, and that's what M5 embeds; we should veto on the same
  axis M5 uses. Revisit if cataloguers report missed co-author
  merges.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `_norm_author` lives in `src/bffi_pipeline/text/normalize.py`;
      audit script imports from there.
- [ ] M5 auto-merge band demotes to `escalate` when both records have
      a `creator:` field AND the normalised forms differ.
- [ ] Re-bench: the 2 `different_authors_collapsed` audit rows
      escalate.
- [ ] Spot-check 5 same-author re-edition rows; all continue to
      auto-merge.

## What this proposal does NOT do

- Doesn't touch M3 contribution extraction.
- Doesn't propose author disambiguation against KANTO/VIAF (that's
  M9's territory).
- Doesn't change M8 union-find — vetoed pairs go through M6 like any
  other escalated pair.
