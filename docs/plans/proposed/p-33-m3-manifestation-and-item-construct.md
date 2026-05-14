# P-33 — M3 Manifestation + Item CONSTRUCT passes

**Status**: proposed.
**Scope**: 2-3 days for an MVP that mints `bffi:Manifestation` from
the obvious BIBFRAME shape (one Manifestation per `bf:Instance`,
publication / extent / carrier / dimensions / identifier-by-ISBN) and
stays silent on `bffi:Item` until the Sierra export carries holdings;
1-2 weeks for the full surface (54 Manifestation predicates × 9 Item
predicates).

**Proposal-base commit**: `47477cd`. The "Motivation" reasons about
the M3 stage as of the post-P-32 scripts overhaul. If `main` moves
materially before this is acted on, re-verify with:

```
git diff 47477cd..HEAD -- \
    sparql/bf_to_bffi_work.rq \
    sparql/bf_to_bffi_expression.rq \
    src/bffi_pipeline/stages/bf_to_bffi.py \
    src/marcxml_export_pipeline/sierra/marcxml.py \
    docs/lkd.rdf
```

Cross-references:
- **P-07** (`p-07-bibframe-856-as-item.md`) — proposed to reclassify
  marc2bibframe2's MARC-856-lifted `bf:Instance` nodes as `bf:Item`.
  P-33 subsumes P-07's resolution: if P-33 ships, the 856 instances
  flow into the Manifestation CONSTRUCT pass naturally, and the
  "856 is really an Item" decision becomes a filter inside the
  Manifestation rq rather than an M2 post-processor patch. Either
  P-07 graduates first (and P-33 follows the established
  856-secondary handling) or P-33 graduates first (and P-07 is
  closed as superseded).
- **`docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 3** — the
  archived spec sketches Work + Expression CONSTRUCT but only
  mentions Manifestation/Item as out-of-scope-for-MVP. P-33 is the
  follow-on.

## Motivation

M3 today mints `bffi:Work` + `bffi:Expression` from each bib record
via two CONSTRUCT passes in `sparql/bf_to_bffi_{work,expression}.rq`.
Manifestation + Item — i.e. the *carrier* and the *holding* layers —
are dropped on the floor. The downstream consequences:

1. **Skosmos shows abstract works only.** A cataloguer can browse
   "Tolkien — The Hobbit" but not "Tolkien — The Hobbit, 1937 Allen &
   Unwin first edition" or "shelf-mark VAR 84.31 MORTON in the
   Pasila branch". For an OPAC-adjacent discovery layer that's a
   real surface-area gap; FRBR's whole pitch is that all four levels
   matter to readers.
2. **Reconciliation can't carry physical traits across mergers.**
   When M8 collapses two `bffi:Work` URIs into one canonical Work
   (same intellectual content, different translations) the
   *Manifestation* layer is where ISBN, extent ("256 sivua"),
   publisher ("Otava, 2013"), and binding ("kovakantinen") would
   accumulate as evidence that the two source records are distinct
   physical objects of the same Expression. Without Manifestation
   nodes, those signals are either lost or smuggled into the
   Expression — which the BFFI ontology rejects (the
   `rdfs:domain bffi:Manifestation` constraints on
   `bffi:editionStatement`, `bffi:extent`, `bffi:dimensions`, …
   make a SHACL-strict Expression carrying them invalid).
3. **No holdings story.** Item-level data (which branch holds which
   copy, its shelf-mark, its enumeration) is the bridge between the
   bibliographic graph and the library's operational state. Sierra
   carries this in its `bib_record_item_record_link` →
   `item_record` → `item_record_property` chain; the Sierra exporter
   currently does NOT propagate any of it into MARCXML. So even a
   well-written Item CONSTRUCT would fire on zero source triples
   today. Item support has two prerequisites — MARCXML and rq —
   not one.

The two-tier consequence: **Manifestation is achievable today
against the existing MARCXML; Item needs a Sierra-export extension
first.** That split shapes everything below.

## Approach

### Manifestation pass (`sparql/bf_to_bffi_manifestation.rq`)

One CONSTRUCT pass, structurally identical to
`bf_to_bffi_expression.rq`: a `?bfInstance a bf:Instance` outer match,
deterministic `bffi:Manifestation` URI minted via `arq:sha1`
(`http://urn.fi/URN:NBN:fi:bib:manifestation:<sha1(bf-instance-uri)>`),
and per-predicate `OPTIONAL` blocks routing the BIBFRAME source
shapes to the BFFI target predicates. The Manifestation IRI is then
attached to the existing Expression via `bffi:manifestationOf` (the
inverse of `bffi:expressionManifested`).

The MVP routes:

| BFFI target | BIBFRAME source | MARC source (informational) |
|---|---|---|
| `bffi:Manifestation` (rdf:type) | `bf:Instance` | every bib record |
| `bffi:expressionManifested → bffi:Expression` | `bf:instanceOf → bf:Work` (rewritten to the Expression URI minted in pass 2) | — |
| `bffi:provisionActivity` (incl. blank-node typed `bf:Publication` / `bf:Manufacture` / `bf:Production` / `bf:Distribution` with `bf:agent` + `bf:date` + `bf:place`) | `bf:provisionActivity ?pa . ?pa a bf:Publication ; bf:agent ?ag ; bf:date ?d ; bf:place ?pl` | **260** / **264** |
| `bffi:publicationStatement` (literal) | `bf:publicationStatement` | **260$a$b$c**, **264 ind2=1** |
| `bffi:manufactureStatement` (literal) | `bf:manufactureStatement` | **264 ind2=3** |
| `bffi:distributionStatement` (literal) | `bf:distributionStatement` | **264 ind2=2** |
| `bffi:productionStatement` (literal) | `bf:productionStatement` | **264 ind2=0** |
| `bffi:copyrightDate` (literal) | `bf:copyrightDate` | **264 ind2=4** |
| `bffi:editionStatement` (literal) | `bf:editionStatement` | **250$a** |
| `bffi:responsibilityStatement` (literal) | `bf:responsibilityStatement` | **245$c** |
| `bffi:extent` (typed value) | `bf:extent ?e . ?e rdfs:label ?lbl` | **300$a** |
| `bffi:dimensions` (literal) | `bf:dimensions` | **300$c** |
| `bffi:carrier` (URI to `<http://id.loc.gov/vocabulary/carriers/*>`) | `bf:carrier` | **338$a$b** |
| `bffi:media` (URI) | `bf:media` | **337$a$b** |
| `bffi:issuance` | `bf:issuance` | **leader/07**, **leader/19** |
| `bffi:seriesStatement` | `bf:seriesStatement` | **490$a** |
| `bffi:tableOfContents` | `bf:tableOfContents` | **505$a** (rolled in here — currently dropped in expression.rq with a comment noting domain mismatch) |
| `bffi:note` | `bf:note ?n . ?n rdf:value ?val` | **500**, **504**, **520**, **546** (existing M3 routes these to Expression — review which belong here vs there) |
| `bf:identifiedBy` (typed `bf:Isbn`) | `bf:identifiedBy ?i . ?i a bf:Isbn ; rdf:value ?v` | **020$a** |
| `bf:identifiedBy` (typed `bf:Issn`) | `bf:identifiedBy ?i . ?i a bf:Issn` | **022$a** |
| `bf:identifiedBy` (typed `bf:Local`, `bf:source = helmet`) | `bf:identifiedBy ?i . ?i bf:source <…helmet>` | **907**/**001**+**003** (propagated by the Sierra exporter to the main `bf:Instance` today) |
| `skos:prefLabel` | `bf:title ?t . ?t bf:mainTitle ?label` | **245$a$b** |
| `bffi:marcKey` | `bflc:marcKey` | — |

That's ~22 predicates of the 54 BFFI Manifestation surface — covers
the dominant Helmet-cataloguing patterns. The remaining 32
(`bffi:bookFormat`, `bffi:fontSize`, `bffi:soundCharacteristic`,
the 12 serial-publication predicates around `firstIssue` /
`lastIssue`, etc.) ride on rare MARC fields and can land as Phase B
once a real corpus run surfaces frequency-of-fire data.

The Manifestation pass parallels the existing Expression pass in
operating against the SAME `?bfWork → ?bfInstance` chain. The Work
URI is needed because the Expression URI (`?exprURI`) is derived
from it via the existing `arq:sha1(STR(?bfWork))` mint; the
Manifestation URI is derived from `?bfInstance` so multiple
manifestations per work (paperback + ebook + audiobook) yield
distinct URIs even when the Helmet bib is single-Instance.

### Item pass (`sparql/bf_to_bffi_item.rq`)

Item lives below Manifestation: shelf-mark, holding library,
enumeration ("vol. 2"), sub-location (the shelf within the branch).
The CONSTRUCT shape mirrors the Manifestation pass:

| BFFI target | BIBFRAME source | MARC source |
|---|---|---|
| `bffi:Item` (rdf:type) | `bf:Item` | **852** + **876** + **877** |
| `bffi:itemOf → bffi:Manifestation` | `bf:itemOf → bf:Instance` | — |
| `bffi:heldBy` (URI to an `<http://urn.fi/URN:NBN:fi:bib:agent:org:helmet:<branch-code>>` org IRI) | `bf:heldBy ?org . ?org rdf:value ?code` | **852$a**, **852$b** |
| `bffi:shelfMark` (literal) | `bf:shelfMark ?sm . ?sm rdf:value ?v` | **852$h**, **852$k** |
| `bffi:sublocation` | `bf:sublocation` | **852$c** |
| `bffi:enumerationAndChronology` | `bf:enumerationAndChronology` | **863-866** |
| `bffi:physicalLocation` | `bf:physicalLocation` | **852$j** |
| `bffi:custodialHistory` | `bf:custodialHistory` | **541** |
| `bffi:holdingOf` (subPropertyOf `bffi:itemOf`) | derived | — |
| `bffi:immediateAcquisition` | `bf:immediateAcquisition` | **541** |

But the rq is **useless against today's MARCXML.** Helmet's Sierra
export currently drops every 852 / 876 / 877 (the "Holdings Data,
Holdings Statement, Textual Holdings" fields) because the
`marcxml_export_pipeline.sierra` exporter only walks
`bib_record_property` + `bib_record_subject` + `varfield`, not the
`item_record_property` chain. So Item is a **two-side change**: the
exporter has to emit one 876 datafield per item row (with $a =
Sierra item-record-id, $h = call number, $p = barcode, $l =
branch-code, etc.), AND the rq needs to fire on those datafields.

### Stage wiring (`stages/bf_to_bffi.py`)

The runner reads `bf_to_bffi_work.rq` and `bf_to_bffi_expression.rq`
today, executes both CONSTRUCTs, and unions their output graphs into
one `<id>.ttl` per record. Extending to a four-rq runner is
mechanical:

- Add a `_SPARQL_QUERIES: Final[tuple[Path, ...]]` that lists the
  four rq paths in execution order
  (Work → Expression → Manifestation → Item). Order matters only for
  failure-attribution clarity in logs; the graphs union commutatively.
- Carry the per-pass timing in the existing summary surface (P-11
  observability already tracks per-stage events; per-CONSTRUCT
  events would be a finer-grained event with `stage=m3-manifest` /
  `stage=m3-item`).
- Add `--skip-manifestation` / `--skip-item` flags so the operator
  can run the Work + Expression subset for backwards-compatible
  comparisons during validation.

### Validation gate

Boundary 2 (BIBFRAME → BFFI) — the SHACL shape at
`config/shapes/bibframe-conversion.shape.ttl` — must grow targets for
the new Manifestation + Item classes:

- Every `bffi:Manifestation` carries exactly one
  `bffi:expressionManifested` linking to a valid Expression URI.
- Every `bffi:Item` carries exactly one `bffi:itemOf` linking to a
  valid Manifestation URI.
- The `bf:identifiedBy → bf:Isbn` triple has `rdf:value` matching
  the ISBN-10 / ISBN-13 regex (catches the "subfield 020$a contained
  '9789137139791 (hårda pärmar)'" gotcha where the binding hint is
  not stripped before mint).

## What must be present on the MARCXML

### Manifestation pass — coverable today

The Helmet Sierra export already carries the fields the Manifestation
CONSTRUCT needs:

| MARC field | Subfields | Already exported? | Routes to |
|---|---|---|---|
| **020** | $a (ISBN), $q (binding hint) | yes | `bf:identifiedBy → bf:Isbn → rdf:value` |
| **022** | $a (ISSN) | yes | `bf:identifiedBy → bf:Issn` |
| **028** | $a (Publisher number), $b (Source) | yes | `bf:identifiedBy → bf:Identifier` (varied subtype) |
| **245** | $a (Title), $b (Remainder), $c (Statement of resp.) | yes | `bffi:responsibilityStatement` + Expression title |
| **250** | $a (Edition statement) | yes | `bffi:editionStatement` |
| **260** | $a (Place), $b (Publisher), $c (Date) | yes (older records) | `bffi:provisionActivity` |
| **264** | $a / $b / $c with ind2 0/1/2/3/4 | yes (newer records) | `bffi:provisionActivity`, typed by ind2 |
| **300** | $a (Extent), $b (Other physical details), $c (Dimensions), $e (Accompanying material) | yes | `bffi:extent`, `bffi:dimensions`, `bffi:supplementaryContent` |
| **336** | $a / $b / $2 (Content type) | yes | (already routed by M3 to Expression) |
| **337** | $a / $b / $2 (Media type) | yes | `bffi:media` |
| **338** | $a / $b / $2 (Carrier type) | yes | `bffi:carrier` |
| **490** / **830** | $a (Series statement / tracing) | yes | `bffi:seriesStatement` |
| **500-546** | $a (Notes) | yes | `bffi:note` — review which routes here vs Expression |
| **505** | $a (Table of contents) | yes | `bffi:tableOfContents` |
| **leader/07**, **leader/19** | — | yes | `bffi:issuance` |

No exporter change needed for the Manifestation pass — every field
above is already in the per-bib MARCXML the M2 stage reads.

### Item pass — exporter change required

Helmet's Sierra schema stores holdings in `item_record` +
`item_record_property` (one row per copy/barcode), keyed off the bib
via `bib_record_item_record_link`. The current exporter (`src/
marcxml_export_pipeline/sierra/marcxml.py`) walks bib-level varfields
only — item rows are dropped before MARCXML serialisation.

For the Item rq to fire, the exporter needs to:

1. **Add a SELECT** that joins `bib_record_item_record_link` →
   `item_record` → `item_record_property` → `varfield (item rows)`
   keyed by bib id.
2. **Emit one MARC 876 datafield per item row**, with subfields:
   - `$a` — Sierra `item_record_id` (the stable internal id).
   - `$h` — call number / shelf-mark (`item_record_property` row
     with `iii_property_id` mapped to "CALL NO." — Sierra-specific
     mapping table; cataloguer confirmation needed).
   - `$j` — copy number (if present).
   - `$l` — location-code (`item_record.location_code`; needs the
     branch-name lookup table — the same one the Helmet OPAC uses).
   - `$p` — barcode (Sierra `item_record.barcode`).
   - `$t` — item status code (CHECKEDIN / CHECKEDOUT / etc.).
3. **Emit one MARC 852** at bib level summarising the holding
   (location-code aggregation) so records with N copies don't need
   the consumer to walk N×876s for the basic "what library has this"
   answer.
4. **Honour the "suppressed" flag** — Sierra carries an
   `is_suppressed` bit per item; suppressed items shouldn't appear
   in public-facing RDF. The existing bib-level suppression filter
   is the precedent; mirror it for items.

Open question — see "Open questions" — is whether the marc2bibframe2
XSLT v3.1.0 actually lifts MARC 876 into `bf:Item` triples cleanly,
or whether an M2 post-process step is needed (parallel to the
existing M2 post-process for `bflc:PrimaryContribution`).

### Helmet branch-code → IRI table

Item.heldBy needs a stable URI per branch. The current pipeline
has no branch vocab. P-33 needs to mint one:

- Filename: `config/vocabs/helmet-branches.ttl` (new file).
- One `bffi:Library` (or `bf:Library`) per code, with `skos:prefLabel`
  in `fi`/`sv`/`en` and `skos:notation` carrying the Sierra
  location-code.
- ~50 branches at full Helmet scale; small enough to hand-curate
  from the OPAC branch list.

The exporter then writes `$l 1pa` (Pasila); the rq dereferences
`<http://urn.fi/URN:NBN:fi:bib:agent:org:helmet:1pa>` and binds it
as the `bffi:heldBy` object. The vocab loads into Fuseki alongside
the Finto dumps.

## Prerequisites

- **P-30** (observability audit) clears the gate — same gating
  sequence as P-22-29 per `docs/plans/proposed/README.md` § Gating
  sequence. Item-level data multiplies the per-record triple count
  by the average copies-per-bib (~3 for Helmet); we need to trust
  the observability layer's "M3 wrote N triples for run X" surface
  before doubling the volume.
- **P-15 / P-16 lessons absorbed.** P-15 caught a class of M3
  authority-URI bugs where the CONSTRUCT bound a literal where it
  should have bound an authority IRI. Manifestation has more
  authority-IRI hops (carrier vocab, media vocab, branch vocab) and
  is more likely to repeat that class of bug; the proposal's rq
  examples should be cross-checked against the P-15 fix pattern
  before drafting.
- **A 200-record cataloguer-curated audit** of what the post-M3
  Manifestation graph looks like for a representative sample. The
  cataloguer should sign off on field-by-field routing
  (260→provisionActivity is uncontroversial; 246 alternate-title
  routing to Manifestation vs Expression is genuinely ambiguous).
- **Sierra exporter access.** The Item half of the proposal needs
  someone with read access to the Sierra `item_record` schema to
  validate the proposed SELECT joins; the schema isn't public.

## Risks

- **R1 — Manifestation/Expression ambiguity for translation
  records.** Helmet records carrying both an "original title" (240)
  and a "translated title" (245) currently produce one Work + one
  Expression. Under P-33 they produce one Work + one Expression +
  one Manifestation — fine for the translated edition. The original
  is *not* a separate Expression in this model; readers of the
  graph have to understand that "originalLanguage" lives on the
  Work + the `bf:translationOf` link is implicit. May surface as
  cataloguer confusion. Mitigation: doc note in
  `docs/external-dependencies.md` for the cataloguer engagement
  before shipping; defer 240→original-Work minting to a follow-on
  proposal if it surfaces as a real ask.
- **R2 — Per-record triple-count inflation.** Manifestation adds
  ~10-20 triples per bib (provisionActivity + extent + carrier +
  identifiers + statements). Item adds ~5-15 triples × average 3
  copies/bib = ~15-45 more per bib. M8 corpus-load was the bottleneck
  the P-19 work just fixed; doubling the per-bib triple count
  re-pressures that path. Mitigation: pre-bench against the 5 k
  sample before committing to a full-corpus run; if M8 wall-time
  regresses materially, the rq can be subset behind a
  `BFFI_M3_MANIFEST_LEVEL` env knob.
- **R3 — Item suppression filter must match bib suppression
  semantics exactly.** A bib with three items, two suppressed and
  one visible, must produce one `bffi:Item` (the visible one), not
  zero (over-filter) or three (under-filter). The exporter's
  existing bib-level suppression is the canonical reference;
  property-test the item filter against it.
- **R4 — Carrier/media vocab IRIs may not match
  `<http://id.loc.gov/vocabulary/carriers/*>`.** marc2bibframe2's
  current behaviour on these vocabs is to emit a blank-node with
  `rdfs:label` rather than the LoC IRI — Cataloguer-side $2 = rdacarrier
  isn't always present, especially on older Helmet records. The rq
  needs a `COALESCE(?carrierIRI, ?carrierLabel)` fallback (mirroring
  the P-15 fix on subjects) so unauthored carrier text doesn't drop
  on the floor.
- **R5 — 856-as-Item collision with P-07.** Both proposals touch
  the bf:Instance → bffi mapping. If P-07 graduates first, P-33's
  Item rq has to filter out the 856-derived Instances (otherwise
  they double-count: once as Item via 856 reclassification, once as
  Item via 876). If P-33 graduates first, P-07 closes as
  superseded. Either way, the proposals can't ship independently
  without a coordination note.
- **R6 — Validation explosion.** SHACL boundary-2 shape additions
  for Manifestation + Item will roughly double the shape count.
  rdflib's SHACL validator at the existing per-record loop is
  already a noticeable fraction of M3 wall-time. Pre-validate the
  shape set's load cost on the 5 k bench.

## Open questions

- **Should 856 graduate to Item now (P-07 path) or stay as
  Manifestation (`bf:Instance`) until P-33 takes it over?** The
  proposals are coupled but the resolution isn't obvious without
  cataloguer input. Default: P-33 graduates first; P-07 closes
  superseded.
- **`bf:tableOfContents` routing — Expression or Manifestation?**
  The BFFI ontology has `rdfs:domain bffi:Manifestation` (per
  `docs/lkd.rdf` line 3995, cited in the existing
  `bf_to_bffi_expression.rq` comment block). But the contents-list
  is intellectual content, not carrier — RDA/FRBR purists put it on
  Expression. P-33 mints it on Manifestation per the BFFI domain
  declaration; if cataloguer pushback materialises, BFFI ontology
  amendment is the right fix (out of scope for this pipeline).
- **246 alternate-title routing.** Goes to Expression (varying-title
  variants of the work) or Manifestation (varying-title variants of
  the published edition)? Defer to cataloguer.
- **`bffi:provisionActivity` as a sub-graph or flat literals?**
  BFFI supports both shapes — the rich shape is `bffi:Manifestation
  bffi:provisionActivity [a bf:Publication ; bf:agent ?ag ; bf:date
  ?d ; bf:place ?pl]` with structured sub-objects; the literal shape
  is `bffi:publicationStatement "Stockholm : Forum, 2013"`. Phase A
  ships the literal shape (matches the existing
  `bf:publicationStatement` triple verbatim, low conversion risk);
  Phase B layers in the structured shape when there's an downstream
  consumer (Skosmos-side facets, e.g.).
- **Holdings-volatility refresh strategy.** Item-level state changes
  hourly (checkouts, returns, item-record creations). The current
  pipeline runs the full M2 → load chain at human timescales. If
  Item is in the published graph, the operator needs a "refresh
  items only" lightweight path — or accepts that the published
  graph is yesterday's snapshot. Out of scope for P-33 to design,
  but worth surfacing now so the proposal isn't blindsided when
  Helmet asks "can we re-publish daily?".
- **What happens to the existing `data/canonical.ttl` /
  `canonical-reconciled.ttl` schema?** M8 + M9 currently union Work
  + Expression URIs and emit `bffi:adminMetadata` for each canonical
  Work. With Manifestation + Item in the graph, the M8 merge logic
  needs to decide whether Manifestation participates in the merge
  group (probably yes: ISBN evidence is the cleanest "these are the
  same edition" signal) and whether Items inherit canonical-Work
  URIs (probably no: items belong to manifestations one-to-many).
  Worth a follow-on M8-design proposal before P-33 graduates, or at
  minimum a deferred-question note in the plan-shape rewrite.
- **Bench cadence.** Should Phase A (Manifestation MVP) ship behind
  a dev-sample-only smoke before merging, or against the curated 13
  + a 200-record stratified sample? The 19-record 2026-05-13 audit
  caught the P-15 651 case from a single record — favours smaller
  sample first.
