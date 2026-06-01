# Role-divergence MARCXML — real Helmet records

Four **real** Helmet bibliographic records selected on 2026-05-26 to
exercise a specific BFFI modelling concern: **the same agent appearing
in different `bf:Contribution` roles across different Works**, and (as
a side-case) **a single MARC entry that carries multiple relators that
must expand into multiple Contribution nodes** on the same Work.

These are not part of the cataloguer-supplied Ask 1 set in `curated/` —
the cataloguer ask was about *coverage* of resource types and biblio
shapes (translation, aggregate, sheet music, etc.). The concern this
set targets is *graph shape*, found by us while inspecting the corpus.
Filenames are Helmet bib IDs as exported from
`helmet-sierra-data-tools/output/marcxml/`; contents are unmodified
MARCXML.

## Why a subdirectory and not flat alongside the synthetic set?

Same reason as `curated/`. The M2 integration test
(`tests/integration/test_marc_to_bf.py`) calls `run(FIXTURES, ...)` on
the parent `sample-marcxml/` directory; M2's
`src/bffi_pipeline/stages/m2/convert.py` walker is non-recursive
(`input_dir.glob("*.xml")`). Files in this subdirectory are invisible
to that test, so `test_summary_counts`'s `VALID_IDS` pin stays
unchanged. Tests that want these records import this path explicitly.

## The records

### The headline pair — same agent, different role across Works

All four records carry KANTO `$0 (FI-ASTERI-N)000045590` on every
appearance of Jansson, so reconciliation will collapse them to one
`bf:Agent` and the role divergence is purely a contribution-graph
concern, not an agent-identity concern.

| Bib ID | Title | MARC entry for Jansson | Expected BFFI role |
|--------|-------|------------------------|--------------------|
| `1316900` | Taikatalvi (1973, Trollvinter → suomi) | `100 1# $a Jansson, Tove, $e kirjoittaja. $0 (FI-ASTERI-N)000045590` | Primary contribution on Work, author role |
| `2302793` | Hobitti, eli, Sinne ja takaisin (2017) | `700 1# $a Jansson, Tove, $e kuvittaja. $0 (FI-ASTERI-N)000045590` (Tolkien holds 100) | Non-primary contribution on Work, illustrator role |
| `1645190` | Hobitti, eli, Sinne ja takaisin (2003) | `700 1# $a Jansson, Tove, $e kuvittaja. $0 (FI-ASTERI-N)000045590` (Tolkien holds 100) | Same as 2302793, second edition — duplicate of the pattern across editions |

The 2003 and 2017 Hobitti records together also exercise the
expression-vs-work distinction for the same Tolkien Work: two
Expressions sharing a parent Work, both with the same illustrator
contribution.

### Side-case — multi-relator `$e $e` on a single MARC entry

| Bib ID | Title | MARC entry for Jansson | Expected BFFI shape |
|--------|-------|------------------------|---------------------|
| `2347992` | Näkymätön lapsi ja muita kertomuksia (2018) | `100 1# $a Jansson, Tove, $e kirjoittaja, $e kuvittaja. $0 (FI-ASTERI-N)000045590` | **Two** `bf:Contribution` nodes on the Work — author and illustrator — both referencing the same agent. Not one Contribution with two roles. |

This is the round-trip case for Skosmos role facets: collapsing into a
single Contribution-with-many-roles loses the per-role facet at the
Skosmos surface. The fixture pins the expected expansion.

### Known data-quality gap pinned by this set

`1316900` (Taikatalvi) records "illustrated by the author" only in a
`500 ## $a Tekijän kuvittama` free-text note — there is no
relator-coded illustrator entry on the record. The BFFI Work for
Taikatalvi will therefore **not** carry an illustrator contribution.
This is expected behaviour per the project rule against mining 500
free-text notes; the fixture exists in part to make that expectation
explicit and testable as a negative assertion.

## Consuming plan

[**P-39**](../../../docs/plans/backlog/p-39-m9-non-primary-contribution-reconciliation.md)
— *M9 reconciles non-primary contributions against KANTO* — is the
plan that directly targets the population this fixture exemplifies. The
Hobitti `700 $e kuvittaja` records are precisely the
already-KANTO-bound case that P-39 Phase B.1's walker should skip
(`prov:specializationOf <kanto-uri>` already present), while a future
test against this fixture can pin that the walker emits zero
`EntityRequest`s for these four records once F1 / F2 propagation has
landed their canonical contributions.

The intra-Work multi-relator expansion (record `2347992`) is more of an
M3 (`bf-to-bffi`) concern than an M9 one — it shapes how marc2bibframe2
output is mapped into the BFFI contribution graph. No active plan
currently pins that expectation explicitly; this README is the closest
thing to a written-down contract for it until M3 cascade work picks it
up.

## What this set does **not** cover

- A case where the *same Work* has Jansson as primary contributor on
  one expression and non-primary on a translation (would require a
  Moomin record where she's only in 700 — none was found in the 1,886
  hits sampled).
- VIAF-only contributors (no KANTO `$0`). All four records' Jansson
  entries carry the KANTO ID.
- Non-Finnish, non-Latin-script authority IDs.
- Compatibility regressions when the agent's `$0` differs across records
  (e.g. legacy entries with no `$0` at all). For that, see `curated/`'s
  `1059592` (pre-RDA, no KANTO IDs).
