# P-46 — BFFI Item class: holdings layer for the 4-class ontology

**Status**: proposed, **deferred**. Data prerequisite is satisfied (per-Item rows are present in Helmet's MARCXML export), but no concrete downstream consumer has materialised — see "Why deferred / activation criteria" below.

**Scope**: 2-3 weeks if a consumer surfaces. Roughly: M2 post-process (or m2bf XSL wrapper) for 876/852 → `bf:Item`, new M3 CONSTRUCT pass (`sparql/bf_to_bffi_item.rq`), SHACL shape additions, branch-vocab mint, Skosmos sibling vocab, and integration smoke. Optional but tightly coupled: 856-demotion rewrite (the unfinished half of [p-33](p-33-m3-manifestation-and-item-construct.md) Phase B).

**Proposal-base commit**: `5ba91ee` (last commit of the P-45 chain that established the 3-class baseline; this proposal layers the 4th class on top of it).

Supersedes the Item half of [p-33-m3-manifestation-and-item-construct.md](p-33-m3-manifestation-and-item-construct.md). The Manifestation half of that proposal shipped via P-45.

## Motivation

The BFFI 1.0.0 ontology has four FRBR/LRM tiers — Work / Expression / Manifestation / Item. As of P-45 the pipeline emits a faithful 3-class graph; the 4th class (`bffi:Item`) is the per-physical-copy layer: barcode, shelf-mark, branch (`bffi:heldBy`), enumeration ("vol. 2"), sub-location, custodial history. It's the bridge between the bibliographic graph and the library's operational state — "which library has this; on which shelf".

What Item-class modelling would unlock:

1. **Holdings federation.** A cross-Helmet view of where every copy of a Work lives, queryable as RDF. Skosmos's "browse by holding library" path becomes possible.
2. **Branch-level Skosmos navigation.** A new `:bffiItems` sibling vocab (mirroring `:bffiWorks`/`:bffiExpressions`/`:bffiManifestations` from P-45 commit 5), URL-routed via `void:uriSpace = http://urn.fi/URN:NBN:fi:bib:item:`. Cataloguers click an item, see its branch + shelf-mark, walk back up the chain to the canonical Work.
3. **Circulation dashboards.** Item carries loan-state via the existing `item_record.item_status_code` Sierra column. A consumer that watches "what's checked out / overdue / unavailable per Work" needs the Item URIs as join keys.
4. **Branch-level holdings reports.** Per-branch coverage analytics ("how many copies of Tolkien does Pasila hold vs Kallio").

None of these have a named consumer today. The user has confirmed this is a deliberate prioritisation defer, not a data-blocked one (see [[item-level-deferred]] memory note: "data is in the MARCXML export but the 4th BFFI class is intentionally not modelled until a concrete consumer asks for it").

## Why deferred / activation criteria

This proposal is parked rather than rejected. It activates when **any** of these surfaces:

- A cataloguer-side request for branch-level Skosmos browsing.
- A holdings-federation initiative at the Helmet consortium level (cross-branch coverage analytics, OPAC alternative, etc.).
- An external partner (academic library, archive) asking for Items as RDF.
- A circulation-side use case that wants Item URIs as join keys against the operational Sierra database.

Until then, the cost-benefit doesn't pencil out: Item adds ~5-15 triples × average 3 copies/bib (~15-45 per bib) over an 800k corpus = ~12-36 million extra triples in Fuseki, plus a Sierra-export schema change, plus a branch-vocab to maintain, plus per-shape SHACL validation cost — without a consumer that visibly benefits.

The data side is **not** the constraint:

- Helmet's MARCXML export already carries per-Item rows (the `helmet-sierra-data-tools` extract walks `bib_record_item_record_link → item_record → item_record_property`). The 876 / 852 datafields the rq would need are emitted with each per-Item row.
- The marc2bibframe2 v3.1.0 XSL lifts MARC 876 into `bf:Item` triples (per the LoC test suite, though we'd want to verify on real Helmet records before committing).

So the implementation gap is purely on the BFFI / pipeline side — the upstream rails exist.

## Approach (sketch — not committed-to-execution yet)

### Item rq (`sparql/bf_to_bffi_item.rq`)

Pattern mirrors the existing P-45 Manifestation rq: a `?bfItem a bf:Item ; bf:itemOf ?bfInstance` outer match, deterministic URI mint via `arq:sha1(STR(?bfItem))` against the `http://urn.fi/URN:NBN:fi:bib:item:` namespace (parallel to the Manifestation namespace committed in P-45 commit 1), and OPTIONAL blocks routing each predicate:

| BFFI target | BIBFRAME source | MARC source |
|---|---|---|
| `bffi:Item` (rdf:type) | `bf:Item` | **876** + **852** + **877** |
| `bffi:itemOf → bffi:Manifestation` | `bf:itemOf → bf:Instance` | derived |
| `bffi:heldBy` (URI to Helmet branch IRI) | `bf:heldBy ?org . ?org rdf:value ?code` | **852$a$b** |
| `bffi:shelfMark` (literal) | `bf:shelfMark ?sm . ?sm rdf:value ?v` | **852$h$k** |
| `bffi:sublocation` | `bf:sublocation` | **852$c** |
| `bffi:enumerationAndChronology` | `bf:enumerationAndChronology` | **863-866** |
| `bffi:physicalLocation` | `bf:physicalLocation` | **852$j** |
| `bffi:custodialHistory` | `bf:custodialHistory` | **541** |
| `bf:identifiedBy` (typed `bf:Barcode`) | `bf:identifiedBy ?i . ?i a bf:Barcode` | **876$p** |

That's ~9 predicates of the BFFI Item surface (9 declared in `docs/lkd.rdf`); the rest (`bffi:itemPortion`, etc.) ride on rare MARC patterns and land in a Phase B once corpus-frequency data is available.

### Branch vocab (`config/vocabs/helmet-branches.ttl`)

`bffi:heldBy` needs a stable URI per branch. Mint one SKOS file:

- One `bffi:Library` (or `bf:Library` — TBD against `docs/lkd.rdf`) per Sierra location-code, with `skos:prefLabel` in `fi`/`sv`/`en` and `skos:notation` carrying the code.
- ~50 branches at full Helmet scale; hand-curated from the OPAC branch list.
- Loaded into Fuseki under graph `http://urn.fi/URN:NBN:fi:bib:agent:org:helmet/` alongside the Finto vocabs.

The 876 `$l` cataloguer-typed branch code (`1pa` = Pasila) resolves to `<http://urn.fi/URN:NBN:fi:bib:agent:org:helmet:1pa>` for the `bffi:heldBy` object.

### Skosmos sibling vocab (`:bffiItems`)

One more entry in `config/skosmos-config.ttl` (mirroring the three siblings P-45 commit 5 added):
- `void:uriSpace = "http://urn.fi/URN:NBN:fi:bib:item:"`
- `skosmos:sparqlGraph` = same `bffi-works` named graph (Items live in the same connected RDF graph as Works/Expressions/Manifestations).
- `skosmos:indexShowClass = bffi:Item`.
- Plus `bffi:Item rdfs:label "Item"@en, "Kappale"@fi, "Exemplar"@sv ; rdfs:subClassOf skos:Concept` alongside the existing three type labels.

### SHACL boundary-3 additions

`config/shapes/bffi.shape.ttl` grows a `bffi:ItemShape` requiring `bffi:itemOf → bffi:Manifestation` (exactly one) and disjointness from Work/Expression/Manifestation. Parallel to the Manifestation shape committed in P-45 commit 2.

### M2 post-process for 876 / 852

Open question (carried forward from p-33): does marc2bibframe2's XSL emit `bf:Item` triples cleanly when fed 876 + 852, or does the M2 post-processor need a step parallel to the existing `bflc:PrimaryContribution` patch? Worth a 5-record smoke against real Helmet records as the first verification step when this is picked up.

### 856-demotion (tightly coupled)

The unfinished half of p-33 Phase B: rewriting MARC-856-derived `bf:Instance` nodes as `bf:Item` (access-point semantics) rather than separate Manifestations. If activation surfaces a consumer that cares about the Item layer at all, the 856 question becomes load-bearing — most Helmet 856 content is a publisher / web-copy URL (genuine access point), not a separate manifestation. Sub-options + rationale already documented in [p-33](p-33-m3-manifestation-and-item-construct.md) "MARC 856 — special case"; the same three-option fork applies here verbatim.

## Prerequisites

- A consumer with concrete requirements (see activation criteria). Without one, scope-creep is inevitable — Item adds enough surface area that "do it right" requires consumer-side feedback on what predicates matter.
- Verification that marc2bibframe2's XSL actually emits `bf:Item` for 876+852 on real Helmet records (5-record smoke).
- Cataloguer-confirmed branch-code → human-name table (the ~50 Helmet branches).
- Sierra-side confirmation of the suppression-flag semantics for items (mirror of bib-level `is_suppressed`).

## Risks

- **R1 — Holdings volatility.** Item-level state (loan status, item-record creations, transfers between branches) changes hourly. The current pipeline runs at human timescales. If Items land in the published graph, the operator needs either a "refresh items only" lightweight path OR an explicit "this is yesterday's snapshot" disclaimer. Worth designing alongside the consumer ask, not in advance.
- **R2 — Triple-count inflation.** ~12-36 million extra triples in Fuseki over 800k bibs. The M8 corpus-load was already a bottleneck (fixed in P-19); doubling per-bib triple counts re-pressures that path. Pre-bench against the 5k sample is mandatory.
- **R3 — Suppression filter must match bib-level semantics exactly.** A bib with three items, two suppressed and one visible, must produce one `bffi:Item`. Property-test against the existing bib-level suppression filter.
- **R4 — 856 + Item interact.** If Item lands without resolving the 856-demotion question, MARC-856-derived `bf:Instance` nodes coexist with `bf:Item` nodes in the graph with overlapping access-point semantics. The 856-demotion design choice can't be deferred any longer once Item is in scope.
- **R5 — Branch vocab governance.** Helmet's branch list changes occasionally (mergers, new locations). The hand-curated `helmet-branches.ttl` needs a clear update path — who owns it, how it ages — before it goes into production.
- **R6 — M8 merge semantics.** Today M8 merges canonical Works on cross-Expression evidence. With Items in the graph, do Items inherit canonical-Work URIs (probably no — they belong to Manifestations one-to-many) and does Manifestation participate in the M8 merge group (probably yes — ISBN evidence is the cleanest "same edition" signal)? Worth a follow-on M8 design proposal before this graduates.

## Open questions

- Does marc2bibframe2 v3.1.0's XSL produce `bf:Item` cleanly for Helmet 876+852, or is an M2 post-process step needed?
- Branch-vocab governance: who owns updates to `helmet-branches.ttl`?
- Holdings-refresh cadence: nightly differential? full re-load on each pipeline run? consumer-side delta queries?
- M8 merge semantics when Manifestation + Item are in scope.
- 856-demotion choice (the three sub-options from p-33 still apply).
- Whether `bffi:Library` is a real class in `docs/lkd.rdf` or whether `bffi:heldBy` ranges over `bf:Agent` (lkd.rdf check before drafting the branch vocab).

## Suggested next step

If a consumer materialises: graduate this proposal into `docs/plans/backlog/p-46-bffi-item-class.md` with phased verification checkpoints (Phase A: 5-record smoke against m2bf XSL output for 876/852; Phase B: branch-vocab + Item rq + shape; Phase C: M8 merge-implication design; Phase D: Skosmos sibling + integration smoke; Phase E: 856-demotion follow-up).

Until then: leave this proposal in place as the on-the-record answer to "what about Items?" — the data is available, the constraint is consumer-pull.
