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
intact. P-51 (`docs/plans/proposed/p-51-subject-vocab-language-suffix.md`)
proposes synthesis options (e.g., re-attach `/fin` from the `$a`
literal's language tag) — those are convention-restoration, not
data restoration; deferred until the production
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
``bffi:Role``. ``docs/lkd.rdf`` records the binding explicitly:

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
`docs/lkd.rdf`. ~26 terms migrated across five families
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
`tests/unit/test_bffi_namespace_discipline.py` test enforces that
every `bffi:*` term in code or SPARQL exists in `lkd.rdf`; a
companion audit (the script under § "Verification of the alias
mapping" in `docs/plans/in-progress/p-53-bffi-aliased-terms-migration.md`)
re-derives the alias mapping from `lkd.rdf` and flags new aliases
worth migrating. **Adding a new `bf:*` emit to canonical.ttl
requires either an entry in one of the two tables above (with
reasoning) or a migration plan to the `bffi:*` counterpart if one
exists.**

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
any related plan in `docs/plans/`.

When a `bffi:` extension would fix the case, flag it as a candidate
for an NLF conversation rather than minting locally — the BFFI
namespace stays closed to what `docs/lkd.rdf` declares (per the
**BFFI namespace discipline** rule in CLAUDE.md).
