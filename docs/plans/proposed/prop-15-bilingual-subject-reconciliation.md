# P-15 — Preserve cataloguer-supplied authority URIs through M3, so bilingual subjects don't re-reconcile to different URIs

**Status**: proposed.
**Scope**: 1-2 days (M3 SPARQL CONSTRUCT change + tests + small audit re-run).
**Proposal-base commit**: `1082d32`. To gauge drift before acting, run
`git diff 1082d32..HEAD --
sparql/
src/bffi_pipeline/stages/bf_to_bffi.py
src/bffi_pipeline/stages/reconcile.py`.

## Motivation

The 2026-05-13 cataloguer-feedback audit ([`docs/performance/2026-05-13-cataloguer-feedback-audit.md`](../../performance/2026-05-13-cataloguer-feedback-audit.md)) flagged a bilingual-subject bug on `b26322791`: the record carries both `Italia` (Finnish) and `Italien` (Swedish) for the same concept, and the pipeline binds them to two different URIs (`yso:p105111` and `allars:Y30493`).

The first cut of this proposal (commit `a7baef5`) diagnosed this as an M9 design gap and proposed a three-layer fix (`skos:exactMatch` crosswalk at reconcile, post-M8 dedupe, slm-fin/slm-swe handling). A deeper trace through `scratchpad/data-cataloguer-audit-2026-05-13/` (commit `1082d32`) showed the diagnosis was wrong: **M9 is doing exactly what its inputs ask it to do — the inputs are wrong.**

The actual root cause, traced step by step:

### Source MARCXML (correct)

Cataloguer-supplied `$650` and `$651` fields carry the authority URI in `$0`:

```xml
<datafield tag="651" ind2="7">
  <subfield code="a">Italia</subfield>
  <subfield code="2">yso/fin</subfield>
  <subfield code="0">http://www.yso.fi/onto/yso/p105111</subfield>
</datafield>
<datafield tag="651" ind2="7">
  <subfield code="a">Italien</subfield>
  <subfield code="2">yso/swe</subfield>
  <subfield code="0">http://www.yso.fi/onto/yso/p105111</subfield>  <!-- same URI! -->
</datafield>
```

The cataloguer did the right thing: both language forms tag the same `yso:p105111` in `$0`.

### M2 BIBFRAME (preserves the URI, but stores it differently for `bf:Topic` vs `bf:Place`)

For `650` (topical subjects), marc2bibframe2 emits `<bf:Topic rdf:about="<yso-uri>">`. The bf:Topic IS the YSO URI. M9 has nothing to reconcile because the binding is already explicit.

For `651` (geographic subjects), marc2bibframe2 emits a `<bf:Place rdf:about="urn:...raw/b26322791#Place651-54">` — a per-record local URI — with `madsrdf:isIdentifiedByAuthority rdf:resource="<yso-uri>"` as a separate triple. The YSO URI is preserved but as a cross-reference, not as the entity's identity.

The same `651`-vs-`650` divergence applies to `600` (personal name subjects), `610` (corporate name subjects), and `611` (meeting name subjects), all of which marc2bibframe2 emits with per-record local URIs plus a `madsrdf:isIdentifiedByAuthority` link.

### M3 BFFI (drops the cross-reference)

The M3 SPARQL CONSTRUCT pair (`sparql/m3-*.rq`) emits `bffi:subject` triples pointing at the BIBFRAME entity's `rdf:about` URI. For `bf:Topic` that's the YSO URI (good). For `bf:Place` / `bf:Person` / `bf:Organization` / `bf:Meeting` that's the per-record raw URI. **The `madsrdf:isIdentifiedByAuthority` link is not followed.**

So the canonical BFFI graph carries:

```turtle
<bffi:Work-b26322791> bffi:subject
    <http://urn.fi/URN:NBN:fi:bib:raw/b26322791#Place651-54> ,  # Italien
    <http://urn.fi/URN:NBN:fi:bib:raw/b26322791#Place651-55> ,  # Italia
    <http://www.yso.fi/onto/yso/p27506> ,                       # idrottshistoria (was a bf:Topic, kept as yso URI)
    ...
```

### M9 reconcile (re-reconciles the per-record URIs from scratch)

M9 sees `<...#Place651-54>` and `<...#Place651-55>` as un-reconciled local URIs whose `rdfs:label` is `Italien` and `Italia` respectively. With the cataloguer-supplied yso URI already lost upstream, M9 has no choice but to reconcile from the literal label. The Finnish "Italia" tier-0-hits `yso:p105111` cleanly; the Swedish "Italien" tier-0-misses YSO (no Swedish `prefLabel "Italien"` in the loaded YSO dump because the Swedish-language YSO entries live in a separate `skos:altLabel` predicate) and falls through to `allars`, where the lexical match wins on `Y30493`. End result: two URIs for one cataloguer-supplied concept, exactly as observed.

**Crucially**: M9 is not buggy. It's reconciling labels it shouldn't be reconciling at all. The cataloguer's `$0` URI was meant to short-circuit reconciliation entirely.

## Approach

A single small change at the M3 layer fixes this for all five 6XX subject kinds (`600`, `610`, `611`, `650`, `651`). Two additional follow-ups address dedupe and the slm parallel.

### Step 1 — M3: prefer `madsrdf:isIdentifiedByAuthority` over per-record raw URI

When the M3 SPARQL CONSTRUCT generates `bffi:subject` triples, if the BIBFRAME entity has a `madsrdf:isIdentifiedByAuthority <authority-uri>` triple, the canonical subject URI is the authority URI, not the per-record raw URI.

This is one CONSTRUCT branch (and probably one Jinja-templated conditional) in `sparql/m3-bf-to-bffi.rq` (or whichever file produces the bffi:subject mapping today; verify on entry).

Effect:
- Cataloguer-supplied URIs propagate through to canonical without losing identity.
- M9 sees them as already-bound and skips reconcile (the no-op path for entities that arrive at M9 already wearing an authority URI).
- The dual-URI bug observed on `b26322791` resolves to a single `yso:p105111` bind.

### Step 2 — M8 / M3 subject dedupe (small, defensive)

Even after Step 1, the canonical graph carries two `bffi:subject <yso:p105111>` triples (one from `Italia`, one from `Italien` — same predicate, same object, same subject Work). RDF semantics already deduplicate (triples are sets), so this is technically a no-op — but the provenance graph still has two `bffi-prov:Reconciliation` activities, which is fine and arguably useful (per-language provenance).

Confirm no double-counting in downstream queries; no code change needed unless a count-aggregation surfaces a bug.

### Step 3 — Verify `slm` follows the same shape

The 2026-05-13 audit also flagged `slm/fin` + `slm/swe` on `b26346564`. The genre/form vocab uses MARC `$655` (Index Term — Genre/Form) which marc2bibframe2 emits as `bf:Genre`. Same code path question: does marc2bibframe2 emit `bf:Genre rdf:about=<yso-uri>` or `bf:Genre rdf:about=<raw-uri>` + `madsrdf:isIdentifiedByAuthority`? Once Step 1 lands, this is verified by running the audit re-run against `b26346564` and checking that the slm subject (if it materialises) carries the cataloguer's `$0` URI.

The b26346564 audit currently doesn't show any slm subject at all — needs the missing-subject investigation that this proposal isn't trying to solve. If Step 1 lands and `b26346564` still shows no slm subject, that's the genuine missing-subject issue surfaced by the audit and goes to its own follow-up.

## Prerequisites

- The audit fixture from the 2026-05-13 cataloguer feedback is the regression test surface. Specifically `b26322791` (yso/fin + yso/swe co-occurrence with `$651` Places) is the canonical "this should now bind to one yso URI" test case.
- The M3 SPARQL CONSTRUCTs are versioned in `sparql/`. Step 1 is a CONSTRUCT change, not Python — touch the SPARQL file directly per the project's "SPARQL in versioned files" convention.

## Risks

- **R1 — different `bf:Place` / `bf:Topic` shape in non-Helmet BIBFRAME.** The fix relies on marc2bibframe2's specific `madsrdf:isIdentifiedByAuthority` emission. If another exporter produces BIBFRAME with the authority URI in a different predicate (e.g. `bf:identifiedBy [ rdf:value "<uri>" ]`), Step 1 misses it. Mitigation: scan the corpus (or a representative slice) for `madsrdf:isIdentifiedByAuthority` predicate coverage before declaring the fix complete.
- **R2 — false-positive promotions on stale `$0`.** If the cataloguer-supplied `$0` URI points at a defunct or renumbered YSO concept, Step 1 propagates the bad URI through and M9 skips its corrective lookup. Mitigation: the existing `bffi-prov` graph records the propagation; cataloguer-side audits surface staleness. The pre-Step-1 behaviour (re-reconcile from literal) effectively trusts the literal over the `$0` URI, which is a different trade-off — accepting Step 1 means trusting the cataloguer's `$0` choice over the reconciler's literal-based lookup.
- **R3 — preserves URIs even when they're wrong by design.** Some cataloguers intentionally point `$0` at an `allars` URI (rather than the parallel YSO one) to keep the Swedish-vocabulary chain explicit. Step 1 honours that intent (since it just preserves whatever `$0` says); if the policy is "always promote allars → yso," that's a separate question and should be handled by the `skos:exactMatch` crosswalk originally proposed — but as a *post-M3* normalisation rather than as the primary fix.

## Acceptance criteria

- [ ] M3 SPARQL CONSTRUCT change preserves `madsrdf:isIdentifiedByAuthority` URIs for all 5 6XX kinds (600/610/611/650/651). Pinned by unit tests with synthetic BIBFRAME fixtures: one fixture per kind, asserting that `bffi:subject <authority-uri>` lands in the M3 output.
- [ ] `b26322791` audit re-run: the canonical Work has exactly one `bffi:subject <yso:p105111>` triple (RDF-semantically dedupe of the two source language forms).
- [ ] M9 provenance on the re-run shows **zero** reconciliation activities for `b26322791`'s Italia / Italien literals (both arrive at M9 pre-bound).
- [ ] The wider 19-record audit re-run preserves all other M9 outcomes — no regression in the 17/19 cataloguer-matching column.

## Open questions

- Does the M3 SPARQL CONSTRUCT change need any backward-compatibility hedge for pre-existing canonical Turtles that carry `<...raw/...#Place651-XX>` URIs? Probably no: M3 is idempotent and re-running it overwrites the BFFI Turtle. Skosmos won't see the old raw URIs after the next `load`.
- Should the `madsrdf:isIdentifiedByAuthority` fallback walk be wider than 6XX? `bf:Agent` (creator URIs) follow the same pattern; marc2bibframe2 emits per-record raw URIs for `bf:Agent` and links the asteri / KANTO authority via `madsrdf:isIdentifiedByAuthority`. The same M3 change could collapse these to the asteri URI directly, removing M9 reconciliation work for any record that arrives with `$0` populated on its `1XX` / `7XX` contributor fields. Worth confirming with a small audit before promoting.

## What this proposal supersedes

- The `skos:exactMatch` crosswalk approach in the first cut of this proposal (commit `a7baef5`) was over-engineered: it tried to fix at M9 what should be fixed at M3. The crosswalk has standalone value (allars → yso normalisation for records that *don't* carry a `$0` URI on the Swedish entry), but that's a much smaller residual case that doesn't need its own plan until evidence demands it.
