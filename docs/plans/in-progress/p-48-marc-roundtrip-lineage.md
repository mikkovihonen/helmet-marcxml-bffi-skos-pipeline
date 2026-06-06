# P-48 — MARC round-trip lineage tracking

**Status**: backlog. Drafted 2026-06-06 as a successor to the
P-47 round-trip work (committed but never plan-tracked). Phase A
implementation starts in the same session that drafted this plan.

**Plan-base commit**: `c53ef2a` (post-batch-B missing-field round-trip
work — 020 / 028 / 250 / 490 / 500 / 505 / 648 / 651 routing).
Before executing later phases, run
`git diff c53ef2a..HEAD -- src/bffi_pipeline/marc_roundtrip/ sparql/bf_to_bffi_*.rq gold/cataloguer-review.html`
to confirm no in-flight work has reshaped the round-trip surface.

**Phase commits**:

- Phase A (raw-URI-fragment lineage): `3be50cf`
- Phase B (synthetic lineage for flat Instance-side fields): `<unfilled>`
- Phase C (diff comparator pairs by lineage marker first): `<unfilled>`

**Owner**: TBD (Mikko, by default).

**Estimated wall-time**: 1-2 days total spread across phases.

- Phase A: ~2 h coding + tests. Converter reads URI fragment, stamps
  `$9 src=…`; diff comparator picks it up. Hooks into existing
  raw-bib URI emission for 6XX subjects + 7XX agents — no SPARQL
  changes.
- Phase B: ~half-day. Adds `bffi-prov:fromSourceField` triples in
  the Manifestation SPARQL for flat fields (020 / 250 / 490 / etc.).
  Larger surface because each flat predicate needs its own routing.
- Phase C: ~2 h. Diff comparator pairs by lineage token first,
  falling back to today's tag + $a + position heuristic when the
  token is absent.

## Motivation

Today's round-trip diff at `src/bffi_pipeline/marc_roundtrip/diff.py`
pairs source MARC fields to reconstructed fields by:

1. Group by `tag` (650s with 650s, 700s with 700s).
2. Within a tag bucket, match by primary `$a` text equality.
3. Match remaining by position.
4. Surplus → `lost` (source has it, recon doesn't) or `added`
   (recon has it, source doesn't).

Every step is heuristic. It breaks down in real records:

- **`$a` drifts.** Cataloguer-typed commas, normalisation, and
  M9-bound authority labels mean the recon's `$a` often doesn't
  match the source character-for-character — pair drops to the
  position fallback even when the lineage is unambiguous.
- **Position is unstable.** The converter emits subjects in graph-
  iteration order; the source has them in MARC ordinal order. Same
  count + same tag + different order = wrong pairs.
- **Cross-tag misroutes.** The b10303327 "Greece-as-650" bug
  (committed fix in `c53ef2a`) was a routing bug — the source 651
  Kreikka became a recon 650 with a YSO URI. The diff couldn't
  show that as "tag changed" because the original 651 bucket and
  the recon 650 bucket never interact.

The fundamental fix: **explicit lineage**. Each round-trip
datafield carries a stable token pointing back to the source field
it represents. The diff pairs on the token, falling back to
today's heuristic only when no token is present (the unconverted
back-compat case).

**The cheap half is free data.** M3 already mints raw URIs with
positional fragments (`#Topic650-20`, `#Place651-21`,
`#Agent700-23`). The fragment literally encodes "20th datafield
in source MARC, tag 650". The round-trip converter just needs to
read it and stamp the emitted datafield. No SPARQL changes; no new
predicates; no graph-shape churn.

**The expensive half needs new triples.** Flat Instance-side
fields — 020 ISBN, 028 publisher number, 250 edition, 264
provision activity, 300 extent — have no raw URI fragments. They
live as flat literals or blank nodes on bf:Instance. To pair
those rigorously, M3 needs to emit a sibling
`bffi-prov:fromSourceField "020-1"` triple per field. This is
larger but unblocks the second half of the records.

## Out of scope (deliberately not done here)

- **Holistic HTML layout improvements** to the cataloguer-review
  diff viewer (split full-record view, subfield-level alignment
  inside cells, color-coded diff highlighting). Those are independent
  UX work and depend on the diff JSON shape this plan stabilises;
  surface as a follow-up once Phase C lands.
- **Provenance for cataloguer-internal subjects without `$0` in
  source** that get reconciled to authority URIs without a raw
  fragment carry-through. The raw URIs DO carry the fragment
  today; if M9 ever drops them in favour of a pure-authority
  graph shape, this plan would need an explicit `bffi-prov:`
  triple per subject too. Currently free.
- **Bidirectional verification** (reconstruct MARC from BFFI, then
  reconstruct BFFI from the reconstructed MARC, diff against the
  canonical BFFI). Useful for catching ontology-level drift but
  one round of round-trip is enough for the cataloguer-review
  surface this plan targets.
- **Per-subfield lineage** (e.g. "the source 700$a went to recon
  700$a, the source 700$e went to recon 700$e via the relator-term
  table"). The field-level token gets us to the right MARC tag +
  position; subfield-level diff inside the cell can be inferred
  from string-set overlap at render time.

## Definition of done

### Phase A — Raw-URI-fragment lineage (the cheap half)

#### A.1 Lineage subfield convention

- [ ] Add a module-level constant `LINEAGE_SUBFIELD: Final[str] = "9"`
  in `src/bffi_pipeline/marc_roundtrip/converter.py`. Subfield $9 is
  the MARC convention for local processing; it's distinct from $5
  (which already carries the `FI-HELME/bffi-roundtrip` round-trip
  marker) and won't collide with cataloguer-supplied subfields.
- [ ] Lineage value format: `src=<tag>-<ordinal>`. Example:
  `src=650-20` (20th datafield in source, tag 650). The ordinal
  comes from the M3-minted raw URI fragment `#Topic650-20`,
  `#Place651-21`, `#Agent700-23`, etc. Document the format in the
  module docstring.

#### A.2 Converter helper

- [ ] Add `_emit_lineage(record, source_uri)` that takes a
  bib-raw URI like `http://urn.fi/URN:NBN:fi:bib:raw/b10303327#Topic650-20`,
  parses the fragment into `(tag, ordinal)`, and returns a
  subfield tuple `("9", f"src={tag}-{ordinal}")` for inclusion in
  `_emit_datafield`. Fragment-pattern regex: `#(Topic|Place|Agent|Hub|MusicMedium|IntendedAudience|CreatorCharacteristic)(\d{3})-(\d+)`.
  The middle 3-digit group IS the MARC tag (650 / 651 / 600 / 700 / 710 / 711 / 730 / 382 / 385 / 386).
- [ ] Returns `None` for non-URI inputs and URIs that don't match
  the pattern (caller falls back to no-lineage stamp — that field
  uses the heuristic pair).

#### A.3 Wire lineage into per-field emitters

- [ ] `_emit_subjects` (650 / 651 / 648 routing) — stamp lineage
  derived from the iteration's `subject` URI (or its raw-bib origin
  via `raw_origin_hints`).
- [ ] `_emit_genre_forms` (655) — same as subjects, derive from
  the genre URI / its raw origin.
- [ ] `_emit_primary_contribution` (100) — derive from
  the contribution's bnode + the work's bib-raw URI (no fragment
  here today; surface as a `bffi-prov:` triple in Phase B).
  Phase A leaves 100 unstamped.
- [ ] `_emit_added_entries` (700 / 710 / 711) — derive from the
  agent URI's `#Agent700-N` / `#Agent710-N` / `#Agent711-N`
  fragment.
- [ ] `_emit_datafield` extended to accept an optional
  `lineage: tuple[str, str] | None = None` keyword; appends the
  `$9 src=…` subfield immediately before the existing `$5`
  round-trip marker so the lineage and marker stay adjacent for
  easy parsing.

#### A.4 Diff comparator change

- [ ] Strip the `$9 src=…` subfield from the reconstructed side
  before subfield-equality comparison (parallel to today's `$5
  FI-HELME/bffi-roundtrip` strip in
  `src/bffi_pipeline/marc_roundtrip/diff.py:_parse_record`).
- [ ] Carry the parsed lineage onto the `FieldRecord` dataclass
  as `lineage: str | None = None`.
- [ ] Rewrite `_pair_data_fields` to: (a) bucket reconstructed
  fields by lineage token; (b) compute the source field's
  expected lineage token from its tag + 1-indexed position within
  the tag bucket; (c) pair via lineage token first, falling back
  to today's tag+$a+position heuristic for the unstamped tail.
- [ ] The pairing change MUST recover the b10303327 Greece case:
  source `651-1 Kreikka` pairs with recon `650 $0 yso/p105037
  $9 src=651-1`. The diff shows `status=changed`, `tag_changed`
  note: "651 → 650" plus the existing indicator notes.

#### A.5 New diff-status when lineage signals a tag move

- [ ] Add `tag-changed` to the `DiffStatus` Literal. It fires when
  the lineage-paired fields have different tags. Render in the
  HTML viewer with its own colour band (sits between `changed` and
  `lost`).
- [ ] The HTML viewer (`gold/cataloguer-review.html`) gets one
  new CSS class `.diff-tag-changed` and one new chip in the
  status enum.

#### A.6 Tests

- [ ] `test_lineage_marker_emitted_for_raw_subject_uri` — minimal
  graph with a `#Topic650-N` raw URI, assert the converter emits
  `$9 src=650-N` on the 650 row.
- [ ] `test_lineage_marker_emitted_for_700_agent` — `#Agent700-N`
  raw URI on a non-primary contribution agent, assert `$9
  src=700-N` on the 700 row.
- [ ] `test_lineage_pairs_authority_uri_via_raw_skos_exactmatch`
  — the b10303327 case: authority URI on the work, raw `#Place651`
  carrying `skos:exactMatch`, assert the 651 recon row carries
  `$9 src=651-N` (the raw origin's fragment, not the authority's
  bare URI).
- [ ] `test_diff_pair_lineage_match_overrides_tag_bucket` —
  source has `tag=651 a=Kreikka`, recon has `tag=650 $0 yso/...
  $9 src=651-1`, assert the diff pairs them and emits `status=
  tag-changed` with the expected tag-change note.
- [ ] `test_diff_pair_falls_back_to_heuristic_when_no_lineage`
  — recon has no `$9` (back-compat path), pair behaves exactly
  as today.

#### A.7 Coverage observability

- [ ] After Phase A lands, run the cataloguer-bundle stage on the
  500-sample and tally: % of recon datafields carrying `$9
  src=…`. Report in this plan as a Phase-A landed-metric.
  Expected coverage: ~70-80 % (6XX + 7XX dominate the field
  count; flat-Instance fields are the remaining unstamped tail).

### Phase B — Synthetic lineage for flat Instance-side fields

#### B.1 `bffi-prov:fromSourceField` predicate

- [ ] Document the new predicate in CLAUDE.md's "Committed
  identifiers" section under the `bffi-prov:` namespace. URI:
  `http://urn.fi/URN:NBN:fi:schema:bffi-prov#fromSourceField`.
  Range: `xsd:string`; value format `<tag>-<ordinal>` (same as
  the `$9 src=…` payload from Phase A).
- [ ] Compute the ordinal at M3 time. Source MARC field positions
  aren't carried into BIBFRAME by marc2bibframe2 — we have to
  synthesise from `bf:Instance`'s sub-predicate iteration. Order
  is deterministic per re-run but doesn't match source ordinal.
  Acceptable: the goal is to pair "this 020 in source with this
  020 in recon," not to recover the source's exact 020 ordering
  (which the cataloguer doesn't care about either).

#### B.2 M3 SPARQL extensions

- [ ] `sparql/bf_to_bffi_manifestation.rq` — for each routed
  flat predicate (`bffi:editionStatement`, `bf:identifiedBy →
  bf:Isbn`, `bf:identifiedBy → bf:AudioIssueNumber`,
  `bffi:hasSeries`, `bffi:note` instance-side,
  `bffi:tableOfContents`, `bffi:provisionActivity`,
  `bffi:publicationStatement`, `bffi:extent`, `bffi:dimensions`,
  `bf:title` for 245), emit a sibling
  `?manifURI bffi-prov:fromSourceField "020-1"` style triple. Use
  `arq:sha1`-derived ordinals if a stable hash is needed for
  determinism, or just hard-code "1" when the predicate is
  semantically single-valued (250, 264, 300).

#### B.3 Converter passes prov triples through to `$9`

- [ ] Round-trip converter reads the `bffi-prov:fromSourceField`
  literal on each manifestation-side predicate's target. Where
  present, stamp `$9 src=…` as in Phase A. The literal IS the
  payload — no fragment parsing needed.

#### B.4 Diff comparator no change

- [ ] Phase A's pairing already keys on `$9 src=…`. Phase B just
  populates more rows. Verify by re-running the cataloguer-bundle
  and re-tallying the coverage metric — expected jump from
  ~70-80 % to ~95 %.

### Phase C — Cleanup + observability

- [ ] Replace the per-tag `_pair_data_fields` heuristic with a
  single global pass that walks ALL recon fields, partitions by
  lineage token, falls back to tag-bucket heuristic only for the
  lineage-absent residue.
- [ ] Add a diff-summary metric: `paired_via_lineage` vs
  `paired_via_heuristic` counts, surfaced in
  `cataloguer-review.html`'s per-record summary line and in the
  bundle manifest. Cataloguers see the pairing-confidence ratio
  at a glance.
- [ ] Update `docs/archived/marcxml-to-bffi-skosmos-pipeline.md`
  back-references / round-trip section IF the diff JSON shape
  change has external consumers.

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| `$9 src=…` collides with cataloguer-supplied `$9` in source MARC | Medium — `$9` is local but Helmet has used it historically | Diff comparator strips `$9 src=…` (prefix-match on value) before subfield equality. Cataloguer-supplied `$9 foo` survives untouched. Pin via a test fixture. |
| Raw URI fragment naming drifts (e.g. `#Topic650-N` becomes `#Topic-N` someday) | Low — naming is committed identifier surface | The fragment regex in `_emit_lineage` is the single point of dependence. If M3 changes the convention, this plan's tests will fail loudly. |
| Phase B's synthetic ordinals don't match source ordinals | Always — we can't recover source positions for flat predicates | Document explicitly in this plan: synthetic ordinals are *stable across re-runs* but DO NOT match source 020 / 250 / 264 / 300 positions. Pairing is by (tag + within-recon ordinal); cataloguers see "this 020" matched to "that 020", not "ISBN #1 in source" matched to "ISBN #1 in recon." Acceptable because Helmet records have 1-2 of each at most. |
| Hub-shape fields (240, 730) lose lineage in Phase A because they're not yet emitted by the converter | High — 240 / 730 are deferred per the c53ef2a missing-field batch | Phase A leaves Hub-shape unstamped because the converter doesn't emit those tags yet. When 240 / 730 emission lands (post-P-48), the lineage hook is already there — just need to wire `_emit_lineage` into the new emitter. |

## Rollback procedure

- Phase A: revert the converter + diff.py changes. The `$9
  src=…` subfield is purely additive on the recon side and the
  diff comparator's lineage path was the new path — falling
  back to today's heuristic is "git revert the phase commit."
  No data migration required because no graph triples are
  changed by Phase A.
- Phase B: revert the SPARQL changes. The `bffi-prov:
  fromSourceField` triples are additive; removing them returns
  the graph to its pre-Phase-B shape.
- Phase C: revert; the cleanup is non-load-bearing.

## Suggested next step

Ship Phase A — implementation is ready to start. The cataloguer-bundle
re-run from `bpnc10fl4` (currently in flight at plan-base time) will
also be the post-Phase-A verification surface.
