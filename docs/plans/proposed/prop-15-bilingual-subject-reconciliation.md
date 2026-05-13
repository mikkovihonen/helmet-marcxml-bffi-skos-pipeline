# P-15 — Bilingual subject reconciliation: collapse `allars` + `yso` (and `slm/fin` + `slm/swe`) duplicates

**Status**: proposed.
**Scope**: 2-4 days (M9 subject-reconcile logic + Finto cross-walk + audit).
**Proposal-base commit**: `7ba3587`. To gauge drift before acting, run
`git diff 7ba3587..HEAD --
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/stages/local_concept_resolver.py
data/finto-dumps/`.

## Motivation

The 2026-05-13 cataloguer-feedback audit ([`docs/performance/2026-05-13-cataloguer-feedback-audit.md`](../../performance/2026-05-13-cataloguer-feedback-audit.md)) confirmed a long-suspected M9 bug: when a Helmet bib record carries the same subject concept in both Finnish (`yso/fin`) and Swedish (`yso/swe`) — a normal cataloguing pattern in Helmet's bilingual Finnish-Swedish service area — M9 reconciles them to **two different authority URIs** rather than collapsing them to one.

Concrete evidence from the audit (`b26322791`):

| Source literal | Source vocab tag | M9-bound URI |
|---|---|---|
| `Italia` (Finnish) | yso | `yso:p105111` |
| `Italien` (Swedish) | yso/swe → routed to `allars` | `allars:Y30493` |

Both URIs *refer to the same concept* (the country Italy). YSO is the multilingual concept scheme with prefLabels in Finnish, Swedish, and English on each concept; `allars` (Allmän finlandssvensk ämnesordsregister) is the Swedish-language *companion* vocabulary kept in parallel for historical reasons. Finto's data model crosswalks `allars` to `yso` via `skos:exactMatch`, but the M9 pipeline today reconciles each language's subject literal independently against its source-language authority and never collapses the resulting URIs.

The same shape applies to **SLM** (Suomalainen lajityyppi- ja muotosanasto, Finnish genre/form vocabulary): `slm/fin` and `slm/swe` are the same parallel construct.

Downstream impact on Skosmos and SPARQL queries:

- A search for "subjects in Helmet using the Italy concept" misses any record that bound to `allars:Y30493` because the query targets `yso:p105111` only (or vice-versa).
- The same canonical Work appears to have *two distinct subjects* when really it has one. Aggregation queries double-count.
- Cataloguer expectation per the 2026-05-13 feedback: **one URI per concept**, regardless of how many language forms the record carries.

## Approach

Three layers of fix, **independently shippable** so each can be measured on its own merits:

### Layer A — Cross-walk normalisation at reconcile time

When M9's reconcile step binds a literal to an `allars` URI (or any concept-scheme URI that has a documented `skos:exactMatch` to a YSO URI in the loaded Finto dumps), it should:

1. Look up the `skos:exactMatch` neighbour during the M9 result-finalisation step (before the canonical-graph mutation).
2. Prefer the YSO URI as the canonical bind; record the `allars` URI in `bffi-prov:wasInfluencedBy` so the cataloguer-side audit trail is preserved.
3. Emit `bffi-prov:stage = "reconciliation-crosswalked"` (new enum value, plus matching `STAGE_*` constant in `judge.py` per CLAUDE.md convention) so the dashboard and downstream queries can tell crosswalk-promoted binds apart from direct YSO matches.

Implementation surface: ~80 lines in `reconcile.py` + the resolver, plus a Finto cross-walk index built at vocab-load time (a small JSON sidecar under `data/finto-dumps/` mapping `allars-uri → yso-uri`, populated by walking `skos:exactMatch` triples in the loaded `allars` graph).

### Layer B — Subject-deduplication pass after M9

After Layer A normalises bilingual binds to YSO, the canonical Work may still carry duplicate subject triples (one from the Finnish literal, one from the Swedish — both now pointing at `yso:p105111`). A small dedupe pass (~20 lines in M8 or the M9 result-merge step) collapses these:

```python
# Before: 2 triples
<work> dct:subject yso:p105111 .  # from "Italia"
<work> dct:subject yso:p105111 .  # from "Italien" (after crosswalk)

# After: 1 triple, both source literals recorded in provenance
<work> dct:subject yso:p105111 .
<activity-fi> bffi-prov:inputLiteral "Italia" ; bffi-prov:resultUri yso:p105111 .
<activity-sv> bffi-prov:inputLiteral "Italien" ; bffi-prov:resultUri yso:p105111 ;
              bffi-prov:wasInfluencedBy <allars:Y30493-original-pick> .
```

The provenance graph keeps both activities so the bilingual cataloguer history is auditable.

### Layer C — Same treatment for `slm/fin` + `slm/swe` (and any future bilingual companion vocabularies)

SLM follows the same parallel-companion pattern. Layer A's cross-walk lookup is vocabulary-agnostic — it walks `skos:exactMatch` in any loaded Finto dump — so SLM is covered by the same code path the moment its dumps are present in `data/finto-dumps/`. No additional logic, just verification that the SLM graphs carry the `skos:exactMatch` triples we expect.

Layer C's deliverable is therefore an **audit + regression test** on an SLM record (the cataloguer flagged `b26346564` — but its subjects didn't materialise in the 2026-05-13 audit; pin down whether the cause is shape rejection at M3 or actually-missing source data, and only then test Layer C end-to-end).

## Prerequisites

- Phase A audit step ([P-14 Phase A](../backlog/p-14-m9-phase-c-audit.md)) sets a precedent for the audit JSONL format. P-15's eventual audit JSONL slots into the same `gold/` convention.
- The `b26322791` audit row in [`gold/cataloguer-feedback-2026-05-13.jsonl`](../../../gold/cataloguer-feedback-2026-05-13.jsonl) becomes a regression-test fixture for Layer A — assert that running M9 on that record produces `dct:subject yso:p105111` and not `allars:Y30493` after the fix.
- Layer C's slm follow-up needs the b26346564 missing-subject investigation (separate small task) to land first.

## Risks

- **R1 — crosswalk staleness.** `skos:exactMatch` triples in Finto can drift over time as Finto rebuilds. Mitigation: the crosswalk JSON sidecar's freshness is keyed on Finto SHA per-vocab (same pattern P-10 Phase B uses for the picker cache), so a stale crosswalk auto-invalidates when the operator next runs `bffi-pipeline load-finto`.
- **R2 — silent drop of valuable URI namespacing.** Some downstream tooling (Skosmos, archival workflows) may *expect* both `allars` and `yso` URIs on records. Mitigation: the provenance graph keeps the pre-crosswalk URI in `bffi-prov:wasInfluencedBy`; a SPARQL view can reconstruct the original bilingual bind if needed.
- **R3 — false crosswalks.** Not every `skos:exactMatch` in Finto is symmetric; in rare cases an `allars` concept may be slightly broader/narrower than its YSO counterpart. Mitigation: Layer A only walks the relation in one direction (`allars → yso` for finding the canonical bind), and the cataloguer audit (200-sample, gated on the same P-14 audit infrastructure) verifies no false-merge.
- **R4 — performance impact on M9 Phase 3.** A `skos:exactMatch` lookup per resolved YSO/allars bind adds an in-memory dict hit per entity. On the 5 k bench that's maybe 1500-3000 extra dict accesses, sub-millisecond aggregate. No measurable impact expected.

## Open questions

- Should the crosswalk also apply when one of the two literals resolves only to `allars`? I.e. record carries `Italien` (sv) but no Finnish form — should M9 promote the `allars` bind to YSO unilaterally? The cataloguer-side expectation is probably yes (the *concept* matters, not the language used to express it in this particular record), but it changes the user-visible URI for monolingual-Swedish records. Worth confirming with cataloguers before shipping.
- Are there bilingual-companion vocabularies *other than* `allars` and `slm/swe` that need the same treatment? `kauno` / `bella`? Search the Finto dumps for `skos:exactMatch` predicates to enumerate.
- How does the crosswalk interact with P-14 Phase C's tier-0 expansion? Tier-0 normalisation might produce a tier-0 hit on the Swedish literal that lands directly on `yso` (multilingual prefLabels). In that case the crosswalk is a no-op and Layer A's overhead is wasted on records that already routed correctly. Worth re-evaluating Layer A after P-14 Phase C lands.

## Acceptance criteria (drafted; refine on graduation)

- [ ] Layer A: `b26322791` audit re-run binds `Italia` and `Italien` both to `yso:p105111`. Provenance carries the original `allars:Y30493` reference for the Swedish literal.
- [ ] Layer B: the canonical Work for `b26322791` has exactly one `dct:subject yso:p105111` triple, not two.
- [ ] Layer C: at least one SLM bilingual record (TBD which — depends on the missing-subject investigation) passes the same single-URI test.
- [ ] 5 k re-bench with Layer A on shows no measurable picker-phase wall regression (P-10's wall-time gates are unaffected).
- [ ] 200-sample cataloguer audit (or a focused 50-sample subset) shows zero false-crosswalk binds.
