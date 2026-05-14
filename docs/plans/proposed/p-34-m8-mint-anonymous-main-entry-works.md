# P-34 — M8 canonical-Work mint for anonymous-main-entry records

**Status**: proposed.
**Scope**: half a day for sub-option (1) (first-contributor anchor); 1-2 days for sub-option (2) (cataloguer rule table) including the cataloguer engagement.

**Proposal-base commit**: `16a0007`. The "Motivation" reasons about M8 immediately after the mint-failure / conflict split (Bug A fix, commit `16a0007`). If `main` moves before this is acted on, re-verify with:

```
git diff 16a0007..HEAD -- \
    src/bffi_pipeline/stages/merge.py \
    src/bffi_pipeline/uris.py \
    sparql/bf_to_bffi_work.rq \
    sparql/bf_to_bffi_expression.rq
```

Cross-references:
- **Bug A fix at `16a0007`** ("M8: split mint failures from canonical conflicts") — separated the *reporting* of the failure mode. P-34 addresses the underlying *mint capability*. Reading the Bug A commit message first gives the context for why this proposal exists.
- **P-33** (`p-33-m3-manifestation-and-item-construct.md`) — also touches M3 → BFFI surface but at the Manifestation/Item layer. P-34 stays at the Work layer where the canonical mint key lives.

## Motivation

The 2026-05-14 helmet-5k-full bench surfaced that **707 / 4906 records (~14%) of the sample never make it into the canonical Works graph**. The Bug A fix at `16a0007` correctly classified these as "mint failures" (not conflicts) and routed them to `canonical-mint-failures.jsonl` so cataloguer review of real M6 contradictions stays clean.

But the underlying capability gap remains: M8 can't mint canonical Work URIs for records that lack a `bffi:PrimaryContribution`. Downstream consequences:

1. **No canonical Work** → no `bffi:adminMetadata` block, no aggregation of identifiers from absorbed siblings, no participation in M8's same/different merge logic.
2. **M9 reconcile never sees them** — the M9 walker iterates canonical Works only. Subjects, contributions, expression metadata in `bffi/<bib_id>.ttl` get extracted but never reconciled.
3. **Skosmos doesn't render them** — the SKOSified output is canonical-Work-driven; without a canonical, no Skosmos concept page.
4. **The pipeline silently loses ~14% of the corpus** on a Helmet-typical input.

Inspection of 5 random mint-failure records (b20363308 "Hanko toisessa maailmansodassa", b10018086 "The Afro-Arabian crossroad", b10407303 "Industrisamhälle och arbetarkultur", b10750897 "Old English organ music for manuals", b25432606) showed:

- MARC 100/110/111 (primary creator) is **absent** on all five.
- MARC 245 ind1 = **0** ("no main-entry under person/title" — the title IS the main entry).
- MARC 700 (added entries / contributors) carries the editor(s), typically with `$e toimittaja` (Finnish) or `$e editor`.
- The records are **edited compilations / anonymous works / anthologies** — a legitimate Finnish cataloguing pattern.

M3's BFFI extraction is correct: it emits `bffi:Work` + `bffi:Expression` + `bffi:contribution` blocks (one per 700 contributor) + `skos:prefLabel` (the 245 title). The only missing thing is `bffi:PrimaryContribution`, because marc2bibframe2 doesn't emit one when MARC 1XX is absent.

M8's mint key `(creator_uri, pref_label)` (via `bffi_pipeline.uris.mint_work_uri`) needs the primary creator. Without it, the record falls through to the mint-failure path.

This is a **legitimate cataloguing pattern producing a routing failure** in the pipeline — not a data-quality bug. The fix is to extend the mint logic to handle the "no primary creator" case.

## Approach

Three sub-options at increasing depth. Sub-option (1) is the conservative default; (2) is the cataloguer-curated extension; (3) is the deep refactor that's only worth considering if (1) + (2) prove insufficient.

### Sub-option (1) — Editor-anchored mint (recommended default)

When the anchor lacks `creator_uri` BUT has at least one non-primary contribution:

1. Extend `_primary_agent_uri()` (or a new sibling `_first_contributor_uri()`) to walk `bffi:Work → bffi:contribution` (any type, not just `bffi:PrimaryContribution`) when the primary path returns None. Pick the **first contributor in BIBFRAME order** (preserves M2 ordering, which preserves MARC 700 ordering, which is cataloguer-determined).

2. Mint the canonical URI from `(first_contributor_uri, pref_label)`. Tag the resulting canonical Work with a new predicate `bffi:mintAnchor = <bib:auth/editor-anchored>` (or similar) so downstream consumers can distinguish "primary-author-anchored" from "editor-anchored" canonical Works.

3. The 707 records on this sample have at least one MARC 700 contributor each (spot-checked all 5). So sub-option (1) recovers essentially all of them.

**Risk:** two records that are genuinely separate Works but happen to share the same editor + similar title get over-merged. Mitigation: the mint key is already exact-match on `(uri, label)`, so accidental collisions are rare; and M8's same/different M6 decisions still apply on top to fold/split as needed.

**LOC estimate:** ~30 LOC in `_primary_agent_uri` (or new function) + ~10 LOC in `apply_merge` to detect the case + ~30 LOC in M3 output's `bffi:mintAnchor` typing (one-shot extension to `bf_to_bffi_work.rq` plus the post-processor). Plus 3-4 regression tests.

### Sub-option (2) — Cataloguer-curated mint-strategy table

When the anchor lacks `creator_uri` AND lacks any non-primary contribution (truly anonymous record with no MARC 100/110/111 AND no MARC 700/710/711):

1. Fall back to a **title-only mint**: `(NULL, pref_label)` → URI hash. Two truly-anonymous records with identical titles get the same canonical Work (likely correct — they're the same intellectual content).

2. Add a cataloguer-curated lookup table at `config/m8-anonymous-mint-rules.yaml` for special-case Helmet patterns (e.g. annual report series, government publications). Each rule maps a 245-pattern + optional cataloguer-tag to a mint strategy (e.g. "use 264$b publisher as creator surrogate").

**Risk:** unknown until we measure how many records on full-corpus scale lack BOTH 1XX and 7XX. Spot-check on this sample suggests near-zero — every mint-failure record had at least one 700. Worth a corpus-wide query before committing to the complexity.

**Prerequisites:** cataloguer engagement to define the rule shapes. Best done after P-30 (observability-audit gate) clears so we can trust the mint-strategy metrics on the dashboard.

### Sub-option (3) — Mint key refactor (deferred, deep change)

Replace the `(creator_uri, pref_label)` mint key with a richer multi-input hash that gracefully degrades through `(primary, editor, publisher, year, pref_label)`. Requires re-minting every existing canonical URI across the corpus → ~5x M8 wall-time, hours of M9 re-reconcile, full Fuseki replace. Only worth it if (1) + (2) prove insufficient on full corpus.

**Verdict:** start with (1). Layer (2) if needed. Defer (3) unless real evidence forces it.

## What this would change downstream

If sub-option (1) ships:

- `canonical-map.jsonl` grows by ~14% to cover the previously-dropped records.
- Each new canonical Work carries a `bffi:mintAnchor` predicate identifying it as editor-anchored.
- M9 reconcile sees the new records. Their `bffi:contribution` blocks (with editor agents) get reconciled against KANTO.
- Skosmos renders them — editors appear in the contributions list instead of the (empty today) author slot.
- `canonical-mint-failures.jsonl` shrinks to whatever's left after sub-option (1) catches the editor-anchored cases. The remaining records (genuinely no 1XX AND no 7XX) become the input set for sub-option (2).

## Prerequisites

- **Bug A fix at `16a0007` shipped** (already done as of plan-base).
- **Cataloguer sanity-check on a 100-record sample** of mint-failures from a real run — confirm the editor-anchored mint produces canonical Works that match cataloguer expectations. Specifically the "editor / contributor / translator" `$e` role distinction: a translator should NOT typically anchor a canonical Work (the original author is the right anchor, but they're missing from this MARC record); an editor for an anthology should.
- **A new predicate URI** for `bffi:mintAnchor` (or use an existing one if BFFI 1.0.0 already has something fit-for-purpose). Vendored ontology at `docs/lkd.rdf` is the canonical reference. Worth a `grep mintAnchor docs/lkd.rdf` before drafting the plan.

## Risks

- **R1 — Editor-anchored mint over-merges.** Two unrelated edited compilations by the same editor with similar titles (e.g. two volumes of the same series) merge unintentionally. Mitigation: same as today's primary-author-anchored case — M8's same/different M6 decisions apply on top of the mint key, so the union-find layer catches real collisions when M6 produces a `different_work` verdict.

- **R2 — `$e role` semantics are inconsistent across cataloguers.** Helmet records have 13+ years of cataloguer history; the `$e` subfield convention varies. Mitigation: route only well-known editor/compiler/contributor roles to the anchor; unknown `$e` fall to title-only mint.

- **R3 — Translator-only records mis-anchor.** A record with no 1XX but a 700 with `$e kääntäjä` (translator) anchors to the translator, who is intellectually wrong. Mitigation: explicit role-blocklist for translator-only anchoring.

- **R4 — Re-running M8 on existing canonical graphs breaks URI stability.** Records previously in mint-failures will get new canonical URIs. Any downstream consumer (Skosmos, Fuseki graph subscribers, cataloguer cross-references) that pinned the old "no canonical" state would see new URIs appear. Mitigation: standard P-32 pre-run-Fuseki-clear handles this for the Fuseki side; no other long-lived consumer exists today.

- **R5 — Adding `bffi:mintAnchor` to canonical Works changes the BFFI shape.** Downstream SHACL shapes need to allow the new predicate. Mitigation: low-risk SHACL update; `bffi:mintAnchor` is a new optional predicate, not a replacement.

## Open questions

- **Should `bffi:mintAnchor` be a typed value or a literal?** Probably a URI from a small fixed vocab (`bib:auth/primary-author-anchored`, `bib:auth/editor-anchored`, `bib:auth/title-only-anchored`). Lets Skosmos render a meaningful badge per Work and lets M9 audit cross-anchor cardinality.

- **Should records anchored by the first contributor carry that contributor as `bffi:PrimaryContribution` on the canonical?** Two options:
  - (a) Promote the first contributor to `bffi:PrimaryContribution` on the canonical Work only (raw Works keep their original shape). Simpler downstream — M9 sees a uniform primary-contribution slot.
  - (b) Keep the canonical's `bffi:contribution` typed as `bffi:Contribution` (not `Primary`). Honest to the source data — cataloguer didn't designate a primary, neither should we. M9 needs to walk both types.
  Verdict: defer to cataloguer input.

- **Records with NO `bffi:contribution` at all** (truly anonymous, no 1XX AND no 7XX). Spot-check: zero of 707 sample records. But full-corpus may differ. Sub-option (2)'s title-only mint covers these — but should we ship sub-option (1) FIRST and measure how many records sub-option (2) actually needs to cover?

- **`bf:Place`, `bf:Meeting`, `bf:Organization` as anchor candidates.** A subject-anchored mint ("Helsinki, kaupunki" with no creator) is theoretically possible but probably wrong (those records mostly have editors anyway). Worth confirming on the corpus before excluding.

- **Counterpoint — leave it alone.** If cataloguers consider 14% record loss acceptable for a sample run (since these are *anonymous main entries* and arguably shouldn't get a canonical Work URI in the FRBR sense), this proposal stays `proposed` indefinitely. The Bug A fix already made the failure mode visible + queryable; that may be enough. Acid test: does any cataloguer-side consumer (cataloguer-audit JSONL, OPAC integration, hand-off-to-NLF spec) actually need these records in the canonical graph?
