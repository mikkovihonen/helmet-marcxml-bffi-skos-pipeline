# P-05 — Canonicalising anonymous + secondary-creator-only Works at M8

**Status**: proposed.
**Scope**: medium. ~3–5 days for the MVP described under Approach,
plus cataloguer review on the URI-minting policy. Not a blocker for
P-02 / P-03 / P-04; can ship independently after the policy lands.
**Proposal-base commit**: `d1fbd32`. To gauge drift before acting,
run `git diff d1fbd32..HEAD -- src/bffi_pipeline/stages/merge.py
src/bffi_pipeline/uris.py docs/marcxml-to-bffi-skosmos-pipeline.md`
(the spec is archived but the contract this proposal modifies is
still defined there in § 5).

## Motivation

Concrete incident — preview-373 corpus (Sierra-exported 373 records),
2026-05-11 23:47 EEST: M8 → M9 → skosify → load completed cleanly
end-to-end, but **Skosmos surfaces only 7 canonical Works out of
372 input Works**. The other 365 unique work URIs went to
`canonical-conflicts.jsonl` (310 entries, since one entry can cover
a same-work cluster of >1 URI) and never reached the published RDF.

The 7-vs-372 gap is not a code bug. It's the current M8 contract at
`src/bffi_pipeline/stages/merge.py:1151`:

```python
if not anchor.creator_uri or not anchor.pref_label:
    # Without a stable (creator_uri, pref_label) pair we can't mint
    # a canonical URI — flag the group as a one-off conflict so
    # M9 / human review surfaces it.
    conflicts.append(...)
    continue
```

`anchor.creator_uri` is populated from `_primary_agent_uri`, which
walks `bffi:contribution → bffi:PrimaryContribution → bffi:agent`
and returns the first agent URI. The implication of the filter:

- Records with no MARC 100/110 main-entry author (anonymous works,
  compilations, serials, music collections, added-entry-only
  records) have no `bffi:PrimaryContribution`, so `creator_uri` is
  `None` and the work falls through to conflicts.
- Records with a MARC 100/110 but a non-URI agent value (rare but
  possible from marc2bibframe2 quirks) hit the same path.
- Records with a primary creator but no `skos:prefLabel` (a title
  was indexed but no label survived the M3 cascade) also fall
  through.

On a curated cataloguer-vetted sample (e.g. the 13-record `Ask 1`
dev set), almost every record has a clean MARC 100 → primary agent
URI → pref label chain, so the filter is invisible. On a random
Helmet sample the rate of records that lack a primary author is
high — anonymous fiction, edited collections, sheet music, atlases,
periodicals, government publications. The preview-373 corpus
demonstrates the worst case: ~98 % of input works are held back.

The current behaviour has a defensible reading: "we won't mint a
stable URI for a Work whose creator-anchor we cannot identify, so
that future merges / re-runs / human review don't end up with
URI churn or duplicate canonicals". But the consequence — those
records being invisible in Skosmos — is the wrong default for the
800 k-record production corpus, where a meaningful fraction of
records is anonymous-but-real. Cataloguers expect to see those Works
in the browsable graph, just without the same merge confidence as
the authored ones.

## Empirical evidence: preview-373 conflict shape

Conflicts file structure on preview-373:

- 310 entries, covering 365 unique work URIs.
- All entries carry `reason: null`. The `GroupConflict` dataclass
  has no `reason` field set by the missing-creator path — only by
  the different-work-edge-inside-same-work-cluster path (which is
  the original intended use of the conflicts file). The two cases
  are currently indistinguishable in the JSONL.
- The 7 canonicals that did land all carry both a URI agent and a
  prefLabel; spot-checks confirm these are the curated, MARC-100-
  bearing records from the random sample.

So at minimum: the `canonical-conflicts.jsonl` format should
distinguish "real conflicts" (same-work edges contradicted by a
different-work edge — needs cataloguer adjudication) from
"missing-anchor" cases (just lacks a stable creator → can be
canonicalised under a fallback policy without human input).

## Approach

The decision space is two orthogonal questions:

1. **What URI-minting strategy applies when a primary creator URI
   is absent?**
2. **What canonical-graph shape applies when the anchor is
   absent?** (Same as authored case, or visually marked as
   anonymous?)

Three options, increasing in ambition. Each is independently
shippable.

### Option A — Fallback URI from title + content + date (~2 days)

Keep the current MARC-100-bearing-records-only contract for
authored Works, but for the missing-creator case, mint a canonical
URI via a deterministic SHA-1 of `(pref_label, content_type,
origin_date)`. Reuses the same `uris.py:mint_work_uri` mechanism
with a different input tuple; produces stable URIs that survive
re-runs and corpus drift.

Rationale: anonymous Works are still real intellectual works with
stable titles. Two records of `Beowulf` (anonymous, 8th-century
Old English, prose) should mint the same canonical URI under this
policy. Records with neither a title nor an author become a
true outlier and stay in the conflicts file as
`reason: "no-anchor"`.

```python
# Sketch in merge.py:
if anchor.creator_uri and anchor.pref_label:
    canonical_uri = mint_work_uri(anchor.creator_uri, anchor.pref_label)
elif anchor.pref_label:
    canonical_uri = mint_anonymous_work_uri(
        pref_label=anchor.pref_label,
        content_type=anchor.content_type,
        origin_date=anchor.origin_date,
    )
else:
    conflicts.append(GroupConflict(..., reason="no-anchor"))
    continue
```

Risk: a popular anonymous title (e.g. *Suomen lait*) may legitimately
exist in multiple distinct content-type / date variants but
otherwise look identical. Tuple includes content + date to mitigate;
genuine clashes still surface through M6's same/different-work
edges if the embedding ever colocates them.

### Option B — Title-only fallback + visible "anonymous" marker (~3 days)

Option A's URI policy plus a graph-shape change: canonical Works
minted from the anonymous fallback path carry a `bffi:adminMetadata`
flag (or a `bffi-prov:anchorType="title-only"` triple) so Skosmos
can render an "anonymous / compiled" badge alongside them. Lets
cataloguers tell at a glance which Works were canonicalised under
the relaxed contract.

Open question: which predicate. `bffi:adminMetadata` already exists
and is the right hook; we'd need a new property under the bffi-prov
namespace (`bffi-prov:anchorType` with values like `primary-creator`,
`title-only`, `secondary-creator-fallback`).

### Option C — Promote primary-but-secondary-only contributions (~4 days)

For records that have `bffi:Contribution` (non-primary) entries with
URI agents but no `bffi:PrimaryContribution`, promote the first
secondary contribution to anchor status under a documented rule.

The natural rule: when MARC 100/110 is empty but MARC 700/710
exists, use the first 700/710 agent. This matches RDA's
"first-named creator" fallback for unascertained primary
responsibility. Combined with Option A: still uses anonymous-URI
minting when there's no contribution at all.

Risk: MARC 700 holds *added entries*, which in practice include
translators, illustrators, editors, performers. Without curation,
promoting them to "primary creator" muddies the canonical-URI
contract. Mitigation: only promote 700s with `bf:Role`s in a
documented allowlist (author, composer, artist, dirigentti,
etc.) — reject editor / translator / illustrator. That allowlist
is a cataloguer-side decision.

## Prerequisites

- Cataloguer sign-off on the URI-minting policy. Pro-bono / NLF
  contribution context: the choice of `mint_anonymous_work_uri`
  inputs becomes a committed identifier; once data is published
  to a Skosmos that downstream tools rely on, changing it later
  breaks links.
- A 1-page write-up of the policy to share with Helmet cataloguers
  during the Ask 1 / Ask 4 follow-up cycle.
- Decision on whether Option A's fallback applies when ANY work
  in the cluster has a primary creator (use that creator), or only
  when ALL works lack one (use the anonymous path). Document this.

## Risks

- **URI churn on policy revision.** If Option A's tuple changes
  between releases (e.g. add language as a fourth field), every
  anonymous canonical URI changes. Treat the tuple as a committed
  identifier on par with `BFFI_WORK_NAMESPACE`.
- **False merges across genuinely-distinct anonymous works.**
  Generic titles (*Tutkielmia*, *Runot*, *Selected works*) without
  a creator can collide. The content_type + origin_date qualifiers
  reduce this but don't eliminate it. M6's same/different signal
  still applies; the SHA-1 collision risk is the new failure mode
  to monitor.
- **Cataloguer expectation drift.** If Option A ships before the
  policy doc, cataloguers may see "phantom Works" in Skosmos
  whose URIs they don't recognise. Sequencing: doc → review →
  ship, not the reverse.

## Open questions

- Does the canonical-conflicts format need a `reason` enum
  (`different-work-edge-inside-cluster` vs. `no-anchor` vs.
  `no-primary-creator`) regardless of which of A/B/C ships?
  Probably yes — it's a 5-line fix and unblocks human-review
  tooling.
- Should the anonymous-URI path also synthesise a placeholder
  `bffi:Contribution` (e.g. with `bffi:agent` pointing at a
  `bffi:UnspecifiedAgent` resource) so the graph shape stays
  uniform downstream? Probably yes, but the M9 reconciliation
  step needs to know to skip authority lookups for those.
- Numbering note: this is the second proposal at index `05`
  (`prop-05` exists in plans as `p-05-m3-cascade-follow-ups.md`);
  the two are disjoint topic areas — disambiguate by path in prose
  if they ever coexist in the same discussion.

## Cross-references

- `src/bffi_pipeline/stages/merge.py:1151` — the filter under
  consideration.
- `src/bffi_pipeline/stages/merge.py:337-344` — `_primary_agent_uri`,
  the function that determines `creator_uri`.
- `src/bffi_pipeline/uris.py` — where `mint_anonymous_work_uri`
  would live.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 5 —
  archived spec section defining the canonical-URI contract.
- `CLAUDE.md` § "Committed identifiers" — where the policy
  documentation would land once approved.
