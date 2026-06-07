# P-50 — Source-field provenance: ground-up lineage redesign

**Status**: backlog. Graduated from proposed 2026-06-07; Phase A starts immediately.

**Scope**: redesign of lineage tracking across the full pipeline (M2 → M3 → M8 → M9 → round-trip converter → diff). Estimated 1-2 weeks. Supersedes P-48's two-phase scheme — the current `$9 src=<tag>-<ord>` token format becomes a recon-time **emission** of the new graph-level provenance, not a parallel mechanism.

**Plan-base commit**: `0775675` (P-49 Phase A + Layer 1 partNumber/partName landing). The provenance redesign sits on top of the now-stable `marckey-bypass` audit so the lineage work doesn't tangle with structured-field auditing. Before starting any phase, run `git diff 0775675..HEAD -- src/bffi_pipeline/stages/m2/ src/bffi_pipeline/stages/m3/ sparql/bf_to_bffi_*.rq src/bffi_pipeline/marc_roundtrip/ docs/lkd.rdf` to confirm no in-flight work has reshaped the relevant surfaces.

**Phase commits**:

- Phase A (M2-post correlator for marcKey-bearing fields): shipped 2026-06-07 (commit pending)
- Phase B (flat literal + provision-activity coverage): shipped 2026-06-07 (commit pending)
- Phase C (SubjectLink reification for URI-keyed subjects): shipped 2026-06-07 (commit pending)
- Phase D (legacy fallback removal): pending — gated on verification pipeline run showing 100 % token coverage on the 500-record sample.

**Owner**: Mikko, by default.

## Implementation note (post-graduation)

The Phase C plan below describes a triplet of locally-minted BFFI terms (`bffi:SubjectLink` / `bffi:hasSubjectLink` / `bffi:subjectTarget`) as Option L1, with `rdf:Statement` reification listed as Option L3 with a "tooling risk" caveat. **At implementation time we reversed that decision** and shipped Option L3 instead — W3C-standard `rdf:Statement` reification works fine in our rdflib + Fuseki + Skosmos stack, and avoids growing the BFFI namespace with a private extension for a problem RDF already solves. The on-the-wire shape is:

```turtle
<stmt> rdf:type rdf:Statement ;
       rdf:subject   <work-uri> ;
       rdf:predicate bffi:subject ;
       rdf:object    <target-uri> ;
       bffi-prov:fromMarcField "<bib>:<tag>:<ord>" .
```

Statement URIs are minted per-record per-occurrence at
`http://urn.fi/URN:NBN:fi:bib:subject-statement:<bib>:<tag>:<ord>`. Functionally identical to L1 (each occurrence gets its own anchor), zero new BFFI terms.

Related: P-50 also moved `bffi:sourceMetadata` (AdminMetadata → source-record pointer) to standard `prov:hadPrimarySource`, and `bffi:syntheticSentinel` (B3 anonymous-agent flag) to `bffi-prov:syntheticSentinel` since the flag is pipeline-internal metadata, not a bibliographic property. The Python attribute names in `bffi_pipeline.provenance.vocab` are retained so call sites don't churn.

## Motivation

Today's lineage scheme — committed in P-48 Phase A, exercised in production for the last week — has revealed three structural shortcomings while solving the problem it was designed for. The b10642122 case from run `20260607-0622-dfc03f` is the clearest demonstration:

Source bib `b10642122` has eleven 650 datafields. Nine carry `$0 <yso-uri>` so marc2bibframe2 emits them as `<bf:Topic rdf:about="<yso-uri>">` direct, with **no M3 ordinal**. Two carry no `$0` and get raw URIs `#Topic650-27` / `#Topic650-31`. Recon side:

- The 2 raw-URI subjects get lineage tokens `650-1` / `650-2` (rank 1 and 2 in the 2-element raw bucket).
- The 9 URI-keyed subjects get **no lineage at all**.

Diff comparator: ranks all 11 source 650s 1..11 and tries to pair by lineage first, then by position. The 2 lineage tokens `650-1` / `650-2` claim source positions 1 and 2 — but source-1 is `yhteiskuntafilosofia` and the raw recon `650-1` is `sivilisaatio` (M3-ord 27, source position 5). All 11 pairings are wrong; the diff shows 11 spurious `changed` rows.

Aggregated across the 500-record sample: **444 row-level mispairings on 650 alone**, plus an unknown number on 651/600/700/etc. The cataloguer-review view becomes uninformative for these fields.

### Why patching P-48 doesn't suffice

The natural patch — "hash the authority URI when present, fall back to position otherwise" — solves this specific shape. But the same family of bugs lurks wherever marc2bibframe2 routing makes ad-hoc decisions about which fields get raw URIs and which get shared ones:

- `100`/`700` agent: KANTO/FINAF binding turns ASTERI-identified persons into shared URIs (no per-record raw URI, no ordinal).
- `730`/`740`: Hubs are per-record but marc2bibframe2's de-duplication across `bf:relation` edges can collapse adjacent identical sources.
- `240`: Hub typed `#Hub240-N` but the N is shared with 100's `#Hub240-` family in some marc2bibframe2 versions — yet another bucket-counting risk.
- Flat Instance-side fields (`020 ISBN`, `250 edition`, `300 extent`, `336/337/338` content/media/carrier): no entity at all — just literals or thin bnodes — so the existing lineage scheme proposed `bffi-prov:fromSourceField` triples in M3 SPARQL as a parallel mechanism (P-48 Phase B, never shipped). Two schemes; two consistency burdens.

The root cause is consistent across all these: **lineage is computed from M3's view of the graph, not from the source MARC**. M3 only sees what marc2bibframe2 chose to surface as raw entities. The source MARC's actual field structure is the authoritative position information; we've been triangulating it from BIBFRAME's incomplete projection.

### Beyond the diff — true provenance

The diff bug is the visible failure mode. The deeper need is graph-level traceability:

> Given a triple `<canonical-work> bffi:subject <yso-uri>` in the BFFI graph, which source MARC field instance(s) produced it?

The round-trip converter answers this implicitly today (when it can): "if you see `$9 src=650-3` on the recon side, that came from source 650 instance #3." But a SPARQL query against the BFFI graph cannot answer it — the recon-side `$9` is only materialised when round-trip runs, and only for the converter's idea of which entity goes with which source field. Whole categories of derivation are invisible:

- A `bffi:subject` triple → which source 650/648/651 produced it?
- A `bffi:Manifestation`'s `bffi:extent` literal → MARC 300 instance #1?
- An `rdfs:label` on an LoC carrier URI → which record's 338 $a contributed the cataloguer-typed label?

The cataloguer-review HTML, the future audit workflows, and the cross-record analytics ("how many records use this YSO concept?") all need this provenance to be a queryable property of the graph, not a recon-time artifact.

## Design principles

Drawn from the failure modes above:

1. **Source-grounded.** Lineage tokens are computed from source MARCXML, not from BIBFRAME's projection. The MARC field instance is the authority; M2's job is to mint a deterministic token per field instance and attach it to every BFFI entity derived from that field.
2. **Content-independent.** The token does not depend on `$a` value, `$0` URI, language, or any other content. A cataloguer typo-fixing `$a` doesn't change the token; an M9 reconciliation doesn't change the token. The token is stable across the entire pipeline lifetime of that record.
3. **Position-stable within bib.** Adding or removing a field shifts subsequent tokens within its tag bucket (so it's not stable across re-cataloguing), but the within-record/within-pipeline-run invariant is what matters for round-trip and provenance queries.
4. **Carryable through every transformation.** M3 CONSTRUCTs must passthrough the provenance triple unchanged. M8's canonical mint (Work merging) takes the union of all input records' source-field tokens. M9's authority binding does not destroy the per-record provenance.
5. **Decomposable.** A token like `<bib>:650:3` is human-readable AND machine-parseable. Operators can grep for it; SPARQL can filter on it.
6. **Unified for all field shapes.** One predicate, one format, one rule. No "raw URI fragment for these fields, prov triple for those fields, no lineage for the third group."
7. **Graph-resident.** The provenance lives in the BFFI graph as triples. The round-trip converter is one consumer; SPARQL queries and the cataloguer-review HTML are others.

## Token scheme

### Format

```
<bib_id>:<tag>:<within-tag-ordinal>
```

Examples: `b10642122:650:3`, `b10068004:700:11`, `b11567594:730:5`.

- `bib_id`: the Helmet bib record identifier (`001` controlfield value).
- `tag`: three-character MARC tag (datafield) or controlfield tag (`008`, `005`, etc.).
- `within-tag-ordinal`: 1-indexed position of this field instance within the same-tag bucket, in source MARCXML **document order**.

Controlfields have ordinal `1` (one instance per tag by definition for the common ones; pathological multi-instance controlfields rank in document order anyway).

Subfield-level provenance is **not** modelled. The token identifies a field instance; per-subfield derivation is recoverable by inspecting which BFFI predicates carry the token (`<bf:Title bffi-prov:fromMarcField "...:245:1"> bf:mainTitle "Foo"` says "this Title was born from 245:1 and routed mainTitle to $a").

### Predicate

```
bffi-prov:fromMarcField
  a               owl:DatatypeProperty ;
  rdfs:label      "from MARC field"@en, "MARC-kentästä"@fi ;
  rdfs:domain     rdfs:Resource ;
  rdfs:range      xsd:string ;
  skos:definition "Identifies the source MARC field instance that
                   contributed to this resource. Format
                   ``<bib_id>:<tag>:<within-tag-ordinal>``. Multiple
                   triples per resource when the resource was
                   derived from more than one source field."@en .
```

Lives in the existing `bffi-prov:` namespace at `http://urn.fi/URN:NBN:fi:schema:bffi-prov#` (defined in `CLAUDE.md` "Committed identifiers" — confirmed scope-fit).

### Cardinality

- 0..n per resource.
- Zero when the resource is purely synthesised (a `bffi-prov:Activity` URI, a canonical Work URI minted by M8 from a SHA1 — but the Work links back via per-record source IDs as we'll see).
- One when the resource was derived from a single source field (the common case: a `bf:Agent` from a single 700, a `bf:Isbn` from a single 020).
- Many when the resource was derived from multiple source fields (a canonical Work that merged across editions has one token per contributing record; a `bf:Title` synthesised from 245 + 246 would have two).

## Architecture

### M2 — produce the tokens

A new sub-stage `m2_post` (or extension of the existing `m2` runner) runs immediately after marc2bibframe2 produces per-record BIBFRAME RDF/XML. For each record:

1. **Parse source MARCXML.** Walk every `<datafield>` and `<controlfield>` in document order. Compute the within-tag ordinal as a counter incremented per tag bucket.
2. **Build a position index.** Map `<tag, ordinal>` → snapshot of the field (subfield codes + values + indicators).
3. **Correlate BIBFRAME entities to source fields.** Per-tag matching strategy:

   | Source field shape | Strategy |
   |---|---|
   | Has `bflc:marcKey` somewhere in BIBFRAME (100/240/600/650/700/710/711/730/740/800/810/811) | Parse the leading tag off marcKey; match marcKey content to source subfield string with bounded edit distance for ISBD-punctuation drift. |
   | Has `$0 <uri>` (6XX / 7XX / 100 / 240) | Match the BIBFRAME entity whose URI equals `$0` (when no marcKey path matched). One entity per source field. |
   | Flat-literal field (020/028/035/250/300/336/337/338/041/505) | Match by literal value at the expected BIBFRAME predicate (e.g. 020 `$a` ↔ `bf:Isbn` with `rdf:value` equal to the source `$a` after digit-only normalisation). |
   | Provision-activity field (260/264) | Match by composite of `bflc:simplePlace`/`simpleAgent`/`simpleDate` against source `$a`/`$b`/`$c`. |
   | Title field (245/246) | Match by `bf:Title` with `bf:mainTitle` equal to source `$a` (concatenated with `$b` when 245). |
   | Note fields (500/520/etc.) | Match by `bf:Note` with `rdfs:label` equal to source `$a`. |
   | Controlfield (005/008/041) | The single per-record entity (the `bf:Work` or `bf:Instance`) — controlfields aren't per-entity in BIBFRAME. Token attached to the per-record root entity with predicate qualified by source tag in the value. |

4. **Emit `bffi-prov:fromMarcField` triples.** For each correlated entity, add one triple `<entity> bffi-prov:fromMarcField "<token>"`.
5. **Audit unmatched.** Source fields with no correlated BIBFRAME entity → log to a per-record audit file `m2-post-audit.jsonl`. Cases: cataloguer-only fields marc2bibframe2 drops (e.g., `001` Helmet bib_id duplicated, `907` local), unusual routing.
6. **Audit over-matched.** BIBFRAME entities matched to multiple source fields → also log (likely indicates a marc2bibframe2 merge we should know about).

The correlation logic is the hard part. The validation handle: the existing 500-record diff already tells us which recon rows pair correctly. A per-tag correlation regression test pins each match strategy.

### M3 — passthrough

Every CONSTRUCT in `sparql/bf_to_bffi_*.rq` adds two lines:

```sparql
?someEntity bffi-prov:fromMarcField ?token .  # in CONSTRUCT clause
...
OPTIONAL { ?someEntity bffi-prov:fromMarcField ?token }  # in WHERE
```

A trivial mechanical change. No logic in M3 itself.

### M8 — union under canonical mint

Canonical Work mint (`src/bffi_pipeline/stages/m8/mint.py`):

```python
canonical_work.fromMarcField = UNION(
    raw_work.fromMarcField for raw_work in member_works_of_this_canonical
)
```

A canonical Work merged across three Helmet records gets three tokens (one per contributing record's 245/100 anchor). Canonical Manifestation: one (1:1 with bib).

Canonical subjects: existing M8 propagation passes (`_propagate_subject_typing`, `_propagate_raw_agent_identifiers`, `_propagate_manifestations`) already copy the relevant triples; they need to additionally carry `bffi-prov:fromMarcField` on the way through. This is the link-node introduction question — see "M9 + the link-node question" below.

### M9 — authority binding without losing per-record provenance

When M9 binds raw subject URI `<...#Topic650-27>` to authority `<yso:p13819>`, the (work, subject) edge changes target. The provenance triple `<...#Topic650-27> bffi-prov:fromMarcField "b10642122:650:5"` needs to follow the binding.

**The link-node question.** For URI-keyed (M9-bound or `$0`-supplied direct) subjects, the (Work → YSO URI) edge has no per-record entity to anchor provenance on. Three architectural options:

#### Option L1 — Reify each (Work, predicate, target) edge as a SubjectLink node

```turtle
<work-canonical>
  bffi:subject <yso-uri> ;            # flat predicate retained for compatibility / Skosmos
  bffi:hasSubjectLink <link-uuid> .

<link-uuid> a bffi:SubjectLink ;
            bffi:subjectTarget <yso-uri> ;
            bffi-prov:fromMarcField "b10642122:650:5" .
```

Pros: clean ontology; SPARQL `?link bffi:subjectTarget ?t . ?link bffi-prov:fromMarcField ?token` queries are direct.

Cons: every existing `bffi:subject` query stays valid (the flat predicate is retained), but the cataloguer-review and Skosmos surfaces gain a parallel access path. ~50 query touchpoints to consider.

#### Option L2 — Named graph per record

Per-record provenance triples go into a per-bib named graph `bib-<bib_id>:` keyed by bib_id. The default/union graph keeps the flat triples.

Pros: zero ontology change; query layering matches the natural "what does this record contribute" semantic.

Cons: Skosmos and most of our query stack assume the default graph; named-graph queries require GRAPH clauses everywhere. Fuseki is fine; the consumer code is not.

#### Option L3 — RDF-star (rdf:Statement) annotation

```turtle
<<<work-canonical> bffi:subject <yso-uri>>> bffi-prov:fromMarcField "b10642122:650:5" .
```

Pros: standard provenance pattern; most concise.

Cons: rdflib's RDF-star support landed but our Fuseki version and Skosmos don't speak it natively. Tooling risk; can revisit when ecosystem catches up.

**Recommended: Option L1**, with `bffi:subject <yso-uri>` retained as a derived shortcut. The reification cost is bounded (one new class, one new property) and matches the existing `bffi:contribution` precedent for Work→Agent edges.

### Round-trip converter — read tokens, emit `$9 src=`

`_lineage_token` is replaced by `_lineage_from_marc_field`: walks the entity's `bffi-prov:fromMarcField` triples, returns the (single) token if one exists, otherwise None. For multi-source canonical entities (a merged Work), the converter operates per-Manifestation so the relevant bib_id picks one token from the set.

The `$9 src=<token>` subfield format becomes `$9 src=<bib>:<tag>:<ord>` directly — no within-bucket renormalisation, no marc2bibframe2-counter dependency.

### Diff comparator — pair by token, eliminate position fallback

```python
# Source side: parse MARCXML, compute token per field
source_tokens = {compute_token(record, df): df for df in datafields}
# Recon side: read $9 src= directly
recon_tokens  = {parse_lineage(df): df for df in recon_datafields if has_lineage(df)}
# Pair by token identity
pairs = pair_by_token(source_tokens, recon_tokens)
# Residue: source fields with no recon token → `lost`; recon with no source token match → `added`
```

The current heuristic fallback (`$a` matching, position-bucket fallback) is **removed**, not retained. After the M2 post-process lands, every reconcilable BFFI entity carries a token; rows without tokens are genuine gaps the cataloguer should see as such.

Source-side `bib_id` is implicit (the diff is per-bib). Tokens compare as `<tag>:<ordinal>` once the bib_id is factored out.

## Phase plan

### Phase A — M2 post-process scaffold + correlation for `bflc:marcKey`-bearing fields

Smallest viable slice that fixes the b10642122 class of bugs. Cover the field-shape category with the largest absolute mispair count.

- New module `src/bffi_pipeline/stages/m2_post/` with `correlator.py` (per-field-shape matchers) and `runner.py` (per-record driver).
- BFFI ontology: add `bffi-prov:fromMarcField` to `docs/lkd.rdf` (private-extension scope, P-49 Layer 3 precedent applies).
- Wire `m2_post` into the pipeline runner between `m2` and `m3` as a CANONICAL_STAGE.
- Match strategy: marcKey-bearing entities only (6XX/7XX/100/240/730/740/800/810/811).
- Audit log writes `m2-post-audit.jsonl` per record; pipeline runner picks it up for cataloguer-review bundle.
- M3 SPARQL: passthrough `bffi-prov:fromMarcField` on raw → canonical mint paths. M8 / M9 unchanged (this phase covers raw-URI tokens that already pass through).
- Round-trip converter: prefer `bffi-prov:fromMarcField` when present; fall back to today's M3-fragment scheme. Both schemes coexist.
- Diff comparator: prefer `bffi-prov:fromMarcField` token when both sides have one; fall back to existing pairing otherwise.

**Acceptance**: b10642122's 650 mispairings drop from 11 to ≤ 2 (the 2 raw `#Topic650-27/31` are the regression risk — Phase A's matcher must handle the no-`$0` case). Aggregate run-wide 650-mispair count drops by ≥ 90 %.

### Phase B — Coverage extension to flat literal + provision-activity fields

- Matchers for 020/028/035/250/300/336/337/338/041/264/505/500/520.
- M3 SPARQL: emit `bffi-prov:fromMarcField` on flat Instance-side entities (the bnodes for `bf:Isbn`, `bf:Extent`, `bf:Note`, `bf:ProvisionActivity`).
- Round-trip converter: prefer token for these fields too.

**Acceptance**: every recon row carries a token (or is explicitly logged as no-source — converter-skipped fields like 852). `marckey-bypass` and `lost-converter-gap` counts unchanged (orthogonal to lineage); `changed`/`identical` pairing accuracy is byte-correct.

### Phase C — Subject-link node introduction (Option L1)

The reification. Adds `bffi:SubjectLink` class, `bffi:hasSubjectLink` predicate, `bffi:subjectTarget` predicate. M3 emits link nodes for every subject occurrence; flat `bffi:subject` retained as a derived shortcut.

- BFFI ontology additions in `docs/lkd.rdf` (private extension).
- M3 SPARQL: emit both the flat `bffi:subject` triple AND the typed link node.
- M8: link nodes are per-record, never merged. Canonical Work has N link nodes, one per contributing record's subject occurrence.
- M9: authority binding rewrites `bffi:subjectTarget` on the link node; `bffi-prov:fromMarcField` survives because it's a triple on the link node itself, not on the target.
- Round-trip converter: walk via link nodes instead of flat `bffi:subject`.
- Diff comparator: per-record link nodes carry tokens directly; no inference needed.

**Acceptance**: 100 % token coverage on subject rows; URI-keyed subjects (the 9-of-11 in b10642122) get distinct, correct tokens.

### Phase D — Cleanup + observability

- Remove the legacy `$9 src=<tag>-<rank>` rank-bucketing fallback from the converter (M2-post tokens are mandatory).
- Remove `_lineage_rank_map` and `build_lineage_rank_map` from the converter.
- Diff comparator's position-fallback path becomes audit-only ("residue" logging).
- Metrics: per-pipeline-run "lineage coverage" = `tokens-emitted / total-recon-rows`. Target: 100 % minus the explicitly-skipped tag set (`852`, `907`).

## Migration / compatibility

- Phase A delivers backwards-compatible coexistence: new tokens preferred, old M3-fragment scheme retained. No external consumer breakage.
- Phase B: still coexistence — flat-field token emission is purely additive.
- Phase C: new link nodes are additive (the flat `bffi:subject` predicate stays as a derived shortcut materialised by M3). Existing consumers see the same flat triples.
- Phase D: removes the legacy fallback. Should land only after Phases A+B+C have produced ≥ 1 full-corpus run showing 100 % coverage.

For Skosmos, no change needed across any phase — the flat predicates Skosmos reads stay intact.

For Fuseki, the new triples are additive. Storage growth: roughly +1 triple per entity, scoped to per-record entities (not shared URIs). Estimated +5-10 % triple count on a 800k-record load.

For the gold sets and eval harnesses: tokens are an emit-side property; eval inputs don't change.

## Risk register

- **Correlation accuracy.** Phase A's heuristic matchers (marcKey content → source field) need bounded-edit-distance tolerance for ISBD punctuation drift. Mitigations: per-shape regression test fixtures; the existing 500-record sample exercises the common cases.
- **marc2bibframe2 version drift.** If marc2bibframe2 changes its entity-emission rules (e.g., starts deduplicating subjects across records, or changes its raw URI fragment grammar), the M2-post correlator needs updating. Mitigation: the audit log makes drift visible; the matchers are isolated in one module.
- **M9 binding redirects.** When M9 rebinds a raw subject URI to an authority URI, the link-node approach (Phase C) decouples this from the provenance token. Phase A/B doesn't yet have link nodes — for these phases, M9-bound subjects might lose their token if M3's passthrough isn't careful. Phase A's acceptance criterion (≤ 2 mispairs on b10642122's 650) catches this regression.
- **Multi-source canonical entities.** A canonical Work merged across 3 records has 3 tokens. The round-trip converter, scoped per-Manifestation, picks the one matching its bib_id. Verified by the existing per-Manifestation reconstruct loop. If a future refactor processes all Manifestations of a Work jointly, the picker needs revisiting.
- **Backwards-compatibility window.** Coexistence of two lineage schemes (Phases A-C) doubles the conditional logic in the converter and diff. Mitigation: Phase D deletes the legacy path on a known-good run; the doubled-logic window is bounded by the project's pipeline-run cadence (~weekly).

## Rollback procedure

Phase A is reversible by skipping the `m2_post` stage in the runner config — the converter falls back to the legacy scheme automatically. No data migration on rollback.

Phases B-D are similarly reversible at the runner level until Phase D ships; after Phase D, rolling back requires reintroducing the legacy fallback (small code change, no graph migration).

## Verification

Per-phase acceptance criteria above. Run-wide:

- `runs/<id>/marc-roundtrip/summary.json` adds a `lineage_coverage` field. Phase A: ≥ 75 %. Phase B: ≥ 95 %. Phase C: 100 %.
- `tests/integration/test_marc_roundtrip_lineage.py` runs the full M2 → diff loop on the 500-record sample and asserts the per-tag mispair count stays at 0 for tokenised fields.
- Manual spot-check: `bffi-pipeline q --query "SELECT ?token ?entity WHERE { ?entity bffi-prov:fromMarcField ?token } LIMIT 20"` returns plausible triples.

## Relationship to P-48 and P-49

- **Supersedes P-48.** P-48 Phase A (the `$9 src=<tag>-<rank>` scheme) shipped and works for the cases where M3 raw URIs exist. P-50 keeps that path as a fallback through Phase C, removes it in Phase D. P-48 Phase B (`bffi-prov:fromSourceField` on flat fields) is **subsumed** by P-50 Phase B with a renamed and harmonised predicate.
- **Orthogonal to P-49.** P-49 is about BFFI's structured-content fidelity (closing the `bflc:marcKey`-bypass audit). P-50 is about provenance-tracking the source-MARC origins. They share the `bffi-prov:` namespace and target the same round-trip diff surface, but neither blocks the other. P-50's tokens make P-49's audit per-row tractable (we can say "this `marckey-bypass` row came from `b10642122:700:3` and the structured side is missing").
- **Migration of the in-progress P-48 status**: on P-50 graduation, P-48 moves from `in-progress` to `completed` with a supersede pointer in its header. The Phase A commit `3be50cf` remains valid history; the `$9 src=` wire format is retained (Phase A coexistence).

## Suggested next step

If the design pencils out, graduate this into `docs/plans/backlog/p-50-source-field-provenance.md` (proposal → plan shape rewrite). Phase A is the smallest test of the architecture — implementable in ~3 days against the existing 500-record sample, with the b10642122 case as the verification fixture.

Open questions that should be answered at graduation:

- L1 / L2 / L3 link-node decision: confirm L1 is preferred before Phase C starts.
- Token format includes `bib_id`. Confirm we want it in the on-the-wire `$9 src=` value (verbose but self-describing) vs implicit-from-record-context (terser; matches today's `<tag>-<rank>` format).
- M2-post audit log: confirm it lives at `runs/<id>/m2-post-audit.jsonl` and is consumed by the cataloguer-review bundle (analogous to the contrib/title-lang audit files).
