# P-25 — M5 anthology-vs-specific scope veto

**Status**: proposed.
**Scope**: half-day.
**Proposal-base commit**: `6b6be25`.
**Source data**: `scratchpad/merge-cluster-verdicts/verdicts.jsonl`.

## Motivation

The merge-cluster audit found **2 / 183 clusters (1.1 %)** that mixed
**anthology / collected-works titles with specific component works** —
a FRBR scope mismatch: an anthology is a distinct Work from any of
its constituent Works.

Example pattern (from `scripts/audit-merge-clusters.py:_is_anthology_title`
that the audit detects, then categorises as `different_scope_same_canon`):
- "Complete works of X" + "Symphony No. 5" by X
- "Selected stories of X" + "The lottery" by X
- "Kootut runot" (collected poems) + a single named poem in the same set

Low absolute count but worth ~80 false merges on the 800 k corpus
extrapolation, AND a qualitatively distinct class (the anthology
shouldn't have the same canonical URI as any of its components).

### Root cause

`embedding_input_string` (`embeddings.py:212`) treats "Symphony No. 5"
and "Complete works" as two title strings; if the author and other
fields match, BGE-M3 can land them inside the auto-merge band
(especially for short titles where the embedding has little context
to distinguish).

## Approach

Lift `_is_anthology_title` and the anthology-marker set from
`scripts/audit-merge-clusters.py` into a shared module
(`src/bffi_pipeline/text/scope.py`). At the auto-merge decision
point, demote to `escalate` when *exactly one* of the pair's titles
is anthology-flagged.

```python
ANTHOLOGY_MARKERS = frozenset({
    # English
    "complete", "collected", "selected", "anthology", "works",
    # Swedish
    "samlade", "valda",
    # Finnish
    "kootut", "valitut",
    # French / German
    "œuvres", "gesammelte", "ausgewählte",
})

def _is_anthology_title(t: str) -> bool:
    words = set(re.findall(r"\w+", t.lower()))
    return bool(words & ANTHOLOGY_MARKERS)

def _scope_mismatch(a: WorkEmbeddingInput, b: WorkEmbeddingInput) -> bool:
    return _is_anthology_title(a.title or "") ^ _is_anthology_title(b.title or "")
```

Both-anthology (e.g. two re-issues of "Collected works") and
neither-anthology pairs are *not* vetoed — only the asymmetric case
where one record is a collection and the other is a specific work.

### Phases

**A.1 Lift the scope module.** ~30 lines.

**A.2 Add the veto.** ~5 lines + flag
`BFFI_M5_SCOPE_VETO=0` for rollback.

**A.3 Re-bench.** The 2 audit rows must escalate. Spot-check 5
same-anthology rows (e.g. "Complete works" 1995 vs "Complete works"
2010 re-issue); they must continue to auto-merge.

## Prerequisites

- **Gating prerequisite — observability trustworthiness.** P-17, P-18, and P-19 must be implemented (graduated 2026-05-14; see ../in-progress/), and prop-30 (critical audit of observability + audit-trail practices) must be complete and signed off. The 2026-05-13 bench surfaced a `used_cascade` field misread that nearly drove prop-27 around a false premise; until the observability surfaces are verified non-misleading, downstream work that consumes bench numbers is faith-based. See [`prop-30`](prop-30-observability-audit-trail-critical-audit.md).
- 2026-05-13 audit baseline.

## Risks

- **R1 — Anthology-flagged false positives.** A title like "Selected
  stories" might be the actual single-Work title of a specific
  publication. Mitigation: only veto when the *other* record's title
  lacks anthology markers (asymmetric pattern); legitimate matched
  pairs are safe.
- **R2 — Marker-list coverage drift.** Each new language pair adds
  conventions ("Verzamelde", "Opere complete"). Mitigation: counter
  in M5 observability; extend the set on observed gaps.
- **R3 — M6 wall growth.** Estimated +2 cases on 20 k → +80 on 800 k →
  +4 minutes M6 wall at 3 s/pair. Negligible.

## Open questions

- Should `bf:contentType` or `bf:adminMetadata` carry an anthology
  flag for richer downstream signal? The audit infers from title text
  because that's what M5 already has; promoting to a typed property is
  out of scope for this proposal but could be a follow-up.
- The marker list is short — should it be a configuration file instead
  of a code constant? Probably not yet; defer until ≥ 3 languages need
  separate maintenance.

## Acceptance criteria (drafted; refine on graduation)

- [ ] `src/bffi_pipeline/text/scope.py` exports `is_anthology_title`
      and the marker set. Audit script imports from there.
- [ ] M5 auto-merge band demotes to `escalate` when exactly one of
      the two titles is anthology-flagged.
- [ ] Re-bench: the 2 `different_scope_same_canon` audit rows
      escalate.
- [ ] Same-anthology re-edition regression: spot-audit 5 cases (both
      titles anthology-flagged); all continue to auto-merge.

## What this proposal does NOT do

- Doesn't model the anthology → component relationship in BFFI (no
  `bffi:hasComponent` predicate). That's a vocabulary question
  separate from this veto.
- Doesn't enrich M3's BIBFRAME → BFFI conversion with anthology
  metadata.
- Doesn't change M8 / M9 — vetoed pairs go through M6 like any other
  escalated pair.
