# P-34 — M8 canonical-Work mint for anonymous-main-entry records

**Status**: in-progress (Phase A shipped 2026-05-14; Phase B + Phase C remain in backlog).
**Scope**: Phase A (sub-option 1, editor-anchored fallback): half a day — **done**. Phase B (sub-option 2, title-only / cataloguer rule table): 1-2 days plus cataloguer engagement, gated on P-30. Phase C (sub-option 3, mint-key refactor): deferred; only if Phase A+B prove insufficient.

**Plan-base commit**: `16a0007` (graduated from proposal at this commit). Phase A measurements + verification ran against this baseline.

**Phase commits**:
- Phase A (editor-anchored fallback + `bffi-prov:mintAnchor` predicate + translator-role blocklist + 4 unit tests): `9261dfd` (graduation + code + tests, 2026-05-14).
- Phase B (truly-anonymous title-only mint + cataloguer-curated rule table): `<unfilled>`.
- Phase C (mint-key refactor): `<unfilled>`.

If `main` moves before Phase B is acted on, re-verify with:

```
git diff <plan-base>..HEAD -- \
    src/bffi_pipeline/stages/merge.py \
    src/bffi_pipeline/uris.py \
    src/bffi_pipeline/provenance/vocab.py \
    sparql/bf_to_bffi_work.rq \
    sparql/bf_to_bffi_expression.rq
```

Cross-references:
- **Bug A fix at `16a0007`** ("M8: split mint failures from canonical conflicts") — separated the *reporting* of the failure mode. P-34 Phase A addresses the *mint capability*.
- **P-05 (abandoned 2026-05-14)** —
  [`docs/plans/abandoned/p-05-anonymous-work-canonicalisation.md`](../abandoned/p-05-anonymous-work-canonicalisation.md).
  Same root issue, drafted earlier against the preview-373 incident.
  P-05's three options (A: title-only fallback / B: title + content
  + date / C: cataloguer-tagged anonymous mint) are absorbed into
  P-34's Phase B (backlog). P-05's preview-373 conflict-shape
  evidence is preserved there as the early signal that surfaced
  this class of failure.
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

## Definition of done

Three phases corresponding to the three sub-options from the source proposal.

### Phase A — Editor-anchored mint (shipped 2026-05-14)

- [x] `stages/merge.py:_first_contribution_agent_uri()` walks `Work → bffi:contribution → bffi:agent` AND `Work → hasExpression → Expression → bffi:contribution → bffi:agent` when `_primary_agent_uri()` returns None.
- [x] Translator-role blocklist: `bf:role` matching LoC `relators/trl` URI OR rdfs:label in `{kääntäjä, översättare, translator, übersetzer}` blocks the contribution from anchoring.
- [x] Lexicographically-smallest non-translator agent URI picked as the deterministic anchor.
- [x] `CanonicalWorkInputs.mint_anchor: "primary" | "first-contributor" | None` field records which path resolved.
- [x] Canonical Turtle carries `<canonical_uri> bffi-prov:mintAnchor <bib:auth/{primary-author,first-contributor}-anchored>` so cataloguers + dashboard filters can split on the anchor kind.
- [x] `provenance/vocab.py` exports `mintAnchor`, `MINT_ANCHOR_PRIMARY_AUTHOR`, `MINT_ANCHOR_FIRST_CONTRIBUTOR`.
- [x] 4 regression tests in `tests/unit/test_merge.py`:
  - `test_first_contribution_fallback_picks_lex_min_agent_uri`
  - `test_first_contribution_fallback_skips_translator_only_records`
  - `test_first_contribution_fallback_returns_none_for_truly_anonymous`
  - `test_canonical_carries_mintanchor_predicate_for_editor_anchored`

**Measured on the 2026-05-14 helmet-5k bench** (re-run of M8 against the existing M2/M3/M5/M6 outputs at `runs/721f5548680d4c08afd8bbef8d76393e/`):

| | Before Phase A | After Phase A |
|---|---:|---:|
| Canonical Works minted | 4,163 | **4,825** |
| ↳ primary-author-anchored | 4,163 | 4,163 |
| ↳ first-contributor-anchored | 0 | **662** |
| Mint failures | 707 | **45** |
| Coverage of M2-succeeded set (4906) | 84.9% | **98.4%** |

662 / 707 = **93.6% of the previously-dropped records recovered**. The remaining 45 are truly anonymous (zero contributors of any kind) and need Phase B.

### Phase B — Title-only mint + cataloguer-curated rule table (backlog)

- [ ] When `_primary_agent_uri()` AND `_first_contribution_agent_uri()` BOTH return None: mint canonical URI from `(NULL, pref_label)`. Tag with `bffi-prov:mintAnchor = bib:auth/title-only-anchored`.
- [ ] Optional `config/m8-anonymous-mint-rules.yaml` for Helmet-specific patterns (annual report series, government publications); each rule maps a 245-pattern + cataloguer-tag to a mint strategy.
- [ ] Risk surface: two truly-anonymous records with identical titles merge into one canonical Work. Mitigation: corpus-wide measurement of `(NULL, title)` collision rate; if material, cataloguer rules disambiguate.
- [ ] Prerequisites: cataloguer engagement on rule shapes; P-30 (observability-audit gate) clearance.

**Phase B is gated on a real cataloguer-side ask.** At 0.9% (45/4906) on Helmet data, the remaining mint failures may be acceptable forever — Phase B only matters if a downstream consumer (Skosmos browser, NLF hand-off, OPAC integration) actually needs them in the canonical graph.

### Phase C — Mint-key refactor (deferred indefinitely)

- [ ] Replace `(creator_uri, pref_label)` with a richer multi-input hash that gracefully degrades through `(primary, editor, publisher, year, pref_label)`.

**Phase C is deferred unless Phase A+B prove insufficient on full corpus.** Re-mints every existing canonical URI → ~5× M8 wall-time, hours of M9 re-reconcile, full Fuseki replace. Don't open this can without evidence.

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
