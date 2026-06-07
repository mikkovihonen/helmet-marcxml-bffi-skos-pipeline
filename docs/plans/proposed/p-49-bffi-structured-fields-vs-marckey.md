# P-49 — Close BFFI structured-field gaps exposed by `bflc:marcKey` round-trip bypass

**Status**: in progress (Phase A + part of Layer 1 shipped 2026-06-07; remainder proposed).

**Scope**: per-field-family workstreams. Each gap-class below is sized to land as its own backlog plan (~3-5 days each) after this proposal is ratified. The umbrella proposal exists to capture the audit + ratify a shared verification criterion, not to commit to one monolithic merge.

**Proposal-base commit**: HEAD of P-48 Phase A (the lineage scaffolding the structured side will need to attach to). Update on graduation.

## Progress

### Shipped 2026-06-07

- **Phase A — `marckey-bypass` diff status**: new fourth diff classification alongside `identical`/`changed`/`lost`/`added`/`tag-changed`/`lost-converter-gap`. The converter emits a `$9 marckey-bypass` sentinel on every recon row built from `bflc:marcKey`; the diff parser strips it and overrides `identical`/`changed` to `marckey-bypass` (does NOT override `tag-changed` — misroutes remain the more serious diagnostic). Summary counter increments; cataloguer-review HTML renders the new status in its own band. Tests: `tests/unit/test_marc_roundtrip_diff.py::test_marckey_bypass_*` + converter coverage in `tests/unit/test_marc_roundtrip_converter.py::test_100_emits_marckey_bypass_sentinel_when_built_from_marc_key`. Wired through all 7 marcKey-reading sites: 100, 240, 6XX (raw + authority + raw-origin), 655, 700/710/711, 730/740.

- **Layer 1 partial — 240/730/740 `bf:partNumber` / `bf:partName`**: M3 SPARQL (`sparql/bf_to_bffi_expression.rq` for 240, `sparql/bf_to_bffi_manifestation.rq` for 730/740) routes the structured part-number/part-name triples from BIBFRAME (which marc2bibframe2 *does* emit alongside `bf:mainTitle`) through to the BFFI Hub. Converter (`_emit_uniform_title`, `_related_title_subfields`) reads structured predicates first; falls back to marcKey only for $a (title proper), $g (responsibility), and $l (title language) — all genuine BFFI gaps. Row stays flagged `marckey-bypass` while any subfield comes from marcKey; the partial improvement is that $n and $p no longer cycle through the raw MARC string. Shared `_merge_structured_parts` helper keeps MARC subfield order (`$a $n $p $g` / `$a $n $p $l`).

### Data-shape finding that reshaped scope

A 500-record BIBFRAME survey (run `20260607-0622-dfc03f`) showed that marc2bibframe2 emits structured predicates inconsistently:

- ✓ `bf:partNumber` / `bf:partName` on `bf:Title` — emitted in 23/500 records. **Routable**, shipped.
- ✗ `bf:date` on `bf:Person` — **never emitted**. The cataloguer-typed `$d 1862-1940` is collapsed into `rdfs:label "Name, 1862-1940"`; only `bflc:marcKey` preserves the boundary.
- ✗ `bf:ComplexSubject` + `bf:hasComponent` — **never emitted** (0/500). Subjects with `$x/$y/$z/$v` subdivisions land as a single `rdfs:label`.

Corpus-frequency follow-up: 6XX subdivisions are rare (~0.04 % of bibs in a 5 000-file random sample). The Layer 2 work is upstream-blocked AND low-volume; the Layer 1 `$d` work is upstream-blocked but high-volume (every personal name with a date suffix loses the boundary).

**Consequence**: the original Layer 2 ("adopt `bf:ComplexSubject`") and the `$d` portion of Layer 1 both need an **M2 post-process** that parses cataloguer-typed `bflc:marcKey` at ingestion time and synthesises the missing structured triples. That work is now scoped under Layer 3 below alongside the new BFFI predicates.

Supersedes the implicit "use marcKey as a recon shortcut" stance baked into the round-trip converter today. Does **not** propose removing `bflc:marcKey` from the BFFI graph — it stays as the audit-provenance witness for round-trip diagnostics. The proposal narrows what the *recon converter* is allowed to read.

## Motivation

The round-trip converter (`src/bffi_pipeline/marc_roundtrip/converter.py`) currently reads `bflc:marcKey` at seven sites to reconstruct MARC subfield boundaries that BFFI's structured properties don't preserve. `bflc:marcKey` is the cataloguer's original MARC subfield string — `"7001 $aAndersson, Benny,$esäveltäjä"` — preserved in the BFFI graph as an opaque text blob.

This means the round-trip succeeds for those fields not because BFFI faithfully captures the bibliographic data, but because the original MARC string is smuggled through the graph and reparsed on the way out. The cataloguer-review diff shows `identical` rows where the actual BFFI side is structurally insufficient. **We are validating the MARC string, not the BFFI ontology.**

Concrete case that motivated the audit: b10068004 700 $a — the original `"Andersson, Benny,"` (with ISBD trailing comma) was preserved only in marcKey; the structured `bf:Agent rdfs:label` is `"Andersson, Benny"` (no comma). The diff shows `changed` for the comma, but if the converter had been reading from `rdfs:label` (which it does as fallback), the actual gap would be invisible because we don't even see the trailing-comma signal.

### Project-level rationale

CLAUDE.md: "Don't merge silent failures into provenance. Log `uncertain` with the actual error." A converter-side marcKey read is exactly that — a silent recovery that hides the structured-side gap.

If BFFI is to be contributable to the National Library of Finland as a faithful FRBR/LRM model of the Helmet corpus, the model needs to encode the bibliographic content directly, not lean on a MARC string passthrough.

## The seven marcKey-bypass sites — audit

Every row below tracks: which converter site, which MARC subfields it currently recovers via marcKey, whether a BFFI/BIBFRAME predicate **already exists** in `docs/lkd.rdf`, and whether M3 currently routes it.

### Family A: Personal-name component subfields (100, 600, 700)

Converter sites: `_emit_primary_contribution` (line 786), `_emit_added_entries` (line 1681), `_raw_subject_row`+`_authority_subject_row` for 600 (lines 1547, 1566).

| MARC | What it carries | BFFI predicate | In `docs/lkd.rdf`? | M3 routes? |
|---|---|---|---|---|
| `$a` | personal name | `rdfs:label` on `bf:Person`/`bf:Agent` | ✓ | ✓ |
| `$c` | title-of-person (e.g. "(fiktiivinen hahmo)", "Sir") | — | **gap** | — |
| `$d` | dates associated (e.g. "1812-1870") | `bf:date` (on bf:Person, BIBFRAME idiom) | ✓ (BIBFRAME side) | — |
| `$q` | fuller form of name (e.g. "(Charles John Huffam)") | — | **gap** | — |
| `$b` | numeration (e.g. "II" for Elizabeth II) | — | **gap** | — |

**Gap**: three of five subfields have no first-class BFFI predicate. The fourth (`$d` → `bf:date`) exists on the BIBFRAME side but M3 doesn't route it.

**Trailing-ISBD-comma**: cataloguer-typed comma after `$a` when `$e` follows is content-bearing punctuation per RDA/AACR2. The cleanest structured fix is **not** to put the comma in `rdfs:label` — it's to record `$a` and `$e` as separate properties and let the converter add ISBD punctuation deterministically from the subfield-ordering rules.

### Family B: Uniform-title components (240, 730, 740)

Converter sites: `_emit_uniform_title` (line 928), `_emit_related_uniform_titles` (line 1767).

| MARC | What it carries | BFFI predicate | In `docs/lkd.rdf`? | M3 routes? |
|---|---|---|---|---|
| `$a`/`$t` | title proper | `bffi:mainTitle` on `bffi:Title` | ✓ | partly (240 yes, 730 no) |
| `$n` | number of part | `bffi:partNumber` | ✓ | — |
| `$p` | name of part | `bffi:partName` | ✓ | — |
| `$l` | language of expression | — | **gap** (no language predicate on `bffi:Title`) | — |
| `$g` | miscellaneous info (730) | — | **gap** | — |

**Gap**: two of five subfields have no predicate. The three that do exist (`mainTitle`/`partNumber`/`partName`) are simply not routed by M3 — the uniform-title Hub stops at `bf:title → bf:mainTitle` collapsed and `bflc:marcKey`.

### Family C: Subject subdivision components (600-655 $x/$y/$z/$v)

Converter sites: `_raw_subject_row` (line 1547), `_authority_subject_row` (line 1566), `_name_subfields_from_raw_origin` (line 1591).

| MARC | What it carries | BFFI/BIBFRAME pattern | Available? | M3 routes? |
|---|---|---|---|---|
| `$a` | main heading | `rdfs:label` on subject node | ✓ | ✓ |
| `$x` | general subdivision (e.g. "Periodicals") | `bf:ComplexSubject` + `bf:hasComponent` chain | BIBFRAME-native pattern | — |
| `$y` | chronological subdivision (e.g. "20th century") | `bf:ComplexSubject` with `bf:Temporal` component | ✓ (`bffi:Temporal` exists) | — |
| `$z` | geographic subdivision (e.g. "Finland") | `bf:ComplexSubject` with `bf:Place` component | ✓ (`bffi:Place` exists) | — |
| `$v` | form subdivision (e.g. "Handbooks") | `bf:ComplexSubject` with `bf:GenreForm` component | ✓ (`bffi:GenreForm` exists) | — |

**Gap**: M3 flattens compound subjects into `rdfs:label`. The component classes exist in BFFI; the `bf:ComplexSubject` wrapper class + `bf:hasComponent` relation need to be vendored or proposed. Cataloguers' subjects with subdivisions are surprisingly common — even a single `$z` modifier on YSO `sodat $z Suomi` collapses to one label.

### Family D: 730/740 miscellaneous information

Captured under Family B but worth flagging: the `$g` after a uniform title on a 730 (e.g. `730 $a Save the best for last / $g Lind, Jon`) has no BFFI home. `bf:responsibilityStatement` is Manifestation-scoped; this is a Hub-scoped one.

## Proposal — what to add to BFFI, in three layers

### Layer 1: Route predicates that already exist in BFFI but M3 ignores

No ontology change needed. M3 SPARQL extensions only:

- **`bffi:partNumber` / `bffi:partName`** on uniform-title Hubs (240, 730, 740).
- **`bffi:Place` / `bffi:Temporal` / `bffi:GenreForm`** components on subjects that have `$x/$y/$z/$v` (requires deciding on the wrapper — see Layer 2).
- **`bf:date`** on `bf:Person` for 100/600/700 `$d`.

Each is one M3 SPARQL block + one converter case removal of the marcKey read for that subfield. Sizes as ~1-2 days per field-family.

### Layer 2: Adopt existing BIBFRAME terms that BFFI hasn't mirrored

Three classes/predicates exist in BIBFRAME (`http://id.loc.gov/ontologies/bibframe/…`) but aren't in `docs/lkd.rdf`:

- **`bf:ComplexSubject`** — wrapper class for subjects with subdivisions.
- **`bf:hasComponent`** — relation from `bf:ComplexSubject` to its parts.
- **`bf:order`** / numeric component-order indicator — needed because `$x Geography → Finland` is not the same subject as `$z Finland → Geography`.

BFFI policy decision needed: do we **(a)** use the BIBFRAME terms directly (BFFI already imports `bf:` predicates extensively via `owl:equivalentProperty`), or **(b)** mirror them as `bffi:ComplexSubject` etc. with `owl:equivalentClass` declarations? Existing BFFI convention favors (b) for class definitions; (a) is acceptable for predicate-only additions per the `bf:source`, `bf:assigner`, `bf:identifiedBy` precedents already used in `sparql/bf_to_bffi_*.rq`.

### Layer 3 (detailed)

Layer 3 now bundles three workstreams that share the same architectural prerequisite — an **M2 post-process that parses `bflc:marcKey` at ingest time and synthesises structured BFFI triples** — so they can be planned and ratified together rather than as one-offs.

#### Layer 3a — Five new BFFI predicates

These MARC subfields have no first-class predicate in BIBFRAME or BFFI today. Proposed minting:

| URI (private extension) | Domain | Range | MARC source | Cardinality | Notes |
|---|---|---|---|---|---|
| `bffi:titleOfPerson` | `bffi:Person` | `rdfs:Literal` | 100/600/700/800 `$c` | 0..n | Honorifics ("Sir", "Princess"), epithets ("the Great"), fictionality marker "(fiktiivinen hahmo)". |
| `bffi:fullerFormOfName` | `bffi:Person` | `rdfs:Literal` | 100/600/700 `$q` | 0..1 | "(Charles John Huffam)" — the expanded form of an initialised name. |
| `bffi:numeration` | `bffi:Person` | `rdfs:Literal` | 100/600/700 `$b` | 0..1 | Sovereign / lineage numbers — "II" for "Elizabeth II". |
| `bffi:titleLanguage` | `bffi:Title` | `id.loc.gov/vocabulary/languages/*` URI **or** `xsd:language` literal | 240/730 `$l` | 0..1 | Language of expression for a uniform title — "Venäjä" / "fin". Prefer URI when the cataloguer typed a recognised language name; fall back to literal otherwise. |
| `bffi:titleMiscellaneousInfo` | `bffi:Title` | `rdfs:Literal` | 730 `$g` | 0..1 | Free-text qualifier on a related uniform title — typically the responsibility / composer. |

For each predicate the proposal will contribute, to `docs/lkd.rdf` (private extension; subject to NLF ratification before upstreaming):

- `rdf:type owl:DatatypeProperty` (or `owl:ObjectProperty` for `titleLanguage` URI form).
- `rdfs:label` in `en` and `fi` (and `sv` when straightforward), matching the existing BFFI convention from `docs/lkd.rdf` (see `bffi:mainTitle` block at line ~660).
- `rdfs:domain` and `rdfs:range` per table.
- `skos:definition` in `en` cross-referencing the MARC subfield.
- `dct:modified` with the introduction date.
- `bffi-meta:closeMatch` to the relevant RDA element where one exists (e.g. RDA P50121 for `titleOfPerson`, P30142 for `titleMiscellaneousInfo`).

**Namespace decision**: use the existing `bffi:` namespace (not a separate `bffi-ext:`). Rationale: `bffi:` already extends BIBFRAME with Finnish-context predicates; adding five more MARC-pragma predicates fits that pattern. A separate namespace would fragment the model and create cross-namespace `owl:propertyChainAxiom` work later if any of these get upstreamed.

**Hashed-URI scheme**: `http://urn.fi/URN:NBN:fi:schema:bffi:titleOfPerson` etc. — same shape as the existing `bffi:` predicates.

#### Layer 3b — M2 post-process: parse `bflc:marcKey` into structured BFFI

The Layer 1 work that didn't ship (the `$d`-on-Person case) and the original Layer 2 (`bf:ComplexSubject` for 6XX subdivisions) both need the same upstream fix: a post-process pass between M2 (marc2bibframe2 XSL) and M3 (BIBFRAME → BFFI CONSTRUCT) that walks every `bflc:marcKey` literal on agents / subjects / hubs and synthesises the structured triples marc2bibframe2 itself doesn't emit.

Sketch:

- **New stage** `m2_post` (or fold into the existing M2 wrapper at `src/bffi_pipeline/stages/m2/`). Runs on the per-record BIBFRAME RDF/XML output **before** M3 reads it.
- **For each `bf:Agent`** carrying `bflc:marcKey` matching `1001 $a... $d<date>`: synthesise `?agent bf:date "<date>"^^xsd:string` (or, future, a structured `bf:Date` block when ISO-date parsing is reliable). Same for `$q` → `bffi:fullerFormOfName`, `$c` → `bffi:titleOfPerson`, `$b` → `bffi:numeration` once Layer 3a predicates land.
- **For each `bf:Topic` / `bf:Place` / `bf:Temporal`** carrying `bflc:marcKey` matching `650 $a<main>$x<gen>$z<geo>$v<form>`: mint a `bf:ComplexSubject` parent + `bf:hasComponent` chain pointing to component nodes typed `bf:Topic` / `bf:Place` / `bf:Temporal` / `bf:GenreForm`. Preserves source-MARC subfield order via `bf:order` on each component. Layer 2 unblocked.
- **For each `bf:Hub` / `bf:Title`** carrying `bflc:marcKey` matching `240/730 $l<lang>` and `730 $g<misc>`: synthesise `?title bffi:titleLanguage`, `?title bffi:titleMiscellaneousInfo` once Layer 3a predicates land.

The post-process is the **only** place that parses MARC subfield strings. Once it has run, the M3 CONSTRUCT queries see exclusively-structured BFFI and can drop their marcKey-fallback paths. `bflc:marcKey` stays in the graph (as provenance) but the round-trip converter no longer reads it for content.

**Architectural placement question**: should the post-process live in our pipeline (`src/bffi_pipeline/stages/m2_post.py`) or upstream as a PR to `NatLibFi/marc2bibframe2`? Upstreaming is the right long-term answer but the LoC-controlled XSL submodule lags the kind of MARC patterns Helmet's corpus carries. Recommendation: implement in our pipeline first, document the divergence in `docs/tech-stack.md`, contribute upstream once the parser is stable.

#### Layer 3c — NLF discussion path

The Layer 3a predicates are private extensions until ratified by the National Library of Finland (`docs/lkd.rdf` is the vendored BFFI 1.0.0; predicates we mint into the `bffi:` namespace without NLF buy-in are local-only and won't validate against the canonical ontology when NLF re-publishes).

Proposed sequence:

1. **Internal validation** (this proposal, ~1 week): ship the predicates as `bffi-local:` in our pipeline. SHACL shapes update to permit them on the relevant domains. Round-trip diff shows `marckey-bypass` rows drop to zero on 100/700 `$c$d$q` and 730 `$l$g`.
2. **Evidence package for NLF**: per-predicate justification doc citing (a) corpus-frequency in Helmet (~800k records — count `$c` / `$d` / `$q` / `$b` / `$l` / `$g` per 100k sample), (b) the absence of equivalent BIBFRAME / RDA predicates, (c) cross-references to the existing BFFI extension precedents (e.g. `bffi:partNumber` / `bffi:partName` were themselves Finnish-context additions to BIBFRAME's title block).
3. **NLF review**: through the Finto / BFFI maintainer channel. Two outcomes:
   - **Accept**: predicates move from `bffi-local:` to `bffi:`, get a `dct:modified` bump in `docs/lkd.rdf`, ratify at next BFFI minor-version release. We rebind URIs in our graph (one-shot Skosify config change).
   - **Reject / alternative proposed**: NLF suggests a different modelling (e.g. `bf:variantName` for `$c`, or a `Note` blank-node pattern). We adopt the alternative; `bffi-local:` URIs deleted from our pipeline; nothing leaked beyond our private graph.
4. **Fallback if NLF channel is slow**: keep `bffi-local:` indefinitely; round-trip works locally, public BFFI dumps stay clean (the predicate URIs aren't in any Finto-published vocab, just in our Skosmos `bffi-works` graph alongside the data they describe).

#### Verification — Layer 3 acceptance criteria

A round-trip diff against the same 500-record sample, post-Layer 3:

- 100/600/700 rows with source `$c$d$q$b` reconstruct from structured BFFI; `marckey-bypass` count drops to **zero** for these fields.
- 240/730 rows with source `$l$g` reconstruct from structured BFFI; `marckey-bypass` count drops to **zero** for 240/730.
- 650/651/648 rows with source subdivisions (`$x$y$z$v`) reconstruct from `bf:ComplexSubject`'s component chain; `marckey-bypass` count drops to **zero** for these fields.
- The 7 marcKey-reading sites in `src/bffi_pipeline/marc_roundtrip/converter.py` all delete their marcKey-fallback branches; only `bflc:marcKey` retention is in the BFFI graph (as provenance), not in the converter's code path.
- The trailing-ISBD-comma case (b10068004 700 `$a "Andersson, Benny,"`) round-trips identically — handled by deterministic ISBD-punctuation reinsertion based on which subfield codes are about to be emitted, not by reading the comma from marcKey.

## Acceptance / verification

Per-family round-trip diff target: every field in the family reconstructs from BFFI structured properties only, with the converter blocked from reading `bflc:marcKey` for that family's subfields. Concrete:

1. **Phase A** ✓ (shipped 2026-06-07) — `marckey-bypass` diff status added. Every converter site that reads marcKey tags its output rows. Audit is now ongoing and visible in cataloguer-review.
2. **Layer 1 partial** ✓ (shipped 2026-06-07) — Hub-side structured `bf:partNumber` / `bf:partName` routed for 240/730/740. Records where the source 240/730 has only `$a $n $p` (no `$l`/`$g`) should drop OFF the `marckey-bypass` list; records with `$l` or `$g` stay flagged pending Layer 3.
3. **Layer 1 remainder + Layer 2 (deferred to Layer 3)** — `$d`-on-Person and 6XX subdivisions. Both blocked on marc2bibframe2 not emitting the structured side; unblocked by Layer 3b's M2 post-process.
4. **Layer 3** — the BFFI ontology extension + M2 post-process. Lands iff NLF signs off (or as `bffi-local:` indefinitely otherwise). Post-Layer 3 acceptance criteria in the Layer 3 section above.

## Why not just keep `bflc:marcKey`?

`bflc:marcKey` is correctly modelled as **provenance, not content**. It belongs in the BFFI graph as a witness ("this is what the cataloguer typed") for audit / re-derivation, exactly like `bffi-prov:Activity` records. Treating it as a content-bearing property silently couples the BFFI side to a specific MARC encoding decision (subfield ordering, ISBD punctuation, AACR2/RDA transcription rules), which:

- Makes BFFI non-faithful — the bibliographic content depends on parsing MARC, not on RDF traversal.
- Locks BFFI to MARC ingest specifically; a future non-MARC source (eg. ONIX, Sierra-native) couldn't produce equivalent BFFI without synthesising a marcKey.
- Hides genuine ontology gaps from the validation pipeline (the b10068004 case).

Keeping `bflc:marcKey` *in* the graph and *out of* the converter's content path is the targeted fix.

## Out of scope / explicitly deferred

- **Adding `bffi:marcKey` as a permitted content property** (proposed in P-33 line 121). This proposal argues the opposite — that direction would entrench the bypass.
- **Removing `bflc:marcKey` from the BFFI graph entirely**. Provenance value is real; we keep it.
- **240 ind1/ind2 reconstruction nuances** (nonfiling characters, title-traced flag). Distinct from the marcKey-bypass issue and routable independently.
- **246 variant titles** — already routed through `bf:VariantTitle bf:mainTitle` (structured); no marcKey read in `_emit_variant_title`. Confirmed clean.

## Suggested next step

If the audit pencils out, graduate this into `docs/plans/backlog/p-49-bffi-structured-fields-vs-marckey.md` (proposal-shape → plan-shape rewrite). The Phase A scope (`marckey_bypass` diff status + Layer 1 uniform-title routing) is small enough to also serve as a confidence-building first cut — once cataloguers see those rows turn from green-but-cheating to honestly-flagged, the prioritisation of Layer 2/3 vs continued raw-MARC tolerance becomes a concrete trade-off rather than a hypothetical.
