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

| Bib ID | Title | MARC entry | Expected BFFI shape |
|--------|-------|------------|---------------------|
| `2347992` | Näkymätön lapsi ja muita kertomuksia (Jansson, 2018) | `100 1# $a Jansson, Tove, $e kirjoittaja, $e kuvittaja. $0 (FI-ASTERI-N)000045590` | Two `bf:Contribution` nodes on the Work — author and illustrator — both referencing the same agent. Not one Contribution with two roles. |
| `2495433` | Ruiskumestarin talo (Jäppinen, 2014) | `100 1# $a Jäppinen, Jere, $e kirjoittaja, $e toimittaja. $0 (FI-ASTERI-N)000100912` + `700 1# $a Lindqvist, Mikko, $e kirjoittaja. $0 (FI-ASTERI-N)000117973` | `100`-side multi-relator (author + editor) + a `700` co-author with separate KANTO. The Work also has bilingual original cataloguing (`041 1# $a fin $a swe $h fin`) — exercises **`041` multi-`$a` parallel-language** on top of the role split. |
| `2569592` | Hyvästejä jättämättä (Meri, 2023) | `100 1# $a Meri, Tapio, $e kirjoittaja, $e kustantaja. $0 (FI-ASTERI-N)000215129` | Same person as author **and** publisher on the same Work — a self-publishing case. Forces M3 to express one agent in both a `bf:Contribution` (author) and an instance-level `bf:provisionActivity` (publisher), without collapsing them. |
| `2578080` | Concerti per una vita (Langlois de Swarte, 2024) | `100 1# $a Langlois de Swarte, Théotime, $e viulu, $e johtaja.` **+** `700 1# $a Langlois de Swarte, Théotime, $e johtaja.` **+** `700 1# $a Langlois de Swarte, Théotime, $e viulu.` | **Same agent encoded TWO ways within one record**: once as multi-relator on `100`, then again as **two separate `700` entries** (one per role) for the same person. The merge logic must recognise this as one agent with role-set `{viulu, johtaja}`, not three separate agents. No KANTO `$0` on any of the three appearances — exercises the no-KANTO branch of the merge. Audio recording (leader6=`j`). |

The four records together exercise:

- multi-relator on `100` (author+illustrator, author+editor, author+publisher, performer+conductor)
- the same multi-role encoded as `100 $e $e` **and** as separate `700` entries
  in the same record (`2578080`)
- self-contribution where the agent plays both creative and publishing
  roles (`2569592`)
- multi-relator coexisting with `041 $a $a` parallel-language originals
  (`2495433`)
- audio-recording (leader6=`j`) and text-monograph variants of the
  same shape

This is the round-trip case for Skosmos role facets: collapsing into a
single Contribution-with-many-roles loses the per-role facet at the
Skosmos surface. The fixtures pin the expected expansion.

### Cross-language reach of the same agent — 1990s KANTO-era Polish translation

| Bib ID | Title | MARC entry for Jansson | Year | Notes |
|--------|-------|------------------------|------|-------|
| `2541420` | *Opowiadania z Doliny Muminków* (Polish translation of *Det osynliga barnet*) | `100 1# $a Jansson, Tove, $e kirjoittaja, $e taiteilija. $0 (FI-ASTERI-N)000045590` | 1997 | Polish-language target (`041 1# $a pol $h swe`). Same KANTO-bound Jansson agent (`(FI-ASTERI-N)000045590`) that participates in the role-divergence pair `1316900` / `2302793` here also participates in this Polish-language Expression of the *Näkymätön lapsi* Work. Extends the agent's reach across **fi / swe / eng / pol** in the fixture pool, with KANTO present in every appearance — the cleanest "same agent across many language Expressions" case in the set. Also the only 1990s-era record in `role-divergence/`, partial mitigation of the otherwise-2010s-and-later temporal bias. |

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
