# Anonymous main entry — canonical-Work mint rules

A reference for **cataloguers** + **pipeline operators**. Describes
exactly which MARC21 fields the BFFI pipeline reads when a record
lacks a primary creator (MARC 100/110/111), and how those fields
combine into a deterministic canonical-Work URI.

Implementation lives at
[`src/bffi_pipeline/stages/merge.py`](../src/bffi_pipeline/stages/merge.py)
(M8 stage). Plan + rationale + bench numbers:
[`docs/plans/completed/p-34-m8-mint-anonymous-main-entry-works.md`](plans/completed/p-34-m8-mint-anonymous-main-entry-works.md).

## TL;DR

Every record gets a canonical Work URI. The rule that mints it
depends on which MARC fields the cataloguer set:

| Has MARC 100/110/111? | Has any MARC 700/710/711? | Mint rule | Canonical Work tagged with |
|---|---|---|---|
| **Yes** | (any) | **Primary-author-anchored** — standard path, `(creator_uri, title)`. | `bffi-prov:mintAnchor = <bib:auth/primary-author-anchored>` |
| No | Yes (any non-translator role) | **First-contributor-anchored** — first non-translator 700/710/711 anchors the mint. | `bffi-prov:mintAnchor = <bib:auth/first-contributor-anchored>` |
| No | Yes (translator only) OR No | **Anonymous-work-anchored** — synthetic anchor from (title, content-type, language). | `bffi-prov:mintAnchor = <bib:auth/anonymous-work-anchored>` |
| No, AND no title (MARC 245$a absent) | (any) | **Mint failure** — record can't be canonicalised; ends up in `canonical-mint-failures.jsonl`. | — |

Cataloguer reads the predicate to know which rule fired on any given
canonical Work — and can flag records where the rule got it wrong.

## Rule 1 — Primary-author-anchored (standard)

**When it fires**: MARC 100, 110, or 111 carries a creator.

**Mint key**: `(creator_agent_uri, normalised_pref_label)` →
SHA-1 → `http://urn.fi/URN:NBN:fi:bib:work:<sha1>`.

**MARC inputs**:

| MARC field | BFFI predicate | Used as |
|---|---|---|
| 100 / 110 / 111 + `$0` (authority URI) | `bffi:contribution → bffi:PrimaryContribution → bffi:agent` (URI) | `creator_agent_uri` |
| 245$a (+ `$b`) | `skos:prefLabel` on Work + Expression | `pref_label` |

**Notes**:
- `creator_agent_uri` is the marc2bibframe2-minted local Agent URI
  (e.g. `…raw/<bib_id>#Agent100-1`) unless an authority lookup at M9
  later replaces it with the KANTO URI.
- The mint hash uses the un-reconciled local Agent URI on purpose —
  M8 must run before M9, and the canonical Work URI must be stable
  across re-runs whether M9 has resolved authorities or not.

## Rule 2 — First-contributor-anchored (P-34 Phase A)

**When it fires**: MARC 100/110/111 absent AND at least one
MARC 700/710/711 with a non-translator role.

**Mint key**: `(first_contributor_uri, normalised_pref_label)` —
same hash function as Rule 1, just with a different anchor.

**MARC inputs**:

| MARC field | BFFI predicate | Used as |
|---|---|---|
| 700 / 710 / 711 + `$0` (authority URI) | `bffi:contribution → bffi:agent` on either the Work OR the linked Expression (M3 routes most 700s to the Expression) | `first_contributor_uri` |
| 245$a (+ `$b`) | `skos:prefLabel` on Work + Expression | `pref_label` |

**Tie-break rule when there are multiple non-translator 700s**:
lexicographically smallest Agent URI wins. The Agent URIs M3
generates have the form `…raw/<bib_id>#Agent700-N` where N is the
field index (e.g. `#Agent700-40`, `#Agent700-41`). Lex-min
gives a deterministic pick across re-runs.

**Translator-role blocklist (intellectually wrong as anchor)**:

| Role marker | Where it appears in BFFI |
|---|---|
| `<http://id.loc.gov/vocabulary/relators/trl>` | `bf:role` URI |
| Free-text label: `kääntäjä` (fi), `översättare` (sv), `translator` (en), `übersetzer` (de) | `bf:role → bf:Role → rdfs:label` |

Comparison is case-insensitive (NFKC + casefold). If every 700 on a
record is a translator, the record falls through to Rule 3.

**Cataloguer-spot-check moment**: edited compilations and anthologies
typically carry the editor in 700 with `$e toimittaja` (fi),
`$e redaktör` (sv), `$e editor` (en). All of those anchor cleanly
under Rule 2. If you spot a canonical Work tagged
`bffi-prov:mintAnchor = <bib:auth/first-contributor-anchored>` that
got the wrong anchor (e.g., the cataloguer wanted the publisher to
anchor instead), flag the record + tell the pipeline team — we
extend Rule 2's input list (or move that case to Rule 3 manually).

## Rule 3 — Anonymous-work-anchored (P-34 Phase B)

**When it fires**: MARC 100/110/111 absent AND no usable 700/710/711
(either no 700s at all, or every 700 is a translator).

**Mint key**: `(synthetic_anchor_uri, normalised_pref_label)` —
where the synthetic anchor is itself a hash of three components.

**Synthetic anchor URI**:

```
http://urn.fi/URN:NBN:fi:bib:anonymous-work-anchor/<sha1(normalised_title|content_uri|language_uri)>
```

**MARC inputs**:

| MARC field | BFFI predicate | Used as |
|---|---|---|
| 245$a (+ `$b`) | `skos:prefLabel` on Work + Expression | `normalised_title`. Normalisation: NFKC Unicode + diacritic-fold (Latin only; preserves Finnish `åäö`) + casefold + whitespace-collapse, via `bffi_pipeline.blocking.fold_label`. **Trailing MARC subfield-delimiter punctuation (`:`, `/`, `,`) is NOT stripped today** — known limitation. |
| Leader/06 + 336$a$b$2 | `bffi:content` URI on the linked Expression (LoC `contentTypes/*` vocab — e.g. `txt`, `prm`, `ntm`, `crn`, `cnd`) | `content_uri` verbatim |
| 008/35-37 OR 041$a | `bffi:language` URI on the linked Expression (LoC `languages/*` vocab — e.g. `fin`, `swe`, `eng`) | `language_uri` verbatim |

**Merge semantics**: two anonymous records merge iff all three
components match.

| Records | Same canonical? | Why |
|---|---|---|
| "Karjala" (text, fi) + "Karjala" (text, fi) | yes | likely the same intellectual content; the canonical aggregates them |
| "Karjala" (text, fi) vs "Karjala" (sound, fi) | no | different content type → different intellectual work |
| "Karjala" (text, fi) vs "Karjala" (text, sv) | no | likely a translation; cataloguer adjudicates whether to unify under same canonical |
| "Karjala :" (text, fi) vs "Karjala" (text, fi) | **no, today** | trailing punctuation isn't stripped; flagged as a known limitation — see "Known limitations" below |

**Year is deliberately NOT in the anchor.** Two editions of the same
anonymous text in the same language collapse into one canonical Work
today. Rationale: publication-year extraction (MARC 008/06-14 / 264$c
/ 260$c) isn't on `bffi:Work` today; plumbing it through M3 wasn't
justified for the bench's residual (45 records out of 4906 on the
2026-05-14 helmet-5k sample). If you find two editions that should
have stayed distinct, flag the records — that's the signal that
motivates the extra extraction work.

## Predicate that tells you which rule fired

Every canonical Work carries exactly one of:

```
<canonical-Work-URI> bffi-prov:mintAnchor <bib:auth/primary-author-anchored> .
<canonical-Work-URI> bffi-prov:mintAnchor <bib:auth/first-contributor-anchored> .
<canonical-Work-URI> bffi-prov:mintAnchor <bib:auth/anonymous-work-anchored> .
```

**Skosmos shows it as a small badge on every canonical Work page**
(once a UI tweak adds the predicate to the rendered metadata box;
not done today).

**SPARQL queries** for cataloguer audit:

```sparql
# Count canonical Works per anchor rule
PREFIX bffi-prov: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#>
SELECT ?rule (COUNT(?w) AS ?n) WHERE {
    GRAPH <http://urn.fi/URN:NBN:fi:bib:graph:bffi-works> {
        ?w bffi-prov:mintAnchor ?rule .
    }
} GROUP BY ?rule
```

```sparql
# All editor-anchored canonical Works with their titles
PREFIX bffi-prov: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?w ?label WHERE {
    GRAPH <http://urn.fi/URN:NBN:fi:bib:graph:bffi-works> {
        ?w bffi-prov:mintAnchor <http://urn.fi/URN:NBN:fi:bib:auth/first-contributor-anchored> ;
           skos:prefLabel ?label .
    }
} LIMIT 100
```

## Known limitations + cataloguer-fixable cases

1. **Trailing MARC punctuation in 245$a not stripped.** `"Karjala :"`
   and `"Karjala"` (same MARC content, just different subfield-
   delimiter cleanup) currently produce different anchors under
   Rule 3. If this proves common in real data, the fix is a one-
   line addition to the title normalisation. Flag records.
2. **No publication-year disambiguation under Rule 3.** Two
   editions of the same anonymous title in the same language
   collapse into one canonical Work. Flag if you find a problematic
   case — that motivates plumbing year through M3.
3. **Lex-min tie-break under Rule 2 is arbitrary.** If the
   cataloguer-intended "primary editor" for an edited compilation
   is consistently NOT the lex-min `#Agent700-N` URI, we'd want
   a heuristic that picks by role (editor preferred over
   illustrator preferred over compiler etc.). Today's anchor pick
   is purely positional. Flag records where the "wrong editor"
   anchors a canonical Work.
4. **Translator-role blocklist is label-based + URI-based, not
   subfield-coded.** If MARC 700`$e` uses a Finnish translator term
   we haven't enumerated (e.g. `tulkki`, `käännöksen tarkistaja`),
   we'll anchor on a translator. Send the term and we add it to
   the blocklist.
5. **No 245$a → no canonical Work.** Records that lack both a
   primary creator AND a title (very rare) end up in
   `canonical-mint-failures.jsonl` with
   `missing_inputs=["pref_label"]`. They're not minted. Fix the
   source MARC.

## How to report a rule-fired-wrong case

For each record:

- Helmet bib_id (e.g. `b16106131`).
- Source MARC field that was missed or misclassified (e.g.
  "this is a translator's primary work; 700 `$e kääntäjä` should NOT
  block it from being the canonical-Work anchor").
- Expected canonical-Work identity (which records should have merged
  / stayed distinct).
- The current `bffi-prov:mintAnchor` value (read from Skosmos or
  the SPARQL queries above).

Send to the pipeline team. Each pattern that recurs gets a one-line
rule extension in M8; the next run picks up the fix.

## See also

- [`docs/plans/completed/p-34-m8-mint-anonymous-main-entry-works.md`](plans/completed/p-34-m8-mint-anonymous-main-entry-works.md)
  — the plan-of-record + bench numbers.
- [`src/bffi_pipeline/stages/merge.py`](../src/bffi_pipeline/stages/merge.py)
  — the implementation
  (`_primary_agent_uri`, `_first_contribution_agent_uri`,
  `_anonymous_work_anchor_uri`, `_emit_mint_anchor`).
- [`src/bffi_pipeline/provenance/vocab.py`](../src/bffi_pipeline/provenance/vocab.py)
  — the `mintAnchor` predicate + value URIs.
