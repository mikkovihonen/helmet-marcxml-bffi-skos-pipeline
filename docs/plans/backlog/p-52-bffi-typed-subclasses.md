# P-52 — BFFI typed subclasses for Work / Expression / Manifestation

**Status**: backlog. Redesigned 2026-06-07 from a narrow "AggregatingWork only" scope into the full subclass-typing landscape after the BFFI 1.0.0 ontology audit showed we're emitting only 3 of 35+ available class types. Aggregation becomes one of several phase clusters within this plan rather than the whole plan.

**Scope**: every BFFI `Work` / `Expression` / `Manifestation` (and where signals exist, `Item`) entity gets typed with the most-specific BFFI subclasses the source MARC signal supports. Today the pipeline emits one `rdf:type` triple per entity (`bffi:Work`, `bffi:Expression`, `bffi:Manifestation`) plus the source `bf:Text` / `bf:Cartography` / etc. secondary type carried through verbatim. BFFI defines:

- 11 `bffi:Work` subclasses
- 18 `bffi:Expression` subclasses (largest group — content-type-derived)
- 6 `bffi:Manifestation` subclasses (carrier-form-derived)
- 1 `bffi:Item` subclass (out of scope here — covered in [P-46](../proposed/p-46-bffi-item-class.md))

Of these 35 subclasses, **16 are OWL-equivalent to BIBFRAME classes** already present in the source graph (e.g., `bffi:Text owl:equivalentClass bf:Text`); the remaining 19 are BFFI-original or require source signals we don't currently read (MARC 008/23, composite aggregation signal).

**Plan-base commit**: `8a09f14` (P-50 #163 — 655 correlator fix + Statement-reification consumer). Before starting any phase, run
`git diff 8a09f14..HEAD -- sparql/bf_to_bffi_*.rq src/bffi_pipeline/stages/m3/ src/bffi_pipeline/marc_roundtrip/converter.py docs/lkd.rdf`
to confirm no in-flight work has reshaped the typing surfaces.

**Phase commits**: (filled in as each phase ships)

- Phase A (OWL-equivalent subclass typing — 16 mirrored classes): pending
- Phase B (broadMatch Work/Expression typing — Cartography, MovingImage, Music, NonMusicAudio): pending
- Phase C (issuance-derived typing — Monograph / Serial / Integrating / Collection on Work + Expression): pending
- Phase D (form-of-item Manifestation typing — Print / Electronic / Microform / Tactile, from MARC 008/23): pending
- Phase E (Aggregating detection + Work/Expression typing): pending
- Phase F (`bffi:aggregates` / `bffi:aggregatedBy` edges + component Expression CONSTRUCT): pending
- Phase G (round-trip emit of 700 ind2=2 / 730 / 740 analytics): pending
- Phase H (M9 component-Expression reconciliation): pending
- Phase I (cataloguer-review subclass-typing + aggregation tab): pending

**Owner**: Mikko, by default.

## Motivation

BFFI's typing system encodes two orthogonal axes inherited from FRBR/RDA:

- **Work axis** — abstract content shape: `MonographWork`, `SerialWork`, `CollectionWork`, `CartographyWork`, `MovingImageWork`, `MusicWork`, `NonMusicAudioWork`, `Manuscript`, `Integrating`, `SeriesWork`, `AggregatingWork`.
- **Expression axis** — realisation form: `Text`, `NotatedMusic`, `StillImage`, `Dataset`, `Object`, `MixedMaterial`, `Multimedia`, `MovingImageExpression`, `MusicAudioExpression`, `NonMusicAudioExpression`, `NotatedMovement`, `Arrangement`, plus issuance variants (`MonographExpression`, `SerialExpression`, `CollectionExpression`, `SeriesExpression`, `AggregatingExpression`), plus `CartographyExpression`.
- **Manifestation axis** — carrier form: `Print`, `Electronic`, `Microform`, `Tactile`, `Archival`, `CollectionManifestation`.

A single record can carry **multiple typings** along each axis — a printed atlas is simultaneously a `MonographWork` (issuance), a `CartographyWork` (content), and a `Print` Manifestation (carrier), with the Expression typed as `StillImage` (realised as printed maps). BFFI uses `subClassOf bffi:Work` etc. so existing consumers walking `?w a bffi:Work` keep working — the subclass typing is purely additive.

Today the pipeline:

- Emits `?work a bffi:Work` plus the source `bf:Text`/`bf:Cartography`/etc. **BIBFRAME** class verbatim (kept from marc2bibframe2's output) — but never the parallel `bffi:Text` / `bffi:CartographyWork` typing.
- Emits `?expression a bffi:Expression` with no subclass at all.
- Emits `?manifestation a bffi:Manifestation` with no subclass.
- Has all the source signals (`bf:issuance` URI tail for LDR/07, BIBFRAME content types in the source graph, AdminMetadata's `bffi:descriptionConventions` for DACS) but routes them only into the round-trip leader and AdminMetadata block — never back onto the entity typing.
- For aggregating-shaped records (~15% of the 500-sample), the work CONSTRUCT's `FILTER NOT EXISTS { ?ref bf:associatedResource ?bfWork }` drops the contained-work links entirely, throwing away both the typing signal and the component-Expression structure.

### Why this matters

1. **Skosmos discoverability.** With proper typing, Skosmos can group resources by class — "all Monograph works", "all Cartography works", "all Print manifestations" — at zero query cost. Today every record sits in one big `bffi:Work` bucket.
2. **Bibliographic fidelity.** A printed atlas modelled as a plain `bffi:Work` loses the cartography signal that drives subject browsing and authority routing.
3. **M9 routing precision.** Cartographic works reconcile against KANTO's place-authority more aggressively than against person-authority; the typing carries the routing hint.
4. **Round-trip diff visibility.** Today the BIBFRAME `bf:Text` / `bf:Cartography` typing lives only as a flat `a bf:Text` triple. The round-trip converter reads it for LDR/06 but the cataloguer-review surfaces don't expose what *kind* of resource each row describes.
5. **Future-proofing.** As BFFI's vocabulary grows (it's still maturing), staying close to the published class hierarchy makes pipeline drift easier to detect — every new subclass triggers a `lkd.rdf` re-import, and the namespace-discipline test catches anything we miss.

### Scope evidence (500-record sample probe)

| Source signal | Records | % | Target subclass |
|---|---:|---:|---|
| `bf:issuance/mono` | ~496 | 99.2% | `bffi:MonographWork` + `MonographExpression` |
| `bf:issuance/serial` | ~4 | 0.8% | `bffi:SerialWork` + `SerialExpression` |
| `bf:Text` on source Work | ~480 | 96% | `bffi:Text` (Expression) |
| `bf:NotatedMusic` | ~10 | 2% | `bffi:MusicWork` + `bffi:NotatedMusic` |
| `bf:MusicAudio` | ~30 | 6% | `bffi:MusicWork` + `bffi:MusicAudioExpression` |
| `bf:MovingImage` | ~5 | 1% | `bffi:MovingImageWork` + `bffi:MovingImageExpression` |
| `bf:Cartography` | ~3 | 0.6% | `bffi:CartographyWork` + `bffi:CartographyExpression` |
| **Any aggregating signal** | **75** | **15%** | **`bffi:AggregatingWork` + `bffi:AggregatingExpression`** |
| **MARC 008/23 = 'r' (regular print)** | ~95% | est. | `bffi:Print` |
| **MARC 008/23 = 'o' / 's' (online / electronic)** | small | est. | `bffi:Electronic` |
| DACS in description conventions | <1% | rare in Helmet | `bffi:Archival` + `bffi:CollectionWork` |

The clearest demonstration record is **b10068004** (Audition songs for female singers): a printed monograph compilation of music scores. Today typed as plain `bffi:Work`/`Expression`/`Manifestation`. With this plan: `bffi:MonographWork`, `bffi:AggregatingWork`, `bffi:MusicWork` (Work axis), `bffi:MonographExpression`, `bffi:AggregatingExpression`, `bffi:NotatedMusic` (Expression axis), `bffi:Print` (Manifestation axis), plus 9 component `bffi:Expression`s for the individual songs.

## Mapping table — definitive

The BFFI ontology declares 16 BFFI subclasses as `owl:equivalentClass` of a BIBFRAME class. These are direct 1:1 routings:

| Source BIBFRAME class | Target BFFI subclass | BFFI axis |
|---|---|---|
| `bf:Text` | `bffi:Text` | Expression |
| `bf:NotatedMusic` | `bffi:NotatedMusic` | Expression |
| `bf:NotatedMovement` | `bffi:NotatedMovement` | Expression |
| `bf:StillImage` | `bffi:StillImage` | Expression |
| `bf:Dataset` | `bffi:Dataset` | Expression |
| `bf:Object` | `bffi:Object` | Expression |
| `bf:MixedMaterial` | `bffi:MixedMaterial` | Expression |
| `bf:Multimedia` | `bffi:Multimedia` ("Software") | Expression |
| `bf:Arrangement` | `bffi:Arrangement` | Expression |
| `bf:Manuscript` | `bffi:Manuscript` | Work |
| `bf:Integrating` | `bffi:Integrating` | Work |
| `bf:Print` | `bffi:Print` | Manifestation |
| `bf:Electronic` | `bffi:Electronic` | Manifestation |
| `bf:Microform` | `bffi:Microform` | Manifestation |
| `bf:Tactile` | `bffi:Tactile` | Manifestation |
| `bf:Archival` | `bffi:Archival` | Manifestation |

The remaining BFFI subclasses use `bffi-meta:broadMatch` (less strict) and need source-specific routing logic:

| Source signal | Target | Notes |
|---|---|---|
| `bf:Cartography` (on source `bf:Work`) | `bffi:CartographyWork` + `bffi:CartographyExpression` | Two-axis emit |
| `bf:MovingImage` (on source `bf:Work`) | `bffi:MovingImageWork` + `bffi:MovingImageExpression` | Two-axis emit |
| `bf:MusicAudio` (on source `bf:Work`) | `bffi:MusicWork` + `bffi:MusicAudioExpression` | Two-axis emit |
| `bf:Audio` (on source `bf:Work`) | `bffi:NonMusicAudioWork` + `bffi:NonMusicAudioExpression` | Two-axis emit |
| `bf:issuance/mono` URI tail | `bffi:MonographWork` + `bffi:MonographExpression` | Read from `bf:issuance` on bf:Instance |
| `bf:issuance/serial` URI tail | `bffi:SerialWork` + `bffi:SerialExpression` | Same source |
| `bf:Collection` rdf:type on source | `bffi:CollectionWork` + `bffi:CollectionExpression` + `bffi:CollectionManifestation` | Three-axis emit |
| `bf:hasSeries` on bf:Instance | `bffi:SeriesWork` + `bffi:SeriesExpression` | Per-series; needs minted Work URI |
| MARC 008/23 = 'r' (regular print) | `bffi:Print` (Manifestation) | New signal — not read today |
| MARC 008/23 = 'o' / 's' (online) | `bffi:Electronic` (Manifestation) | New signal |
| MARC 008/23 = 'a' / 'b' / 'c' (microform variants) | `bffi:Microform` | New signal |
| MARC 008/23 = 'f' (Braille) | `bffi:Tactile` | New signal |
| DACS in `bffi:descriptionConventions` | `bffi:Archival` + `bffi:CollectionWork` | Already read for LDR/08 |
| Composite aggregating signal (see Phase E) | `bffi:AggregatingWork` + `bffi:AggregatingExpression` | Custom detection |

## Phase plan

Nine phases. Phases A-E are pure typing additions (no consumer impact). Phases F-H introduce structural change to the canonical graph + round-trip + M9 (the aggregation-specific work). Phase I is reviewer-facing.

### Phase A — OWL-equivalent subclass typing (16 mirrored classes)

For every entity in the canonical graph that carries a BIBFRAME class with an OWL-equivalent BFFI subclass, emit the parallel BFFI typing. Pure SPARQL CONSTRUCT additions, scoped per file:

- `sparql/bf_to_bffi_work.rq`: when `?bfWork a bf:Manuscript`, emit `?workURI a bffi:Manuscript`. Same for `bf:Integrating`.
- `sparql/bf_to_bffi_expression.rq`: when `?bfWork a bf:Text` (etc.), emit `?exprURI a bffi:Text` (and parallel for `bf:NotatedMusic`, `bf:NotatedMovement`, `bf:StillImage`, `bf:Dataset`, `bf:Object`, `bf:MixedMaterial`, `bf:Multimedia`, `bf:Arrangement`).
- `sparql/bf_to_bffi_manifestation.rq`: when `?bfInstance a bf:Print` (etc.), emit `?manifURI a bffi:Print` (and parallel for `bf:Electronic`, `bf:Microform`, `bf:Tactile`, `bf:Archival`).

**Acceptance**: integration test asserts a synthetic `bf:Work a bf:Text` source produces `?expr a bffi:Expression, bffi:Text` (both triples present). On the 500-record sample, ~96% of Expressions get `bffi:Text`; the Manuscript / Manifestation-axis classes appear on the rare records that carry them. No round-trip impact (typing is read-only from the converter's perspective).

### Phase B — broadMatch Work/Expression typing (Cartography / MovingImage / Music)

Four BIBFRAME content classes need two-axis emit (one Work-side, one Expression-side):

| Source | Work-side | Expression-side |
|---|---|---|
| `bf:Cartography` | `bffi:CartographyWork` | `bffi:CartographyExpression` |
| `bf:MovingImage` | `bffi:MovingImageWork` | `bffi:MovingImageExpression` |
| `bf:MusicAudio` | `bffi:MusicWork` | `bffi:MusicAudioExpression` |
| `bf:Audio` (non-music) | `bffi:NonMusicAudioWork` | `bffi:NonMusicAudioExpression` |

SPARQL CONSTRUCT branches in `bf_to_bffi_work.rq` + `bf_to_bffi_expression.rq`. Same shape as Phase A but with a per-type Work-vs-Expression split.

**Acceptance**: a `bf:Cartography`-typed source produces both `?work a bffi:CartographyWork` and `?expr a bffi:CartographyExpression`. Boundary-5 smoke test (`Skosify dual-typing`) still passes — these are additional types, not replacements.

### Phase C — Issuance-derived typing (Monograph / Serial / Integrating / Collection)

Read `bf:issuance` URI tail on the bf:Instance (the converter already reads this for LDR/07) and emit the corresponding BFFI Work + Expression subclass:

- `<…/issuance/mono>` → `bffi:MonographWork` + `bffi:MonographExpression`
- `<…/issuance/serial>` → `bffi:SerialWork` + `bffi:SerialExpression`
- `<…/issuance/integrating>` → `bffi:Integrating` (Work-side; no parallel Expression class in BFFI)
- `<…/issuance/collection>` or DACS signal → `bffi:CollectionWork` + `bffi:CollectionExpression` + `bffi:CollectionManifestation`

The issuance signal lives on the bf:Instance but BFFI's typing applies to the Work and Expression. The M3 manifestation CONSTRUCT walks `?bfInstance bf:instanceOf ?bfWork`; piggyback on that inverse-link to route the typing from the bf:Instance's `bf:issuance` triple to the canonical Work / Expression URIs.

**Acceptance**: on the 500-record sample, ~99% of Works gain `bffi:MonographWork` typing; the 4 records with `bf:issuance/serial` gain `bffi:SerialWork`. The 4 BFFI Manifestation-axis triples (`CollectionManifestation` on the rare DACS-bound records) appear correctly.

### Phase D — Form-of-item Manifestation typing (Print / Electronic / Microform / Tactile)

MARC **008/23** (Form of item) is the source signal for carrier form. Currently the pipeline doesn't read 008/23. Add:

- M2 (`marc2bibframe2` is reading 008 anyway) → the BIBFRAME side might already emit `bf:Print` / `bf:Electronic` / `bf:Microform` / `bf:Tactile` on the bf:Instance. **Verify first** — if marc2bibframe2 already emits these, Phase D collapses to a no-op (Phase A's OWL-equivalent table already routes them).
- If marc2bibframe2 doesn't: read MARC 008/23 in M2's salvage pass and emit `bf:Print` / `bf:Electronic` / `bf:Microform` / `bf:Tactile` on the bf:Instance using the mapping `'r' → Print`, `'o'|'q'|'s' → Electronic`, `'a'|'b'|'c'|'d' → Microform`, `'f' → Tactile`. Phase A then routes BIBFRAME → BFFI.

**Acceptance**: every Manifestation has at least one carrier-form subclass (or explicitly logged as "no carrier-form signal in source"). Round-trip 008/23 emission stays unchanged (we already synthesise it from carrier signals).

### Phase E — Aggregating detection + Work/Expression typing

Detect aggregating records via composite signal:

- ≥2 source 730 datafields (compilation pattern), or
- ≥2 source 740 datafields, or
- ≥1 source 700 with `ind2=2` (strict-MARC analytical entry), or
- ≥1 source 740 with `ind2=2`, or
- 245 with `$n` or `$p` (multipart designator).

When detected, type the parent Work as `bffi:AggregatingWork` and the parent Expression as `bffi:AggregatingExpression`. Coexists with the Phase A-D typing — an aggregating songbook gets `bffi:AggregatingWork` AND `bffi:MonographWork` AND `bffi:MusicWork` on the same Work URI.

Detection location is open: SPARQL inside `bf_to_bffi_work.rq` (with COUNT subqueries) or a Python post-pass in `m3/post_process.py` walking the source graph. The Python path is simpler to iterate on; the SPARQL path is more declarative. Phase E commit decides at implementation time.

**Acceptance**: ~75/500 records (15%) emit `bffi:AggregatingWork`. b10068004 is in the set.

### Phase F — `bffi:aggregates` / `bffi:aggregatedBy` edges + component Expressions

For each aggregating record, mint a component Expression URI per analytical entry and emit:

```turtle
<aggregating-expr> a bffi:AggregatingExpression , bffi:Expression , bffi:MonographExpression ;
                   bffi:aggregates <component-expr-1> ,
                                   <component-expr-2> ,
                                   … .

<component-expr-1> a bffi:Expression ;
                   bffi:aggregatedBy <aggregating-expr> ;
                   skos:prefLabel "I dreamed a dream"@en ;
                   bffi:title <component-title-1> .
```

Component-Work URI: `http://urn.fi/URN:NBN:fi:bib:work:<sha1(parent-bib + component-marc-key)>`. Derivative of the parent's so the component is greppable.

Component-Expression URI: parallel pattern, `expression:<sha1(parent-bib + component-marc-key)>`.

Source signals that mint components:

- **730 ind1=0** in source → one component Expression per source 730 row; prefLabel from `$a`.
- **740 ind1=any** → one component Expression per source 740 row when not already captured by a 730.
- **700 ind2=2** → one component Expression with a `bffi:contribution → bffi:agent` link to the analytical entry's agent; title from `$t` (uniform-title subfield).

**Acceptance**: b10068004 emits 9 component Expressions, each linked via `bffi:aggregates` from the parent Expression. The parent's `bf:hasSeries` flat shortcut is retained for Skosmos compatibility.

### Phase G — Round-trip emit of aggregated components (700 ind2=2 / 730 / 740)

Today the round-trip converter has no path for emitting 700 ind2=2 / 730 / 740 from aggregation triples — every component is silently dropped. Wire `_emit_aggregated_components(record)` as a new helper in `converter.py`:

- Walk `bffi:aggregates` on the Expression.
- For each component Expression with `bffi:contribution → bffi:agent` → emit 700 ind2=2 with the agent's name.
- Without a contribution → emit 730 ind1=0 or 740 (depending on the component's source-MARC-field token in `bffi-prov:fromMarcField`).
- Lineage: each component carries the P-50 token from M2-post; `_emit_datafield` consumes it.

**Acceptance**: b10068004 recon side regains its 9 730 ind1=0 rows + 9 700 ind1=1 composer entries. The current 18 "lost" rows for that record drop to 0 with `$9 src=` lineage matching source ordinals.

### Phase H — M9 reconciliation for component Expressions

`m9/requests.py` `_iter_creator_requests` walks all `bffi:Expression` subjects today. Components inherit the existing walk for free (they're `bffi:Expression`s). What needs explicit work:

- **Per-component picker context**: when reconciling a component's contribution, include the *parent's* prefLabel in the picker prompt situational context block. New candidate-context fetcher hook reads `?component bffi:aggregatedBy ?parent` and inlines parent context.
- **Provenance**: each component reconciliation emits its own `Reconciliation` Activity URI.
- **Picker cache**: shared component agents across records (e.g., Schönberg on two different songbook records) cache once.

**Acceptance**: integration test seeds two records aggregating the same component; M9 reconciles both to the same authority URI with one cache hit.

### Phase I — Cataloguer-review subclass-typing + aggregation tab

Two reviewer tabs:

- **"Typing"** — per-record summary of which Work / Expression / Manifestation subclasses were emitted. Lets cataloguers eyeball misclassifications.
- **"Aggregations"** — per aggregating record, list components with title + agent + reconciliation status.

Sidecar JSONL rows produced during the M3 / M9 passes; HTML reviewer renders them.

**Acceptance**: the new tabs populate non-trivially on the 500-record sample. Each record's Typing row matches what `bffi-pipeline q "SELECT ?t WHERE { <work-uri> a ?t }"` returns.

## Verification (run-wide, end of Phase I)

- `runs/<id>/marc-roundtrip/summary.json` shows materially-reduced `lost` rows for 700/730/740 on aggregating-flagged records (target: ≥80% drop on those 75 records).
- New `subclass_typing` field in `run.json` summarises per-axis counts (e.g., `monograph_work: 496, serial_work: 4, cartography_work: 3, …`).
- Cataloguer-review HTML's Typing + Aggregations tabs populate.
- Spot-check: `bffi-pipeline q --query "SELECT ?t (COUNT(*) AS ?n) WHERE { ?w a ?t. FILTER(STRSTARTS(STR(?t), STR(bffi:))) } GROUP BY ?t ORDER BY DESC(?n)"` returns a distribution dominated by `bffi:Work` + `bffi:MonographWork` + `bffi:Text` with long-tail content / aggregation / form classes.

## Migration / compatibility

- Phases A-E are **purely additive** typing — existing consumers walking `?w a bffi:Work` keep working (subclass semantics).
- Phase F changes the canonical graph (new component URIs, new `bffi:aggregates` edges). Consumers walking `?e a bffi:Expression` already see them (subclass typing); flat consumers querying `bffi:contribution` will see contributions on the component Expressions in addition to the parent.
- Phase G changes round-trip output for the 75 aggregating records. The 700/730/740 rows previously appeared as `lost`; after Phase G they appear as `identical` or `changed`. Diff per_status counters shift.
- Phase H changes M9 outputs (more Reconciliation Activities). Provenance graph grows ~10% on aggregating records, negligible corpus-wide.
- Phase I changes only the cataloguer-review bundle (additive).

For Skosmos: no breaking changes. New subclasses surface via the existing `bffi:Work` vocab; consumers gain navigability by class.

For Fuseki: triple count grows by an estimated +5-10% on the 800k load (multiple typing triples per entity + component Expression triples on the 15% aggregating subset).

For the gold sets and eval harnesses: no input change required.

## Risk register

- **Phase A: `bf:Multimedia` ambiguity.** BFFI calls `bf:Multimedia → bffi:Multimedia` with the label "Software", while the BIBFRAME class is broader. Risk of mislabelling. Mitigation: keep the equivalence (it's declared in `lkd.rdf`); BFFI's interpretation is the canonical one. Surface in the Typing tab so cataloguers can flag genuine mismatches.

- **Phase B: BIBFRAME → BFFI two-axis ambiguity.** `bf:Cartography` on source represents both Work-shape and Expression-shape; mapping to two BFFI classes is a choice we make, not an OWL declaration. Mitigation: the BFFI ontology's `bffi-meta:broadMatch` to `bf:Cartography` is on both `CartographyWork` and `CartographyExpression` — the ontology blesses the two-axis route. Document in the SPARQL CONSTRUCT comments.

- **Phase C: Series-vs-Aggregating overlap.** `bffi:SeriesWork` (a Series IS a kind of compilation) and `bffi:AggregatingWork` are both subclasses of Work; some records will reasonably get both. Mitigation: explicit (a record CAN be both); no exclusion logic.

- **Phase D: marc2bibframe2 008/23 routing inconsistency.** If marc2bibframe2 only emits `bf:Print` on some records, we'd type those records correctly but leave the rest as bare `bffi:Manifestation`. Mitigation: M2 salvage pass reads 008/23 directly when the BIBFRAME side is silent, so we have a guaranteed fallback path.

- **Phase E: Aggregating detection over-firing.** A record with ≥2 730s for cross-references (not compilation) gets wrongly typed. Mitigation: the disjunction-of-signals rule is permissive by design; cataloguer-review Typing tab makes false positives visible.

- **Phase F: Component URI collision across records.** Two parents aggregating the same conceptual song would mint different component-Expression URIs (different parent-bib in the SHA-1). M8 canonical Work mint should still merge them at the Work level (same `(title, primary-contributor)` block). Risk: M8's miner doesn't recognise aggregated components as merge-eligible. Mitigation: integration test on a seeded two-compilation scenario.

- **Phase G: Round-trip diff churn.** Phase G re-classifies ~1,800 rows from `lost` to `identical`/`changed`. Cumulative summary counts shift; cataloguer-review presents the shift as recovery. Mitigation: phase commit message documents the expected shift.

- **Phase H: Picker prompt blow-up.** A compilation with 30 components produces 30 picker requests; the per-call situational context block carries parent context each time. Mitigation: the parent-context block is short (one title literal); 30× is bounded. Cap at 50 components/parent with a "remaining: skipped" audit row.

- **Recursive aggregation.** `AggregatingExpression` is also `Expression`, so an anthology-of-anthologies is technically modelable. The converter's emit loop must not stack-overflow. Mitigation: emit-side traversal carries a visited-set; cycle test in the unit suite.

## Rollback procedure

Each phase is reversible without graph migration:

- **Phase A-D**: remove the typing CONSTRUCT branches; existing `bffi:Work`/`Expression`/`Manifestation` typing stays. Orphan subclass triples are harmless to consumers.
- **Phase E**: remove the AggregatingWork typing emit. M3 reverts to pre-aggregating-typing behaviour.
- **Phase F**: remove the component-Expression mint + `bffi:aggregates` emit. The Phase E typing stays (Work is still flagged aggregating, just without component edges).
- **Phase G**: remove `_emit_aggregated_components` from the converter. Recon returns to pre-aggregation behaviour.
- **Phase H**: revert `m9/requests.py` walking to skip Expressions whose `rdf:type` chain includes `bffi:AggregatingExpression`.
- **Phase I**: remove the new tabs from `cataloguer-bundle.zip`.

A full rollback to pre-Phase-A leaves orphan typing triples in canonical (harmless) and orphan component Expressions if Phase F shipped. A deliberate rollback strips them with a one-shot SPARQL DELETE pass.

## Relationship to other plans

- **P-46 (`bffi:Item` class)** — same shape of work (adopt a BFFI ontology class we currently don't emit). P-46 stays in `proposed/` pending a concrete consumer ask; P-52 graduates directly to backlog because the round-trip diff residue gives us an immediate consumer (the 75 mis-modelled aggregating records).
- **P-49 (BFFI structured fields vs marcKey)** — Phase F's component-Expression minter overlaps with P-49 Layer 1's `bf:partNumber` / `bf:partName` routing on 240 / 730 / 740. They co-evolve: Phase F emits component Expressions; P-49 Layer 1 routes part subfields within each component's title.
- **P-50 (source-field provenance)** — every typed entity carries its `bffi-prov:fromMarcField` token (the Phase A typing is purely additive on entities P-50 already tagged). Phase G's round-trip emit consumes those tokens for `$9 src=` lineage. P-50 Phase C's `rdf:Statement` reification is the prior art the aggregation edges follow.

## Suggested next step

Phase A: ship the OWL-equivalent typing CONSTRUCT branches first (lowest-risk, broadest coverage). One commit per CONSTRUCT file (`work.rq`, `expression.rq`, `manifestation.rq`) for tractable review. Verify via the existing M3 integration test suite + a new test asserting per-axis typing is present on the b10068004 fixture.
