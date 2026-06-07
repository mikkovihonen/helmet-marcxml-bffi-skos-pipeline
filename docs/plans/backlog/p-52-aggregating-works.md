# P-52 — `bffi:AggregatingWork` / `bffi:AggregatingExpression`

**Status**: backlog. Drafted 2026-06-07 directly into backlog; no proposal-shape precursor — the BFFI ontology already defines the classes and predicates, and the trade-offs are limited enough that we'll work them out in phases rather than as a forward-looking proposal.

**Scope**: introduce typed routing for compilation / multipart / anthology records. Type the parent Work as `bffi:AggregatingWork` (subclass of `bffi:Work`) and the parent Expression as `bffi:AggregatingExpression` (subclass of `bffi:Expression`). Wire `bffi:aggregates` / `bffi:aggregatedBy` between the aggregating Expression and its component Expressions. Restore the contained-work links the current Work CONSTRUCT filters out so the round-trip can emit MARC 700 ind2=2 / 740 / 730 analytics. Co-evolve the round-trip converter to read the new typing and routing.

**Plan-base commit**: `8a09f14` (P-50 #163 — 655 correlator fix + Statement-reification consumer in `_emit_genre_forms`). The aggregating work sits on top of the now-stable subject-statement reification pattern. Before starting any phase, run
`git diff 8a09f14..HEAD -- sparql/bf_to_bffi_*.rq src/bffi_pipeline/stages/m2/ src/bffi_pipeline/stages/m3/ src/bffi_pipeline/stages/m8/ src/bffi_pipeline/marc_roundtrip/`
to confirm no in-flight work has reshaped the Work-typing or Expression-typing surfaces.

**Phase commits**:

- Phase A (detection + Work/Expression typing): pending
- Phase B (aggregates/aggregatedBy edges + component Expression CONSTRUCT): pending
- Phase C (round-trip restoration of 700 ind2=2 / 740 / 730 ind1=0 analytics): pending
- Phase D (M9 reconciliation of component Expressions / component Works): pending
- Phase E (cataloguer-review aggregation tab): pending

**Owner**: Mikko, by default.

## Motivation

BFFI 1.0.0 defines two Work/Expression subclasses for compilation-shaped resources:

- `bffi:AggregatingWork` `rdfs:subClassOf bffi:Work` — the parent Work of an anthology, multi-author monograph, music compilation, journal issue, conference proceedings.
- `bffi:AggregatingExpression` `rdfs:subClassOf bffi:Expression` — the parent Expression carrying the aggregating relationship.

Edge predicates:

- `bffi:aggregates` — `AggregatingExpression → Expression` (`exactMatch` RDA `P20319`)
- `bffi:aggregatedBy` — `Expression → AggregatingExpression` (`exactMatch` RDA `P20320`, `owl:inverseOf bffi:aggregates`)
- `bffi:aggregatesAsExpression` — narrowed-range variant on `Expression → AggregatingExpression`

Today's pipeline never emits any of these. Every Helmet record types its Work as plain `bffi:Work` regardless of whether it's a monograph or a compilation. The M3 work CONSTRUCT's `FILTER NOT EXISTS { ?ref bf:associatedResource ?bfWork }` explicitly drops contained-work links — the right call for monographs but throws away the aggregation signal for the ~15% of the Helmet sample that carries one.

### Scope evidence (500-record sample probe)

| Signal | Records | % |
|---|---|---|
| 505 (contents note) | 101 | 20.2% |
| **ANY aggregating signal** | **75** | **15.0%** |
| ≥1 740 (variant title, often analytical) | 48 | 9.6% |
| ≥1 730 (uniform title, often analytical) | 39 | 7.8% |
| ≥2 730 (compilation pattern) | 32 | 6.4% |
| 245 with `$n` or `$p` (multipart) | 20 | 4.0% |
| ≥2 740 | 19 | 3.8% |
| 700 ind2=2 (analytical entry, strict-MARC) | 10 | 2.0% |
| LDR/07='m' monograph | 496 | 99.2% |

Note that **LDR/07 is not a useful signal in Helmet** — 99.2% of records are coded as monographs regardless of content shape. The aggregating signal is in the field-level analytics, not the leader.

The clearest demonstration record is **b10068004** (Audition songs for female singers): one 245 with sub-part designator, 9 distinct 700 composer entries, 9 distinct 730 uniform-title entries (one per song), 505 contents listing the same nine songs. Today this is typed as a plain `bffi:Work` with the 730 chain emerging as a flat `bf:hasSeries` list, losing the song-by-song component structure entirely.

### Why this matters

1. **Bibliographic fidelity.** A songbook isn't a monograph — modelling it as one collapses meaningful FRBR-level structure.
2. **Skosmos discoverability.** Once aggregating Expressions exist, Skosmos can render "this expression aggregates: …" panels and "this song is part of: …" reverse navigation. Cataloguer-review surfaces gain a meaningful "components" tab.
3. **M9 reconciliation precision.** A `Lloyd Webber, Andrew` agent on a 730 component shouldn't be reconciled with the same name on the parent's 700 as if they were the same primary contribution — they're contributions to *different* Works that happen to share an aggregator.
4. **Round-trip round-trip.** The current pipeline silently drops 730 / 740 contained-work links on Works that have them; round-trip diff shows them as lost rows with no recovery path.

## Approach

Five phases, each independently shippable and reversible.

### Phase A — Detection rule + Work/Expression typing

Add an M3 SPARQL branch (or a Python post-pass) that classifies a Work as aggregating when **any** of the following holds:

- `≥2 730` datafields in source MARC (compilation pattern)
- `≥2 740` datafields
- `≥1 700` with `ind2=2` (strict MARC analytical entry)
- `≥1 740` with `ind2=2`
- `245 $n` or `$a $p` (multipart designator)

Type the Work as `bffi:AggregatingWork` *and* (still) `bffi:Work` — `subClassOf` makes the second triple semantically redundant but keeps existing consumers walking `?w a bffi:Work` working. Similarly type the Expression as `bffi:AggregatingExpression` `bffi:Expression`.

Where the detection lives is open: SPARQL inside `bf_to_bffi_work.rq` is the cleanest if the COUNT-over-OPTIONAL aggregation pencils out; otherwise an M3 post-pass in `m3/post_process.py` walking the source graph.

**Acceptance**: on the 500-record sample, ~75 records (15%) emit `bffi:AggregatingWork` typing. b10068004 specifically is in the set. M3 / M8 / M9 integration tests still pass without modification (typing is additive).

### Phase B — `bffi:aggregates` / `bffi:aggregatedBy` edges + component Expressions

For each aggregating signal, mint a component Expression URI and emit:

```turtle
<aggregating-expr> a bffi:AggregatingExpression , bffi:Expression ;
                   bffi:aggregates <component-expr-1> ,
                                   <component-expr-2> ,
                                   … .

<component-expr-1> a bffi:Expression ;
                   bffi:aggregatedBy <aggregating-expr> ;
                   skos:prefLabel "I dreamed a dream"@en ;
                   bffi:title <component-title-1> .
```

Component minting strategy:

- **730 ind1=0** in source → one component Expression per source 730 row, prefLabel from `$a`.
- **740 ind1=any** → one component Expression per source 740 row when not already captured by a 730.
- **700 ind2=2** → one component Expression with a `bffi:contribution` link to the analytical entry's agent; the title typically lives in `$t` (uniform title subfield).

Component-Work URI scheme: `http://urn.fi/URN:NBN:fi:bib:work:<sha1(parent-bib + component-marc-key)>` — derivative of the parent's work URI so the component is greppable from the parent.

Open: should the round-trip converter walk `bffi:aggregates` *forward* (from parent to components, emit each component's 730/740 row) or *reverse* (from each component back to parent — slower but parallel-to-subject pattern)? Forward is the natural fit; revisit if memory pressure surfaces on the full 800k-record run.

**Acceptance**: b10068004 emits 9 component Expressions, each linked via `bffi:aggregates` from the parent Expression. The parent Expression's `bf:hasSeries` flat shortcut is retained for Skosmos compatibility. Validation: `bffi-pipeline q --query "SELECT ?p (COUNT(?c) AS ?n) WHERE { ?p bffi:aggregates ?c } GROUP BY ?p"` returns ~75 rows on the sample with plausible component counts.

### Phase C — Round-trip restoration of analytical entries

Today the round-trip converter has no path for emitting 700 ind2=2 / 740 / 730 from aggregation triples — every component is silently dropped. Wire:

- **`_emit_aggregated_components(record)`** new helper in `converter.py`, called from the existing emit chain.
- Walk `bffi:aggregates` on the Expression. For each component Expression with `bffi:contribution → bffi:agent` → emit 700 ind2=2 with the agent's name. Without a contribution → emit 730 ind1=0 or 740 (depending on which the source-side correlator tagged).
- Lineage: each component carries a `bffi-prov:fromMarcField` token pointing at its source 7XX row (P-50 reification pattern); the emit calls `lineage=self._lineage_token(component)`.

The choice of 730 vs 740 vs 700 ind2=2 on emission is driven by the component Expression's typing / which `bffi:contribution` it carries. P-50 Phase C's reification provides the source-MARC-field anchor.

**Acceptance**: b10068004 recon side regains its 9 730 ind1=0 rows + 9 700 ind1=1 composer entries. The current 9 + 9 = 18 "lost" rows for that record (today's diff residue, contained-work-filtered) drop to 0 with `$9 src=` lineage matching the source ordinal.

### Phase D — M9 reconciliation for component Expressions / Works

Today M9 walks `bffi:contribution → bffi:agent` on the canonical Work / Expression. With aggregation, components have their own `bffi:contribution` triples (a song's composer). M9 needs to reconcile those independently — `Lloyd Webber, Andrew` on a component is the same Andrew Lloyd Webber person whether it's a song component or a primary contribution, so the KANTO/finaf lookup should hit the same authority URI.

Concretely: `m9/requests.py` `_iter_creator_requests` walks all `bffi:Expression` subjects today (including the new `bffi:AggregatingExpression`s, which are also Expressions thanks to `subClassOf`). For each Expression's contributions, request reconciliation. Components inherit the existing walk for free if minted as `bffi:Expression` (or `bffi:AggregatingExpression` recursively, for nested aggregations).

What needs explicit work in M9:

- **Per-component request kind**: same `person` / `corporate_body` taxonomy applies; no schema change.
- **Per-component picker context**: the LLM picker prompt should know the *parent's* title for disambiguation (e.g. "this person is credited on a song in an English-language pop-music compilation"). New M9 candidate-context fetcher hook reads `?component bffi:aggregatedBy ?parent` and includes the parent's prefLabel in the prompt's situational context block.
- **Provenance**: each component gets its own `Reconciliation` Activity URI; the existing `prov:wasGeneratedBy` chain works unmodified.

**Acceptance**: an integration test seeds two records aggregating the same component (e.g. two compilations both containing "I dreamed a dream / Schönberg"); M9 reconciles both to the same finaf person URI without re-querying the authority for each occurrence (cache hit). M9 audit log carries one Reconciliation Activity per occurrence.

### Phase E — Cataloguer-review aggregation tab

Add a fifth tab to `gold/cataloguer-review.html`: "Aggregations". One row per aggregating Work, listing component title + component agent + reconciliation status. Lets a cataloguer eyeball whether components are correctly typed and bound. Builds on the existing bundle infrastructure — sidecar JSONL row per aggregating record, HTML reviewer renders it.

**Acceptance**: the 75 aggregating records in the sample populate the new tab. Per-record component counts match the source MARC's 7XX-2 / 730 / 740 counts.

## Phase commits + verification

After each phase ships, fill in the corresponding `Phase commit` line above with the merge commit hash. Verification per phase:

1. **Phase A**: SPARQL count probe + integration test asserting b10068004 emits `bffi:AggregatingWork` typing.
2. **Phase B**: query `SELECT … WHERE { ?p bffi:aggregates ?c }` against canonical.ttl + Fuseki round-trip.
3. **Phase C**: round-trip diff residue drop on the 75 aggregating records. Per-tag `lost` count for 700/730/740 drops materially (target: ≥80% reduction on aggregating-flagged records).
4. **Phase D**: M9 picker-cache hit rate on shared component agents (a Schönberg appearing in 3 records should cache once and be reused).
5. **Phase E**: visual inspection of the new tab in cataloguer-review HTML.

Run-wide end-of-Phase-E verification: `bffi-pipeline run` on the 500-record sample reports `aggregating_work_count` in `run.json` matching the Phase A probe.

## Migration / compatibility

- Phases A, B are additive — every existing consumer still walks `?w a bffi:Work` and gets the right answer (subclass semantics).
- Phase C changes round-trip output for the 75 aggregating records (more rows emitted). The 700/730/740 rows previously appeared as `lost`; after Phase C they appear as `identical` or `changed` (paired). Diff per_status counters move accordingly.
- Phase D changes M9 outputs for the same 75 records (more reconciliation Activities emitted). Provenance graph grows proportionally — ~10× on aggregating records, negligible corpus-wide given the 15% prevalence.
- Phase E changes only the cataloguer-review bundle (additive).

For Skosmos: no breaking changes. New typed Works are still surfaced via the existing `bffi:Work` vocab; the new `bffi:AggregatingWork` / `bffi:AggregatingExpression` resources gain their own Skosmos panel automatically if we add them to the Skosmos config — but that's a P-52 follow-on, not part of this plan.

For Fuseki: triple count grows by an estimated +10% on the 800k-record load (one Expression + one aggregation edge + one component prefLabel per aggregated component, ~10 components per aggregating record × 15% prevalence).

For the gold sets and eval harnesses: no input change required. Embedding / judge / picker evaluations still operate on Works; aggregating Works behave exactly like other Works.

## Risk register

- **Detection over-firing.** A record with `≥2 730` because of edition cross-references (not compilation) might be wrongly typed as aggregating. Mitigation: the detection rule is a disjunction of signals, not a single threshold; the cataloguer-review tab makes false positives visible per-record. Phase A acceptance includes a manual eyeball pass on the 75 flagged records.

- **Component URI collision across records.** Two different parents might mint the same component-Expression URI for "I dreamed a dream / Schönberg" (deterministic SHA-1 of parent-bib + marc-key would produce different URIs because the parent-bib differs). This is **intentional** — the same conceptual song appearing on two compilations is two `bffi:Expression`s of the same `bffi:Work` (M8 canonical Work mint should merge them). Risk is that M8's canonical Work miner doesn't recognise aggregated components as merge-eligible; mitigation: integration test on the seeded two-compilation scenario.

- **M9 picker prompt blow-up.** A compilation with 30 song components produces 30 picker requests; the picker prompt's situational context block (Phase D) carries the parent's title each time, adding tokens. Mitigation: the picker prompt is already templated and short; 30× a parent-title literal is bounded. Worst case: cap component reconciliations per parent at 50 with a "remaining components: skipped" audit row.

- **Cataloguer expectations.** Helmet cataloguers may not have a consistent convention for which 7XX shape to use for analytics. `_PREDICATE_BY_TAG`-style detection might miss conventions we haven't seen. Mitigation: ship Phase A with broad detection, narrow only on observed false positives.

- **Round-trip diff churn.** Phase C re-classifies many rows from `lost` to `identical`/`changed`. The cumulative diff residue numbers shift; the cataloguer-review tab presents the same shift as recovery. Mitigation: phase commit message documents the expected shift; baseline `summary.json` for the pre-Phase-C run gets archived alongside the plan.

- **Recursive aggregation.** A compilation can in theory itself contain compilations (anthology-of-anthologies). The data model handles this (`AggregatingExpression` is also `Expression`), but the converter's emit loop must not stack-overflow on a self-referential cycle. Mitigation: emit-side traversal carries a visited-set; cycle test in the unit suite.

## Rollback procedure

Each phase is reversible at the SPARQL / Python boundary without graph migration:

- **Phase A**: remove the typing emit from `bf_to_bffi_work.rq`. Existing consumers still walk `bffi:Work` correctly.
- **Phase B**: remove the component-Expression mint + `bffi:aggregates` emit. The flat `bf:hasSeries` triples we preserved for Skosmos are unchanged.
- **Phase C**: remove `_emit_aggregated_components` from the converter's emit chain. Recon side returns to today's behaviour (730/740/700-ind2-2 lost again).
- **Phase D**: revert M9 `requests.py` walking to skip Expressions whose `rdf:type` chain includes `bffi:AggregatingExpression` (or remove the aggregating-context picker hook). Phase D's M9 output is purely additive Activities; existing canonical bindings unchanged.
- **Phase E**: remove the aggregation tab + sidecar from `cataloguer-bundle.zip`.

After Phase D ships and the canonical graph carries component Expressions, a full rollback to pre-Phase-A would leave orphan triples in canonical (component Expressions with no aggregator). Acceptable — they're harmless to consumers — but a deliberate rollback should also strip them with a one-shot SPARQL DELETE pass.

## Verification (run-wide, end of Phase E)

- `runs/<id>/marc-roundtrip/summary.json` shows a measurable drop in `lost` rows for tags 700/730/740 (target: ≥80% drop on aggregating-flagged records).
- New `aggregating_work_count` field in `run.json` lists the count + per-record bib_ids for the operator to spot-check.
- Cataloguer-review HTML renders the new Aggregations tab with non-empty rows.

## Relationship to other plans

- **P-46 (`bffi:Item` class)** — same shape of work (adopt a BFFI ontology class we currently don't emit). P-46 is currently in `proposed/` pending a consumer ask; P-52 graduates directly to backlog because the round-trip diff residue gives us an immediate consumer (the 75 currently-mis-modelled records).
- **P-49 (BFFI structured fields vs marcKey)** — Phase B's component-Expression minter overlaps with P-49 Layer 1's `bf:partNumber` / `bf:partName` routing on 240/730/740. They co-evolve: Phase B emits component Expressions; P-49 Layer 1 routes the part subfields within each component's title.
- **P-50 (source-field provenance)** — every component Expression carries a `bffi-prov:fromMarcField` token from M2-post; Phase C's round-trip emit consumes those tokens for `$9 src=` lineage. P-50 Phase C's `rdf:Statement` reification is the prior art the aggregation edges follow.

## Suggested next step

Start Phase A: write the SPARQL / Python detection rule + add the typing emit, ship a small integration test against the b10068004 fixture, and confirm `bffi-pipeline q` on the post-run canonical shows the expected ~75 aggregating-Work rows on the 500-sample.
