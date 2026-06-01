# Format-variant linkage — 776 / 780 / 785

Three records sampled on 2026-06-01 from the 30k-record cataloguer
discovery corpus
(`scratchpad/cataloguer-pick-2026-06-01.{txt,csv,md}`). Together they
exercise **MARC's linking-entry fields** for Expression-to-Expression
and Work-to-Work relationships across format and title-change
boundaries:

- `776` — *Additional physical form available* (same Work, different
  format — e.g. print ↔ ebook)
- `780` — *Continues* (this serial is a continuation of an earlier
  one)
- `785` — *Continued by* (this serial is continued by a later one)

The 30k discovery corpus contained **451 records (1.5 %)** with at
least one of these three fields — distributed as 776=355, 780=29,
785=40 (plus overlaps). The `curated/` set has zero coverage; this
subdirectory closes that gap with one record per pattern.

## Records

| Bib ID | Title | Pattern | What it exercises |
|--------|-------|---------|-------------------|
| `1837972` | *Tuhat loistavaa aurinkoa* (Hosseini, fin translation, 2007) | **`776` print ↔ ebook** | `776 08 $i Verkkoaineisto: $z 9789511440932` — the print edition (catalogued as `nam` / leader byte 6 = `a`) links to its e-book sibling by ISBN. Two `bf:Instance`s of the same Work / Expression, differing only in `bf:carrier`. The link is by ISBN string only — no bib-ID cross-reference, so BFFI must resolve the sibling Instance by ISBN lookup, not by linked identifier. |
| `1599669` | *ET-lehti* (Espoo edition, 1982–) | **`780` serial continues** | `780 00 $t ET : eläketieto - elämäntaito $d 1973-1982, $x 0355-7227` — this Espoo-published serial continues an earlier Helsinki-published one (different bib record, ISSN given). Leader byte 7 = `s`. Tests Work-to-Work succession modelling: the predecessor's URI must be reachable from this record's ISSN string, and the predecessor's own `785` (continued by) should round-trip back to this record. |
| `1904380` | *Nykytekstiili* (Helsinki, 1954-1997) | **`785` continued by** | `785 00 $t Habit $d 1998-, $x 1455-5697` — terminated serial points forward to its successor. The reverse of `1599669`. Leader byte 7 = `s`, `008` date-type `d` (publication ended). Tests that a *closed* serial Work can still carry a forward `bf:precededBy` / `bf:succeededBy` relationship to a live one. |

## What this set exercises

- **Same Work, different format (FRBR Expression branching).** `1837972`
  exercises the canonical case: one Work, one Expression (Finnish
  translation), two Instances (print + e-book). The 776 chain must be
  resolved into a `bf:hasOtherPhysicalFormat` triple on each Instance,
  not on the Work.
- **Title-change predecessor / successor chains.** `1599669` and
  `1904380` together exercise the canonical serial-title-change shape:
  one record points back (`780`) at an old title; another points
  forward (`785`) at a new title. M3 must emit Work-to-Work
  `bf:precededBy` / `bf:succeededBy` triples, not collapse the titles
  into a single Work.
- **ISBN-only and ISSN-only linkage.** Both `1837972` (ISBN in `$z`)
  and the serial records (ISSN in `$x`) link the sibling record by
  *identifier string*, not by bib-ID. M3 needs an identifier-lookup
  step (against `Instance.identifier`) to resolve the sibling URI.
  Skosmos publication must be tolerant of missing siblings — the
  ebook of `1837972` may not be in the corpus at all.
- **Closed-serial dates.** `1904380`'s `008` date-type `d` and date
  range `19541997` (run from 1954 to 1997) test that a Work can carry
  a `bf:terminus` boundary date.

## What this set does **NOT** exercise

- A `776` record where the e-book sibling is *also* present in the
  fixture set. Both records would need to point at each other; we have
  only one half here. If end-to-end round-trip testing of the 776
  graph is needed, add the e-book record (ISBN 9789511440932) as a
  paired fixture.
- A complex title-change history (multiple consecutive title changes
  forming a chain of 4+ records). `1599669` → predecessor and
  `1904380` → successor are single-hop. A multi-hop chain would
  exercise transitive-closure walking but no fixture record in the
  pick covered this.
- **`780`/`785` between non-serial Works** (e.g. a monograph that
  supersedes another monograph). The 451 chain records in the 30k pick
  were dominated by serials (~96 of them) and same-Work format
  variants (`776`, 355); the mid-chain monograph case appears not to
  occur often enough to surface in the sample.
- `787` (other relationship), `765` (translation of), `767`
  (translated as) — out of scope for this set. The translation-of
  linkage is already handled via `240 $l` + `041 $h` on the records
  themselves.

## Why a subdirectory and not flat alongside the synthetic set?

Same reason as `curated/` and the other sibling subdirectories — the
M2 integration test's non-recursive `glob("*.xml")` on the parent
`sample-marcxml/` directory means files in subdirectories are
invisible to that test. Tests that want these records import this
path explicitly.
