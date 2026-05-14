# P-07 — Reclassify marc2bibframe2-lifted MARC 856 from `bf:Instance` to `bf:Item`

**Status**: proposed.
**Scope**: half a day to 1-2 days (depends on whether we patch in M2 or upstream marc2bibframe2).
**Proposal-base commit**: `549baa0`. The "Motivation" reasons about the
M2 stage immediately after `fix(marc-to-bf): scope shape + post-
processor to the main Instance` worked around the symptom. If `main`
has moved before this is acted on, re-verify with
`git diff 549baa0..HEAD --
src/bffi_pipeline/stages/marc_to_bf.py
config/shapes/bibframe-conversion.shape.ttl
third_party/marc2bibframe2/`.

## Motivation

marc2bibframe2's XSLT lifts every MARC 856 (Electronic Location
and Access) field into a separate `bf:Instance` IRI of the form
`<base>#Instance856-<idx>`, attached to the main Work via a
sibling `bf:hasInstance` triple. The P-02 5 000-record production-
style run turned up 10 records (0.2 %) where this caused the
Boundary-2 SHACL shape to fire on the secondary Instance for
missing Helmet identifier + AdminMetadata; a follow-up trace also
exposed a non-deterministic-ordering bug in `_find_root_resources`
that sometimes stamped Helmet identifier on the secondary Instance
instead of the main one.

`549baa0` works around the symptom at two layers — the M2
post-processor now picks the main Instance deterministically by
URI convention, and the SHACL shape excludes secondaries by URI
regex. The records convert successfully. But the underlying
semantic question is unanswered:

**Is a MARC 856 URL really a separate manifestation (`bf:Instance`),
or is it an access point to an existing manifestation (`bf:Item`
or `bf:electronicLocator` literal)?**

MARC 856 is "Electronic Location and Access". Its content is
typically:

- A publisher / library website URL pointing at the book's PDF
  or web copy (same intellectual content as the print volume).
- A vendor link to buy / borrow.
- A landing page describing the title.

In RDA + BIBFRAME 2 terms, **most of these are item-level
(holding-level) access points, not separate manifestations.** A
separate manifestation would be a record with its own ISBN /
publisher / pub-date — when that's present, MARC 776 (Additional
Physical Form) is the cataloguer's tool, not 856. marc2bibframe2's
"always lift 856 to Instance" policy is therefore an over-
classification for the typical Helmet 856 use case.

Concretely on the failing sample:

| Bib | 856 content | Looks like… |
|---|---|---|
| 1936313 | `virtuaaliviipuri.tamk.fi` project landing | landing page, not manifestation |
| 1987967 | `kansanmusiikki-instituutti.fi/.../KIJ68web.pdf` | same content as print, web-served |
| 2030816 | `kansanmusiikki-instituutti.fi/...` | landing |
| (etc.) | … | mostly access points |

All 10 are access-point / web-copy style, not separate-
manifestation style.

## Approach

Three sub-options, in increasing depth:

1. **M2-stage rewrite** (post-XSLT, pre-shape): walk the
   marc2bibframe2 graph, find every `#Instance<tag>-<idx>`
   resource of `bf:Instance` type, and rewrite it as
   `bf:Item`. Adjust the surrounding `bf:hasInstance` triple from
   the main Work to a `bf:hasItem` triple on the main Instance
   (item-of-instance, not item-of-work). The Boundary-2 shape's
   regex exclusion can then drop the URI-suffix filter — items
   aren't checked, only Works and Instances. Local change, no
   third_party touch.

2. **Configurable case-by-case rewrite**: extend (1) with a
   cataloguer-supplied rule that maps the *content* of the 856
   ($u URL + $3 materials-specified) to a verdict — "this 856 is
   a separate ebook manifestation, keep as Instance" vs "this is
   just a publisher landing page, demote to Item". Allows
   genuine separate ebook editions (with their own ISBN) to
   remain Instances. Configuration likely lives under
   `config/shapes/` or `config/bf-rewrites.yaml`.

3. **Upstream PR to marc2bibframe2**: change the XSLT to emit
   856 as `bf:Item` by default, with a stylesheet parameter to
   opt back to the legacy "always Instance" behaviour. Cleanest
   long-term answer, but the third_party module is a git
   submodule we're explicitly *not* forking
   (`CLAUDE.md`: "Don't modify `third_party/marc2bibframe2/`.
   Wrap, don't fork."), so this would be either an upstream
   contribution to Library of Congress or a soft fork sitting
   parallel to the submodule.

## Prerequisites

- `549baa0`'s URI-regex workaround has shipped, so the pipeline
  is unblocked while we deliberate. P-07 is purely a model-
  quality improvement on top.
- An empirical sample of Helmet 856 usages broad enough to
  validate (1) vs (2). The 5 000-record run gives us 10 / 5 000
  data points. The full 800k corpus would give a much sharper
  picture of how many 856s are *genuinely* separate
  manifestations (have own ISBN, own publisher, etc.) vs the
  access-point / landing-page majority.

## Risks

- **Information loss if we get it wrong**: demoting a genuine
  separate-ebook 856 to a `bf:Item` collapses two manifestations
  into one. Recoverable by re-running the pipeline once the
  classifier is corrected, but the canonical Works graph would
  carry the wrong record-merge for the duration.
- **Skosmos display drift**: Skosmos renders Items differently
  from Instances; cataloguers reviewing existing pages would see
  the e-link reshuffle even when the underlying access pattern
  hasn't changed.
- **Downstream M3 / M8 / M9 routing surprises**: those stages
  were written against marc2bibframe2's current "856 →
  Instance" output; rewriting at the M2 boundary needs M3's
  property routing for `bf:hasItem` + `bf:Item` to be tested.
- **Sub-option (1) blast radius**: rewriting every 856 globally
  is the safest *if* the access-point assumption holds for ~all
  Helmet 856s. (2) is safer but needs cataloguer input on the
  rules.
- **Sub-option (3)**: PR turnaround at the upstream LoC project
  is probably months, and our submodule pin would diverge from
  upstream main in the meantime.

## Open questions

- **What fraction of Helmet 856s are access points vs separate
  manifestations?** The 10-record P-02 sample is too small to
  decide. A `bffi-pipeline 856-audit` CLI (or a simple SPARQL
  query against the full BFFI graph) that classifies every 856
  by content-shape (presence of ISBN, presence of $3 materials-
  specified, etc.) would inform sub-option choice.
- **If we ship (1), do we still want (2) later?** Probably yes
  for the edge cases where Helmet *does* have separate-ebook
  records and a cataloguer would tag them. But (1) might be a
  90-%-fix that pays off immediately.
- **Counterpoint**: the workaround in `549baa0` is sufficient.
  The shape filter excludes secondaries, post_process picks the
  main Instance deterministically, the pipeline runs clean. The
  semantic mis-classification only matters if a downstream
  consumer (Skosmos browser, KOHA importer, NLF hand-off
  validator) starts caring about Instance-vs-Item distinctions
  on 856-derived nodes. If they don't, P-07 stays `proposed`
  indefinitely.
- **Alternative**: keep 856 as `bf:Instance` but mark it with a
  `bffi:secondaryInstance` typing predicate so downstream code
  can filter consistently. Lighter than the full Instance →
  Item rewrite. Not a great fit for the BIBFRAME-purist view of
  the model.

## Cross-references

- [`config/shapes/bibframe-conversion.shape.ttl`](../../config/shapes/bibframe-conversion.shape.ttl)
  — the SHACL target whose URI-regex exclusion P-07 would let us
  delete if 856 → Item lands cleanly.
- [`src/bffi_pipeline/stages/marc_to_bf.py`](../../src/bffi_pipeline/stages/marc_to_bf.py)
  `_find_root_resources` — the deterministic-pick fallback added
  by `549baa0` becomes load-bearing if we keep 856 as Instance,
  but would simplify under P-07.
- `third_party/marc2bibframe2/xsl/` — XSLT we don't modify (per
  `CLAUDE.md`); sub-option (3)'s upstream PR target.
- BIBFRAME 2.5 spec § "Instance vs Item" — the canonical
  semantic distinction this proposal hangs on.
