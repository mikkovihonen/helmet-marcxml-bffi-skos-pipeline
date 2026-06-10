# BFFI ontology limitations as observed by the round-trip

The pipeline's round-trip stage (`marc-roundtrip`) reconstructs
MARCXML from the canonical BFFI graph and diffs it against the
source MARC. The diff residue is the empirical ledger of where
BFFI 1.0.0 + marc2bibframe2 + our pipeline collectively don't
preserve source-MARC information.

Some of that residue is real data loss; some is **acceptable
data movement** — the originating data survived the round-trip
but landed in a different MARC field whose meaning is
bibliographically equivalent (or modernised) relative to where
the cataloguer originally wrote it.

This document is the registry of the second category. Each entry:

1. **Names the case.**
2. **Describes the original MARC shape, the BFFI graph state, and
   the reconstructed MARC shape.**
3. **Pinpoints why the original shape can't be restored from BFFI
   alone** — usually because the distinction isn't carried in any
   `bffi:` / `bf:` predicate after marc2bibframe2's normalisation.
4. **Documents the conclusion**: is this acceptable, deferred to a
   future BFFI extension, or planned to be fixed inside our scope?

**Cardinal rule** (from a 2026-06-08 architectural review while
walking the diff residue): the round-trip converter MUST NOT
consult `bffi-prov:` (pipeline-internal provenance) to make
bibliographic-content decisions. The whole point of the round-trip
is to verify that the **`bffi:` namespace alone** can reconstruct
the source as closely as possible. Pipeline-internal data is fair
game for UI / pairing machinery (the `$9 src=` lineage token used
by the diff comparator and cataloguer-review HTML), but never for
deciding what content emits.

When the BFFI graph genuinely lacks the signal to restore a
distinction, the right answer is to (a) emit the closest
equivalent, (b) record the case here, and (c) flag it as a
candidate BFFI ontology extension for a future NLF conversation.

---

## L-01 — MARC 260 (pre-RDA) → MARC 264 ind2=1 (RDA)

**Case** Source MARC field 260 is the AACR2-era publication
statement (`$a Place : $b Publisher, $c Date`). MARC 264 with
ind2=1 is the RDA-modern equivalent for publication-role
provision activities; 264 ind2=2/3/4 are for distribution /
manufacture / copyright. The two tags carry the same
bibliographic data shape; the difference is when the record was
catalogued (pre-RDA vs RDA-era).

**Source MARC** — Helmet example b10068004:
```
260   $a London : $b Wise Publications, $c c1997
```

**BFFI graph** — marc2bibframe2 normalises both 260 and 264
records to a single `bf:ProvisionActivity` shape with:
- `rdf:type bf:Publication` (default; covers source-264 ind2=1
  AND source-260, because pre-RDA records have no ind2 marker)
- `bflc:simplePlace "London"` / `bflc:simpleAgent "Wise
  Publications"` / `bflc:simpleDate "c1997"`
- No predicate indicates which source-MARC tag (260 vs 264) the
  cataloguer originally used.

**Reconstructed MARC**:
```
264 ind1=" " ind2="1"   $a London : $b Wise Publications, $c c1997
```

**Why BFFI can't restore the original 260 form**: marc2bibframe2's
XSL silently collapses both source forms to `bf:ProvisionActivity`
with `bf:Publication` typing. No surviving triple records that
the cataloguer wrote `260` rather than `264 ind2=1`. The pipeline
cannot recover the distinction without either (a) modifying the
marc2bibframe2 submodule (prohibited per `CLAUDE.md`) or (b)
reading the source MARCXML at M2-post and synthesising a
non-BFFI marker (which `bffi-prov:fromMarcField` actually does as
a pipeline-internal token — but consulting that from the
round-trip violates the cardinal rule above).

**Conclusion: acceptable.** The bibliographic data — place,
publisher, date — survives intact. The reconstructed 264 ind2=1
is the RDA-modern equivalent of source 260; the underlying
information is the same; only the cataloguing-format-version
marker is lost. Records appear in the diff as one `lost` 260 row
+ one `added` 264 ind2=1 row per ProvisionActivity (~12 records
on the 500-sample, with another ~290 source-260 records lost
through a separate gap where M2-post couldn't correlate the
ProvisionActivity at all).

**Candidate BFFI extension** that would let this round-trip
verbatim: a `bffi:cataloguingConvention` predicate (analogous to
the existing `bffi:descriptionConventions` for AdminMetadata)
on the ProvisionActivity, recording the AACR2 / RDA convention
used by the source cataloguer. Open question for an NLF
conversation; not in scope for the current pipeline.

---

## L-02 — `$2 yso/fin` → `$2 yso` (vocabulary language-suffix loss)

**Case** Helmet has two historical conventions for the MARC
6XX `$2` vocabulary tag:

- **Legacy**: `$2 yso/fin` / `$2 yso/swe` — vocab code + 3-letter
  language code identifying which language's prefLabel the
  cataloguer transcribed
- **Modern**: `$2 yso` — bare vocab code; the language is implicit
  in the `$a` literal

**Source MARC** — Helmet example with legacy convention:
```
650 _7 $a musiikki $2 yso/fin
```

**BFFI graph** — `bf:source <…/subjectSchemes/yso>` with the
source bnode's `bf:code "yso"`. The language suffix is **silently
dropped at marc2bibframe2's XSL layer**; verified by a probe
showing `$2 yso/fin` and `$2 yso` produce byte-identical
BIBFRAME output.

**Reconstructed MARC**:
```
650 _7 $a musiikki $2 yso
```

**Why BFFI can't restore the suffix**: no surviving triple
carries the cataloguer's exact `$2` value. Per CLAUDE.md, we
don't modify the marc2bibframe2 submodule, so the suffix
cannot be preserved verbatim.

**Conclusion: acceptable.** The subject term and its source
vocabulary survive. Records originally tagged with the legacy
language-suffixed form lose the convention marker; the
bibliographic content (which vocab, which subject term) is
intact. Synthesis options exist (e.g., re-attach `/fin` from the
`$a` literal's language tag at conversion time) but they restore
the convention marker, not the data; deferred until the production
diff residue signals the convention-noise is worth the policy
call.

---

## L-03 — M9 authority binding swaps `$a` to authority's preferred label

**Case** When the cataloguer-supplied subject `$a` differs in
language or form from the matched authority URI's `skos:prefLabel`
(e.g., cataloguer wrote Swedish "djur" while YSO's preferred
label is Finnish "eläimet"), M9 binds the record to the authority
URI. The round-trip then renders the authority's prefLabel as
`$a`, not the original cataloguer text.

**Source MARC**:
```
650 _7 $a djur $2 yso $0 http://www.yso.fi/onto/yso/p2387
```

**BFFI graph** — after M9 binding:
```
?work bffi:subject <yso/p2387>
<yso/p2387> skos:prefLabel "eläimet"@fi, "djur"@sv, ...
```

**Reconstructed MARC**:
```
650 _7 $a eläimet $2 yso $0 http://www.yso.fi/onto/yso/p2387
```

**Why this happens**: the canonical graph carries the YSO concept
as a shared resource — every record pointing at `yso/p2387` shares
the same set of language-tagged prefLabels. The cataloguer's
original `$a` text isn't separately retained on the per-record
side; on render we pick by display-language preference (`fi, sv,
en` per `CLAUDE.md`).

**Conclusion: acceptable.** The classified subject (the YSO
concept) survives verbatim via `$0` and is the canonical
identifier of the row. The `$a` text now reflects the
authority's preferred form rather than the cataloguer's chosen
language. The diff comparator surfaces these specifically as
`language-reconciled` (distinct from `changed`) so the cataloguer
review can scan them separately.

---

## L-04 — bf:Note `mnotetype` discriminator drives 5XX tag

**Case** MARC 5XX notes (504 bibliography, 511 participants, 520
summary, 546 language, 586 awards, etc.) all become
`bf:Note` blank nodes in BIBFRAME. marc2bibframe2 categorises
them with an `rdf:type <…/mnotetype/<tail>>` discriminator:

- 504 → `mnotetype/biblio`
- 511 → `mnotetype/participants`
- 520 → `mnotetype/summary`
- 546 → `mnotetype/language`
- 586 → `mnotetype/awards`
- ...

The round-trip converter routes `bf:Note` nodes to the matching
5XX MARC tag based on this typing. **Notes with no
`mnotetype/<tail>` type fall through to MARC 500** (general note)
regardless of what the cataloguer originally wrote.

**Why this is sometimes a move**: when a categorical-note source
field (e.g., 511 participants) has a note typing that
marc2bibframe2 doesn't emit for that record — or when the
mnotetype routing table doesn't have a mapping for some rare
5XX tag — the note text survives but lands as 500 in recon.

**Conclusion: acceptable** (note text preserved; categorical tag
may broaden to 500). Helmet's rare 5XX tags
(`574` special collections, others) currently lack mnotetype
mappings on the BIBFRAME side and round-trip as 500. If a new
mnotetype tail surfaces that we want to route, extending the
mapping table in `_MNOTETYPE_TO_MARC_5XX` is a one-row change.

---

## L-05 — 740 ↔ 730 routing depends on marc2bibframe2's Hub vs Work decision

**Case** MARC 730 (uniform title - added entry) and 740 (uncontrolled
related/analytical title) are both "related title" added entries.
marc2bibframe2 routes the related title to either:

- `bf:Hub` (controlled uniform title — typically when source had
  730), or
- `bf:Work` (uncontrolled — typically 740)

Our converter renders the typing back: `bf:Hub` → 730, `bf:Work`
→ 740. When the cataloguer's intent doesn't match marc2bibframe2's
heuristic (e.g., a 740 with a sub-field pattern that triggers Hub
routing), the recon emits 730 instead.

**Conclusion: acceptable** when the actual title content survives
in the right structural shape (a related-title row at one of
730/740). Some records where source had ind2=2 (analytical entry)
become 730/740 without the ind2=2 marker because BFFI doesn't
carry the analytical-entry flag. P-52 Phase G.bis adds 700 ind2=2
emission from aggregation components for the cases where we
detected aggregation; the 730/740 ind2=2 distinction remains a
gap.

---

## L-06 — Single-source-tag entities can round-trip to a different MARC tag

**General pattern.** Several BIBFRAME shapes collapse multiple
source-MARC tags into one entity:

- 100 vs 700 (with the same agent) — both become
  `bf:contribution → bf:Agent`; distinguished only by the
  contribution's `bf:PrimaryContribution` typing
- 240 vs 730 — both become `bf:Hub` with `bf:title` chain; the
  ind1 distinction (240 ind1=1 traced, 730 ind1=0) is preserved
  but the choice between the tags depends on the Work's
  `bf:expressionOf` linkage vs `bf:relation → bf:associatedResource`

The round-trip's tag-routing for these collapsed cases reads
**structural BFFI signals only** (entity typing, relation
predicate). It does not consult `bffi-prov:fromMarcField`
(pipeline-internal) to recover the original tag, per the
cardinal rule.

**Conclusion**: data shape survives; the tag chosen on emit
follows BFFI's structural typing. Records where the source's
structural intent matched marc2bibframe2's normalisation
round-trip verbatim; records where the heuristic differs land in
the closest-tag bucket.

---

## L-07 — Enrichment-derived MARC `$4` no longer emitted (BFFI role values switch from LoC to MTS)

**Case** Pre-redesign, the round-trip emitted MARC `$4 cmp` /
`$4 trl` etc. on `100` and `700` rows for **every contribution**,
because M3's relator-term-enrichment pass lifted every Finnish
``$e`` term to a LoC relator URI (e.g. ``"säveltäjä" → <relators/cmp>``).
Source Helmet MARC essentially never has `$4` (10 occurrences in
473 k records per the 2026-06-07 corpus inventory), so this was
*enrichment*, not round-trip faithfulness — the recon added a
subfield the cataloguer never wrote on 99 % of records.

**Source-``$4`` records still round-trip verbatim.** The ~10
records where the cataloguer DID write ``$4`` in source MARC are
unaffected: marc2bibframe2 lifts the source ``$4 cmp`` to
``bf:role <relators/cmp>``, M3 propagates it as
``bffi:role <relators/cmp>`` on the canonical, and
``_collect_role_subs`` recognises the LoC-relator URI prefix and
emits ``$4 cmp`` from the URI's last path segment. The deletion
removed only the *synthesised* ``$4`` for the 99 % of records.

**Architectural reason for removal** BFFI 1.0.0 does not
designate LoC relators as the value vocabulary for
``bffi:Role``. ``vocab/lkd.rdf`` records the binding explicitly:

```xml
<rdf:Description rdf:about="…/schema:bffi:Role">
  <bffi-meta:relatedValueVocabulary rdf:resource="…/au:mts:m34"/>
  <bffi-meta:relatedValueVocabulary rdf:resource="…/au:mts:m153"/>
  <bffi-meta:relatedValueVocabulary rdf:resource="…/au:mts:m491"/>
  <bffi-meta:relatedValueVocabulary rdf:resource="…/au:mts:m1157"/>
  <owl:equivalentClass rdf:resource="…/bibframe/Role"/>
</rdf:Description>
```

The four MTS collections partition role concepts by FRBR axis
(Work / Expression / Manifestation / Item). The project's M3
enrichment pass (`relator_term_enrichment.py` +
`marc_relator_terms.py`) and the round-trip `$4` emission path
were both removed during the role-redesign; role values are now
MTS concept URIs added at M10 / Skosify time (see the
``_synthesise_role_mts_uri`` flattener in
``src/bffi_pipeline/stages/m10/skosify_run.py``).

**Source MARC**:
```
100 1   $a Tolstoy, Lev, $e kirjoittaja
700 1   $a Adrian, Esa,  $e kääntäjä
```

**BFFI graph** — Contribution carries:

  - ``bffi:role [ a bf:Role ; rdfs:label "kirjoittaja" ]`` —
    the cataloguer's free-text term, preserved verbatim by
    marc2bibframe2 and passed through M3/M8 untouched.
  - At ``canonical-skosified.ttl`` only:
    ``bffi:role <http://urn.fi/URN:NBN:fi:au:mts:m552>``
    (MTS concept "kirjoittaja" from the Work-axis collection
    ``mts:m34``). Lives in the Skosify-loaded artefact, not in
    the canonical NLF would ingest.

**Reconstructed MARC**:
```
100 1   $a Tolstoy, Lev, $e kirjoittaja
700 1   $a Adrian, Esa,  $e kääntäjä
```

**Conclusion: acceptable.** ``$e`` round-trips verbatim from the
bnode ``rdfs:label``. ``$4`` is intentionally not synthesised because:

1. **Source fidelity wins**: the cataloguer didn't write a code,
   so reconstructing the record without one matches the source.
2. **BFFI compliance**: the role URI added at M10 / Skosify is an
   MTS concept whose last URI segment (`m552`) is not a MARC
   relator code — the LoC URI → MARC-code shortcut doesn't apply
   to MTS URIs.
3. **Cataloguer review**: the reconstruction shows what was
   catalogued, not what an enrichment pipeline could infer. That
   makes diff residue mean "real data movement" rather than
   "expected enrichment delta".

---

## L-08 — `bf:*` terms still emitted in canonical (no `bffi:*` alias in `lkd.rdf`)

**Case** After P-53 (the BFFI-aliased-terms migration), the canonical
graph uses `bffi:*` consistently for every term whose
`owl:equivalentClass` or `owl:equivalentProperty` is declared in
`vocab/lkd.rdf`. ~26 terms migrated across five families
(`bffi:role`, `bffi:Agent`, `bffi:Title`, `bffi:identifiedBy`,
`bffi:Topic` / `bffi:Place` / `bffi:Person` / `bffi:Organization` /
`bffi:Meeting` / `bffi:Temporal`, `bffi:mainTitle` /
`bffi:partName` / `bffi:partNumber`, `bffi:status`,
`bffi:qualifier`, etc.).

**The remaining `bf:*` terms in canonical.ttl are NLF's deliberate
BIBFRAME reuse, not migration oversight.** BFFI 1.0.0 does not
redefine these terms — `lkd.rdf` carries no `bffi:*` counterpart.
Per the CLAUDE.md "BFFI namespace discipline" rule ("reuse an
existing standard term") and "What not to do" rule ("don't mint
local `bffi:` terms"), we emit them from the BIBFRAME namespace
directly.

**Predicates we still emit as `bf:*`:**

| `bf:*` predicate | Why no `bffi:*` alias |
|---|---|
| `bf:source` | Used as the source-vocab pointer on identifiers and authority targets. BIBFRAME's term is reused directly. |
| `bf:note` | Used as a typed-note predicate inside Manifestation. |
| `bf:date`, `bf:place`, `bf:agent` | Slots inside `bf:ProvisionActivity` blank-node chains (date/place/agent on the activity itself). |
| `bf:hasSeries`, `bf:hasInstance` | FRBR-axis linkages BFFI doesn't redefine (series + instance-of). |
| `bf:isbn`, `bf:issn`, `bf:ean`, `bf:audioIssueNumber`, `bf:systemNumber` | Skosify-stage flat-identifier display predicates emitted only in `canonical-skosified.ttl` (P-45 commit 12 — see `_synthesise_identifier_predicates` in `m10/skosify_run.py`). |

**Classes we still emit as `bf:*`:**

| `bf:*` class | Why no `bffi:*` alias |
|---|---|
| `bf:Hub` | Aggregating-entity class for related works / uniform titles. |
| `bf:Series` | Series resource (object of `bf:hasSeries`). |
| `bf:VariantTitle` | Variant-title class on the Expression's `bffi:title` chain. |
| `bf:Isbn`, `bf:Issn`, `bf:Ean` | Identifier-type classes inside `bf:identifiedBy` chains. |
| `bf:AudioIssueNumber`, `bf:SystemNumber` | Same — identifier-type classes. |

**Closed set.** Anything NOT in the two tables above must be
emitted as `bffi:*` if a `bffi:*` alias exists in `lkd.rdf`. The
namespace-discipline test in `tests/` enforces that every `bffi:*`
term in code or SPARQL exists in `lkd.rdf`, and an rdflib-based
audit (see `docs/bf_to_bffi_mapping.md`) re-derives the alias
mapping from `lkd.rdf` and flags new aliases worth migrating.
**Adding a new `bf:*` emit to the canonical graph requires an
entry in one of the two tables above (with reasoning); otherwise
the conversion routes to the `bffi:*` counterpart.**

**Boundary discipline.** This list is about what `canonical.ttl`
carries — the NLF-shippable surface. Stages and boundaries that
deliberately operate in the BIBFRAME namespace (M2's enrichment of
the marc2bibframe2-emitted graph; M3 SPARQL WHERE clauses reading
that graph) continue to use the BIBFRAME predicates and classes
exactly as marc2bibframe2 produces them. The migration is a
canonical-output property, not a pipeline-wide ban on `bf:*`
identifiers.

**Conclusion: acceptable, and design-of-record.** The mix of
`bffi:*` (for terms BFFI has aliased) and `bf:*` (for terms BFFI
deliberately doesn't) is the BFFI 1.0.0-aligned shape the project
ships. When NLF extends BFFI with a new alias (e.g., a future
`bffi:Series` or `bffi:hasInstance`), the migration playbook from
P-53 applies: invert the audit, swap the emit sites, run the
five-family verification cadence, move the row from the "still
`bf:*`" table to the "migrated" set.

---

## L-09 — MARC 880 vernacular pairing recovered heuristically; no `bffi:vernacularOf` predicate

**Case** MARC 880 is the "Alternate Graphic Representation" field — the
record's vernacular (original-script) rendering of a Latin-transliterated
field. Records with non-Latin source content (Russian / Cyrillic, Arabic,
Hebrew, CJK, Greek, Devanagari, etc.) carry both the transliterated form
in the primary tag (100/245/700/etc.) and the original-script form in a
paired 880 row. Pairing is positional via `$6`:

```
100 1   $6 880-01 $a Tolstoy, Lev Nikolaevich
245 1 0 $6 880-02 $a Voina i mir.
880 1   $6 100-01/(N $a Толстой, Лев Николаевич
880 1 0 $6 245-02/(N $a Война и мир.
```

`$6 100-01/(N` reads: "this 880 is the vernacular pair of the first 100
in this record; the script is Cyrillic (MARC code `(N` = ISO 15924
`Cyrl`)."

**BFFI graph state** BFFI 1.0.0 has the script-identification piece
(`bffi:Script` is a class, `owl:equivalentClass bf:Script`, subclass of
`bffi:Notation`; `bffi:notation` is the predicate on `bffi:Expression`).
It has the variant-form piece (`bf:VariantTitle` is used; we already
route variant titles via `bf_to_bffi_expression.rq`). What it does
**not** have is an explicit "this is the vernacular pair of that"
predicate — no `bffi:vernacularOf`, `bffi:hasVariantForm`, or similar
link between a Latin-transliterated structural form and its vernacular
counterpart. `vocab/lkd.rdf` declares no such property; the closest
neighbours are:

- `madsrdf:variantLabel` — "any variant of this label", not
  specifically "other-script form"
- BCP 47 language+script tags on `rdfs:Literal`s — works for
  string-level pairs but not for multi-subfield structural forms (a
  full 100 row carries `$a $c $d $q` + authority URI + relator code;
  one literal-language-tag can't represent the full pair)
- `bf:VariantTitle` typing on a `bf:Title` blank node — flags
  variance but doesn't say from-what

**Why BFFI can't restore the pairing structurally**: marc2bibframe2
normalises the positional MARC into a graph of structured entities,
erasing the "first 100" / "second 245" indexes. The `$6` linking field
that says "this 880 pairs with that primary row" is lost in
normalisation. Even if we ran a M3-post pass that walked `bflc:marcKey`
to recover the `$6` data, we'd have no BFFI predicate to encode the
pairing back into the canonical graph.

**Our mitigation (Solution A): re-use marc2bibframe2's existing
language-tagged-literal pairing.** A 2026-06-08 audit of
`third_party/marc2bibframe2/xsl/ConvSpec-880.xsl` (and a spot-check
on Helmet record `b18685389`) found that marc2bibframe2 already
pairs 880s natively in BIBFRAME — when both a primary tag (e.g.,
245) and its paired 880 exist, marc2bibframe2 processes them
together and emits **both** literals on the **same** BIBFRAME
entity, with `xml:lang` on the vernacular form. M3 SPARQL preserves
both literals through to canonical. Spot-check on `b18685389`:

```turtle
?provisionActivity bflc:simpleAgent  "TsJeNTRAL PARTNJeRŠIP",
                                     "ЦЕНТРАЛ ПАРТНЕРШИП"@ru ;
                   bflc:simplePlace  "Moskva",
                                     "Москва"@ru ;
                   bflc:simpleDate   "2007", "2007"@ru ;
?manifestation bffi:publicationStatement
                       "Moskva: TsJeNTRAL PARTNJeRŠIP, 2007",
                       "Москва: ЦЕНТРАЛ ПАРТНЕРШИП, 2007"@ru ;
                   bffi:responsibilityStatement
                       "režisser Sergei Ursuljak",
                       "режиссер Сергей Урсуляк"@ru .
```

So the data is already in canonical. **No M3 changes are needed.**
The pairing is implicit: one untagged literal (the primary
Latin / transliterated form) + one or more language-tagged literals
(the vernaculars) on the same entity, all under the same predicate.

**Round-trip emit (the only piece this project needs to add)**: walk
each entity for language-tagged companions of the predicates that
serialise to a paired-880-able MARC tag (245 / 260 / 264 / 100 /
110 / 111 / 130 / 240 / 246 / 247 / 700 / 710 / 711 / 730 / 740 /
800 / 810 / 830 — the set listed `convertLinked="false"` in
`map880.xml`). For each language-tagged literal companion, emit
a MARC 880 row with `$6 <primary-tag>-<seq>/<script-code>`, where
the script code is derived by Unicode-script detection on the
literal text (Cyrillic block → `(N`, Arabic → `(3`, Hebrew → `(2`,
CJK → `$1`, Greek → `(S`, etc.).

For Helmet, this resolves correctly in **99 %+ of records**
because each record has at most one vernacular pair per field —
the implicit pairing (untagged + language-tagged on the same
entity, under the same predicate) carries the structural relation.
Records with multiple non-Latin scripts in different fields (e.g.
a Russian record citing an Arabic-script book in 700) can
mis-pair. Corpus-level frequency: probably <0.1 % (verifiable post-
implementation).

**Conclusion: acceptable for the canonical we ship today; flagged
as a candidate for a future BFFI ontology extension.** The
bibliographic content survives intact (both transliterated and
original-script forms are in the graph, queryable, renderable in
Skosmos with appropriate script labels). The structural pairing —
"THIS 880 row goes with THAT 245 row" — is recovered heuristically
on round-trip rather than carried explicitly in the graph.

**Candidate BFFI extensions** for a Layer-3 NLF conversation
(precedent: P-49):

- **Solution B**: model each script-variant as its own `bffi:Expression`
  with `bffi:notation` typing, linked via `bffi:AggregatingExpression`
  under a shared Manifestation. Requires relaxing the SHACL
  `bffi:expressionManifested maxCount 1` constraint on
  `bffi:Manifestation` and overloads the aggregating-expression
  pattern (originally for music compilations) onto multi-script
  editions. ~4 % corpus growth in aggregating Expressions.
- **Solution C**: propose a new BFFI predicate `bffi:vernacularOf`
  (range `bffi:Title` / `bffi:Agent` / `bf:ProvisionActivity`) that
  carries the pairing explicitly. Aligns with how AACR2/RDA-Z39.7 +
  ISBD describe vernacular forms. Out of project scope; needs NLF.

Solution B + C remain available as escalations if Solution A's
heuristic mis-pairs measurably in production. Until then,
canonical.ttl ships with the BCP 47-tagged literal pattern and
round-trip emits 880 rows with heuristic `$6` reconstruction.

---

## L-10 — Helmet-local 09X classifications (091/092/093/094/095/097) dropped at the BIBFRAME boundary (marc2bibframe2 gap)

**Case** Helmet records carry several local-vocabulary
classification fields in the MARC 09X range:

| Tag | % corpus | Records | What it carries |
|---|--:|--:|---|
| 091 | 98.63 % | 789,496 | Helmet local-location code (e.g. `$a 77`, `$a 78`, `$a 89`) — shelf-section / collection grouping |
| 097 | 98.34 % | 787,111 | Helmet local secondary classification (often a finer-grained YKL number than 084) |
| 095 | 79.34 % | 635,036 | Helmet local additional classification (multi-`$b` — supplementary classes) |
| 092 | 54.67 % | 437,587 | Helmet local genre / format classification |
| 094 | 30.28 % | 242,336 | Helmet local class (combined main + sub-class) |
| 093 | 20.05 % | 160,462 | Helmet local primary classification (numeric scheme) |

Coverage: ~99 % of Helmet records have at least one 09X
classification field; total volume across the six tags is
~2.85 million sub-fields per-record-deduped (probably ~5 million
including repetitions).

**Source MARC** — Helmet example b10068004:
```
084   $a 78    $2 ykl
091   $a 77
092   $a 78.8935  $b 78.891
095   $a 788.33  $b 783.63  $b 788.44  $b 783.21
097   $a 78.8911
```

**BFFI graph state**: ONLY the 084 row survives. `b10068004`'s
canonical Work carries `bffi:classification` with
`bffi:classificationPortion "78"` and source code `"ykl"` —
the four 09X rows are absent.

**Why the data is lost**: marc2bibframe2's `ConvSpec-050-088.xsl`
contains a template explicitly only for MARC 084
(`xsl:template match="marc:datafield[@tag='084' or
(@tag='880' and substring(marc:subfield[@code='6'],1,3)='084')]"`).
The 050-088 spec also handles 050, 060, 070, 072, 080, 082, 083,
086 — but **not** the 09X local fields. The 09X tags fall through
to marc2bibframe2's default "drop unhandled datafield" path, so
they never appear in the BIBFRAME XML this project consumes.

This is an upstream-fork-or-walkaround decision. CLAUDE.md
forbids modifying `third_party/marc2bibframe2/`:

> Don't modify ``third_party/marc2bibframe2/`` (git submodule).
> Wrap, don't fork.

**Reconstructed MARC**: the four 09X rows do not reappear in the
round-trip. The cataloguer-review diff classifies them as `lost`
(source had data; recon doesn't).

**Conclusion: acceptable for now; documented as a Helmet-specific
upstream gap.** Three options exist for closing the gap, in
escalating order of project work:

1. **Local M2-post synthesis pass** — read the source MARC
   directly (we already have it on disk for the diff comparator),
   walk the 09X rows, mint `bf:Classification` blank nodes with
   Helmet-local source URIs (e.g.,
   `<http://urn.fi/URN:NBN:fi:bib:source:helmet-class-091>`),
   attach them to the `bf:Work` *before* M3 SPARQL runs. The
   existing `_emit_classifications` round-trip walker
   (P-54 Phase 1A) would then automatically emit them once the
   Helmet-local source codes are added to
   `_CLASSIFICATION_SOURCE_CODE_TO_MARC_TAG`. Estimated: ~3-4 h
   of Phase 1B work; deferred per the 2026-06-08 scope call.
2. **Patch the marc2bibframe2 submodule** — add 09X templates
   to `ConvSpec-050-088.xsl`. Forbidden by CLAUDE.md.
3. **Upstream contribution to marc2bibframe2** — file a PR
   against `lcnetdev/marc2bibframe2` to add 09X handling for
   library-local classifications. Long lead-time; doesn't solve
   the immediate gap.

Until Phase 1B ships, **the 09X classifications are not
recoverable from the canonical graph**. Consumers who need them
(Helmet shelf-organisation, finer-grained YKL drill-downs) must
read the source MARCXML directly.

---

## L-11 — Helmet-local 5XX broadcaster notes (574/575) dropped at the BIBFRAME boundary (marc2bibframe2 gap)

**Case** Helmet records use two non-standard 5XX note tags for
broadcast-media-related metadata:

| Tag | % corpus | Records | What it carries |
|---|--:|--:|---|
| 574 | 29.06 % | 232,618 | Broadcaster note (`$a` = broadcasting station / network name) |
| 575 | 12.90 % | 103,220 | Broadcaster of original (`$a` = original broadcaster when current record is a re-broadcast or recording-of-broadcast) |

Combined coverage: ~42 % of records carry at least one of these
fields — concentrated on the audio / video / TV / radio side of the
corpus.

**Source MARC** — Helmet example b18685389:
```
574    $a L. Järventausta
```

**BFFI graph state**: no triples derived from 574 or 575 — neither
mnotetype-typed `bf:Note` nor any other shape. The cataloguer's text
is absent from the canonical entirely.

**Why the data is lost**: `third_party/marc2bibframe2/xsl/ConvSpec-5XX.xsl`
has templates for the standard MARC 5XX tags (500, 502, 504, 505,
506, 508, 511, 513, 520, 521, 540, 545, 546, 561, 586, etc.) but
no template matching `tag='574'` or `tag='575'`. These tags are
defined locally by Helmet (Sierra / Finnish public-library
convention) and don't appear in the standard MARC bibliographic
format, so marc2bibframe2 has no mapping for them. The data falls
through marc2bibframe2's default "drop unhandled datafield" path
and never reaches BIBFRAME XML.

CLAUDE.md forbids modifying the marc2bibframe2 submodule
(`Don't modify ``third_party/marc2bibframe2/`` (git submodule).
Wrap, don't fork.`).

**Reconstructed MARC**: the 574 and 575 rows do not reappear in
the round-trip. The cataloguer-review diff classifies them as
`lost` (source had data; recon doesn't).

**Conclusion: acceptable for now; documented as a Helmet-specific
upstream gap.** Same options as L-10:

1. **Local M2-post synthesis pass** — read the source MARC for 574
   and 575, mint `bf:Note` blank nodes typed
   `rdf:type <…/mnotetype/helmet-broadcaster>` (and
   `helmet-broadcaster-orig` for 575) on the `bf:Instance`. Add
   the two new tails to `_MNOTETYPE_TO_MARC_5XX` so the round-trip
   reconstructs the rows. Estimated: ~1-2 h.
2. **Patch marc2bibframe2** — forbidden by CLAUDE.md.
3. **Upstream contribution** — file an issue with
   `lcnetdev/marc2bibframe2` for Helmet-local extension support.
   Probably won't be accepted since they're truly local fields,
   not standard MARC.

Until option 1 ships, **the broadcaster notes are not recoverable
from the canonical graph**. Consumers who need them (broadcast-
metadata pipelines, TV/radio cataloguing tools) must read the
source MARCXML directly.

---

## L-12 — No LoC `vocabulary/countries/*` ↔ YSO bridge in published authority data (Finto / NLF interoperability gap, not technically BFFI)

**Case** Every Helmet record encodes a country of publication
through MARC 008 positions 15-17 (a 3-character MARC country code,
e.g. `xxk` = United Kingdom, `fi ` = Finland, `ru ` = Russia).
`marc2bibframe2` mints
`bf:place <http://id.loc.gov/vocabulary/countries/{code}>` on the
ProvisionActivity from those bytes (`ConvSpec-Process8-ProvAct.xsl`
lines 878-884, using `$countries = "http://id.loc.gov/vocabulary/
countries/"` from `variables.xsl:26`); M3 SPARQL carries the URI
through into the canonical BFFI Manifestation; Skosify lifts the
same URI onto the Manifestation as `dct:spatial` for Skosmos
display (see `_synthesise_provision_display` in
`src/bffi_pipeline/stages/m10/skosify_run.py:229`).

The LoC countries vocabulary publishes **only English `rdfs:label`
literals** for these URIs. The Finnish-cataloguing audience needs
`skos:prefLabel @fi` (`"Suomi"`), `@sv` (`"Finland"`), `@en`
(`"Finland"`) — the project's display priority. **YSO main**
(`http://www.yso.fi/onto/yso/`, the same Fuseki graph that
`load_finto.py` already loads) has these multilingual labels for
every modern country (`yso:p94426` "Suomi"@fi / "Finland"@sv /
"Finland"@en; `yso:p94479` "Venäjä"@fi / "Ryssland"@sv /
"Russia"@en; etc.) — note that the country-level concepts live
in main YSO, not in the YSO-paikat sub-vocabulary as one might
expect from the name. But **neither side publishes a
`skos:exactMatch` / `owl:sameAs` bridge** between the two URI
spaces:

- LoC's `countries.skos.rdf` carries no Finto cross-references.
- YSO main concepts carry no LoC `vocabulary/countries/*`
  exactMatches (spot-checked Finland, Sweden, Russia, France
  on the Finto Skosmos UI — none cite the LoC URI).
- Wikidata DOES have a property `P3866` ("MARC country code")
  that maps Wikidata items to LoC MARC codes AND a property
  `P2347` ("YSO ID") that maps the same items to YSO concepts,
  so a Wikidata-mediated crosswalk exists but isn't built into
  either authority's published SKOS feed.

**Source MARC** — Helmet example b18685389 (Russian DVD,
008 positions 15-17 = `ru `):
```
008   …090714s2007    ru 322        s   vlrus d
```

**BFFI graph state**:
```turtle
<…manifestation:469…> bffi:provisionActivity [
    a bf:ProvisionActivity, bf:Publication ;
    bf:place <http://id.loc.gov/vocabulary/countries/ru> ;
    …
] ;
dct:spatial <http://id.loc.gov/vocabulary/countries/ru> .
```
Neither `<countries/ru>` nor any peer concept carries a
multilingual prefLabel in the canonical graph or in the Fuseki
graphs `load_finto.py` populates. Cataloguer-review surfaces, the
typing-review HTML, and the Skosmos concept pages render the bare
URI tail (`ru`, `fi`, `xxk`) instead of `"Venäjä"` / `"Suomi"` /
`"Englanti"`.

**Reconstructed MARC**: not affected — round-trip recovers the
008 positions from `bf:place` regardless of what labels we
attach to the URI.

**Why the data quality is degraded**: the URI is correct and
stable; what's missing is the *translation surface* a Finnish
cataloguer-review consumer expects. The label gap exists in
authority-data interop space, **not** in BFFI's ontology or in
marc2bibframe2's conversion — both correctly preserve the URI.
The gap manifests because:

1. LoC publishes the canonical MARC-country URIs but in their
   own scheme (`<vocabulary/countries/*>`), with English-only
   labels.
2. Finto / NLF curates equivalent place concepts in YSO-paikat
   with NLF-quality multilingual labels, but in YSO-paikat URIs.
3. Neither published authority connects the two — the Wikidata
   crosswalk is the only bridge and it isn't in either feed.

**Conclusion: documented as an authority-interop gap; addressed
locally with a project-owned bridge graph; escalated to NLF for
upstream resolution.**

Three resolution paths, in ascending order of scope:

1. **Local bridge graph (interim)** — vendor a small CC0 TTL
   under `vocab/loc-countries-bridge.ttl` with `skos:exactMatch`
   (to YSO main) plus inlined `skos:prefLabel @fi/@sv/@en`
   (cached from YSO at file-creation time) per LoC country code.
   A Skosify pass `_synthesise_country_labels` walks every
   `bf:place` / `dct:spatial` reference in the canonical graph
   and copies the bridge's prefLabels onto the LoC URI before
   write-out — Skosmos then renders the multilingual labels on
   the LoC concept page without needing to follow the
   `exactMatch` at query time. Bridge is small (the 500-sample
   has ~21 distinct codes; full corpus likely ~150-200; LoC's
   total is ~290 including historical codes like
   `yu` Yugoslavia, `gx` East Germany). Light maintenance:
   countries change rarely, the file is appended manually when
   a missing code surfaces in a run.

2. **Wikidata-derived snapshot (richer alternative to path 1)** —
   use Wikidata's `P3866` and `P2347` properties to mint the
   bridge from a single SPARQL query against
   `query.wikidata.org`, plus a fi/sv/en/de/fr label pull on
   the same Wikidata items as a fallback when YSO-paikat lacks
   coverage. Snapshot to a CC0 TTL vendored alongside path 1.

3. **Upstream contribution to YSO-paikat (long-term)** — propose
   to NLF / Finto that YSO-paikat publish `skos:exactMatch`
   triples to `<http://id.loc.gov/vocabulary/countries/*>` for
   every place concept that has an unambiguous MARC-country
   counterpart. This is the canonical fix — it benefits every
   downstream consumer of NLF authority data, not just this
   pipeline — and is on the table once we have a
   working bridge to point at as a proof-of-concept.

The local bridge (path 1, optionally extended by path 2) is the
near-term plan. Adding the YSO-paikat `exactMatch` triples to
the upstream feed is a candidate for an NLF / Finto conversation;
not in scope for the current pipeline.

---

## L-13 — Personal-name sub-components ($b/$c/$d/$q on 100/600/700/etc.) and 730/240 subfield boundaries ($l/$o/$h) round-trip via `bflc:marcKey` only

**Case** MARC fields modelling people and uniform titles carry rich
sub-component structure inside a single datafield. For personal
names (X00, 600, 700) the source typically looks like
```
600 1 4  $a Hiiri, Mikki,  $c (fiktiivinen hahmo),  $d 1928-
700 1 _  $a Brueghel, Pieter,  $c the Elder,  $d -1569
```
and for uniform titles (240, 730)
```
730 0 _  $a Tomtarnas julnatt,  $l suomi (Tonttujen jouluyö); $o sov., mieskuoro / $g Sefve, Vilhelm
240 1 0  $a Symphony,  $n no. 5,  $r C minor,  $s arr.
```
The individual subfields carry typed sub-components (title-with-name
$c, dates $d, fuller name $q, numeration $b, language of work $l,
arrangement $o, key $r, version $s). MARC's data model treats these
as distinct subfields with cataloguer-meaningful boundaries.

**BFFI graph state**: marc2bibframe2 collapses every personal-name
sub-component into a single `rdfs:label` literal on a `bf:Agent` /
`bf:Person` / `bf:Organization` node and preserves the boundary
information ONLY in a parallel `bflc:marcKey` literal:
```turtle
<#Agent600-21> a bffi:Person ;
    rdfs:label "Mikki Hiiri (fiktiivinen hahmo)" ;
    bflc:marcKey "60004$aMikki Hiiri$c(fiktiivinen hahmo)" .
```
Same shape for uniform-title Hubs:
```turtle
<#Hub730-46> a bf:Hub ;
    bflc:marcKey "7300 $aTomtarnas julnatt,$lsuomi (…) /$gSefve, Vilhelm" ;
    bffi:title [ bffi:mainTitle "Tomtarnas julnatt, suomi (…) / Sefve, Vilhelm" ] .
```

**Reconstructed MARC** (with marcKey-driven recovery shipped after
the 2026-06-09 round-trip diff scan):
```
600 _ 7  $a Mikki Hiiri  $c (fiktiivinen hahmo)
730 0 _  $a Tomtarnas julnatt,  $l suomi (…)  $o sov., …  $g Sefve, Vilhelm
```
The subfield boundaries survive because `_name_subfields_from_marc_key`
(for 6XX/7XX) and the `marcKey-first` tier in `_related_title_subfields`
(for 730) + `_emit_uniform_title` (for 240) parse the `bflc:marcKey`
literal verbatim. **Without marcKey, those subfields collapse into
$a** — the b20122470 / b12191139 reproducers in
`scratchpad/2026-06-09-roundtrip-diff.md`.

**Why no structured BFFI predicate covers this**:

1. **`vocab/lkd.rdf` does not define name-component predicates**.
   Spot-checked: no `bffi:nameDate`, `bffi:nameTitle`,
   `bffi:nameNumeration`, `bffi:nameFullerForm`, no equivalent
   `bffi:titleLanguage` on the Title node, no
   `bffi:titleMediumQualifier`. `bf:Agent` / `bffi:Person` carry
   `rdfs:label` and nothing more granular.

2. **The BFLC ontology *does* define them**
   (`bflc:date`, `bflc:title`, `bflc:numeration`, `bflc:fuller`),
   but those terms are not aliased in `vocab/lkd.rdf`. The project's
   BFFI namespace discipline allows direct BIBFRAME / BFLC use, so
   we COULD emit `bflc:date` / `bflc:title` / etc. on canonical
   agents — but only if marc2bibframe2 emits them in the first place.

3. **marc2bibframe2 does not emit them either**. I enumerated every
   `bflc:*` term marc2bibframe2's XSL produces:
   ```
   AppliesTo, CreatorCharacteristic, DemographicGroup, EncodingLevel,
   GovernmentPubType, MachineModel, MetadataLicensor,
   MovingImageTechnique, OperatingSystem, ProgrammingLanguage,
   SerialPubType, SeriesAnalysis/Classification/Numbering/…/Tracing,
   applicableInstitution, appliesTo, citation, creatorCharacteristic,
   encodingLevel, governmentPubType, marcKey, metadataLicensor,
   movingImageTechnique, nonSortNum, projectedProvisionDate,
   serialPubType, seriesTreatment, simpleAgent, simpleDate, simplePlace
   ```
   No `bflc:date`, `bflc:title`, `bflc:numeration`, `bflc:fuller`,
   `bflc:titleLanguage`, `bflc:arrangement`. The
   `ConvSpec-1XX,7XX,8XX-names.xsl` template builds a combined
   `rdfs:label` via `tChopPunct` and writes `bflc:marcKey` alongside;
   the sub-component boundaries live nowhere else.

**Conclusion: marcKey IS the preservation mechanism — acceptable.**
marc2bibframe2's implicit design contract: `rdfs:label` is for
human display, `bflc:marcKey` is for structured fidelity. The
round-trip respects that contract by parsing marcKey when subfield
structure matters. The dependency is flagged with `$9 marckey-bypass`
on each emitted row so the audit surfaces it.

**Out-of-scope future paths**:

1. **Propose new BFFI predicates to NLF.** Candidate set:
   `bffi:nameDate`, `bffi:nameTitle`, `bffi:nameNumeration`,
   `bffi:nameFullerForm` on `bffi:Agent`; `bffi:titleLanguage`,
   `bffi:titleMediumQualifier`, `bffi:arrangementStatement` on
   `bffi:Title`. Solves the BFFI ontology side. Doesn't fix the
   marc2bibframe2 emit gap — would still need either a fork
   (forbidden) or an M2-post pass that parses source MARC and emits
   the structured triples ahead of M3.

2. **Pre-M3 enrichment pass** that parses source MARC directly and
   writes `bflc:date` / `bflc:title` / etc. onto the bib-raw entity
   before M3 SPARQL fires. The marcKey is already there — this would
   just hoist parsed components onto dedicated predicates. Mostly
   benefits downstream LKD consumers (queryable name components)
   rather than the round-trip (which has marcKey directly). Estimated:
   ~4-6 h.

3. **Wait for marc2bibframe2 upstream.** If marc2bibframe2 ever
   adopts the BFLC name-component predicates, our pipeline would
   inherit them automatically. No timeline; not assumed.

The marcKey-driven recovery in `src/bffi_pipeline/marc_roundtrip/converter.py`
(`_name_subfields_from_marc_key`, `_related_title_subfields`,
`_emit_uniform_title`) covers the round-trip use case completely.
Downstream consumers needing queryable sub-components should either
parse marcKey themselves or wait for path 2 above.

---

## How to add a new entry

When you find a round-trip case where:

1. Bibliographic data survived (place / title / agent / subject /
   etc. all present in recon)
2. But landed in a different MARC field, with a different
   sub-field structure, or under a different convention than the
   source had
3. And the divergence comes from a BFFI / BIBFRAME ontology
   shortfall (not from a code bug)

…add a new section above with the `L-NN` numbering. Include the
five elements: case description, source MARC sample, BFFI graph
state, reconstructed MARC sample, why-BFFI-can't-restore-it, and
conclusion (acceptable / planned-fix / deferred). Cross-link to
any related plan in `docs/plans/` when one applies.

When a `bffi:` extension would fix the case, flag it as a candidate
for an NLF conversation rather than minting locally — the BFFI
namespace stays closed to what `vocab/lkd.rdf` declares (per the
**BFFI namespace discipline** rule in CLAUDE.md).
