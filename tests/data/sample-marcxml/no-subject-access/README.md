# Thin-metadata records — no subject access

Two records sampled on 2026-06-01 from the 30k-record cataloguer
discovery corpus
(`scratchpad/cataloguer-pick-2026-06-01.{txt,csv,md}`). Both lack
controlled-vocabulary subject access entirely — but in two distinct
ways. The corpus inventory found **4,736 records (15.7 % of the pick)**
in this shape, so it is a production-data reality, not an edge case.

This set pins behaviour for stages that walk subject contributions
(M3's subject extractor, M5's embedding builder, M9's reconciliation
walker): they must not crash, must not silently skip the whole record,
and must emit a Work with no subject contributions cleanly.

## Records

| Bib ID | Title | "No subject access" pattern | Year | RDA tagging |
|--------|-------|------------------------------|------|-------------|
| `2626636` | *To hell & back again* (Varg Vikernes memoirs) | **Has 6XX content but no `$2` vocabulary tagging.** Three subject fields (`655 $a muistelmat`, `650 $a heavy rock`, `650 $a death metal`) carry plain literal values with no `$2` controlled-vocabulary reference and no `$0` authority URI. The subject *text* is there; the subject *vocabulary identity* is not. | 2025 | Full RDA: `336 txt / 337 n / 338 nc`. |
| `2337535` | *Kalevipoeg : pilteepos osa 3* (Kreutzwald) | **Bare record with no 6XX at all.** `100 $a Kreutzwald, Friedrich Reinhold` (no `$e` relator), `245`, single `700 $a Nukki, Ats` (illustrator, no `$e`). No subject fields of any kind. No RDA 336/337/338. | 2017 | No RDA tagging. |

## What this set exercises

- **`bf:subject` contribution path returns empty cleanly.** M3 must
  build a `bf:Work` for these records that carries title, contributor
  (`bf:contribution`), and minimal RDA metadata, but with **zero**
  `bf:subject` triples. No empty-`bf:Topic` blank nodes; no synthesised
  "untagged" subject contributions.
- **Subject-walker tolerates `$2`-less `6XX`.** `2626636` has subject
  *text* — `muistelmat`, `heavy rock`, `death metal` — but no
  vocabulary identifier. M3 should NOT default-assume `local` or
  `slm/fin` for these; the absence of `$2` is itself information
  (cataloguer left vocabulary ungrounded). Recommended product
  decision: emit the subject text into a `bf:subject` with a typed
  literal but no `bf:source`, and log a warning. The fixture exists to
  surface the case so the M3 author can make the call.
- **Pre-RDA records pass through.** `2337535` has no `336/337/338` and
  no `$e` on the main entry. M3 must fall back to leader-byte-derived
  RDA values (leader `a/m` → `txt/n/nc`) and to default `bf:Person`
  with no role assertion. The pre-RDA fallback path is also exercised
  by `curated/1059592`; this record adds a Latin-1-era Estonian-language
  case to that coverage.
- **Embed stage handles subject-less Works.** M5 builds the embedding
  vector from title + subjects + contributors. For these records the
  subject component is empty; the embedding should be built from the
  remaining fields only, not skipped.

## What this set does **NOT** exercise

- A record where the `100` is also missing — i.e. title-only records.
  None were present in the inventory subset I filtered (modern text
  monograph + 100 main entry + zero `$2` vocab). If a fully bare record
  becomes a concern, sample one separately.
- A record where `6XX` is present but with `$2` referring to a vocab
  M3 does not know how to map. That overlaps the `vocab-typos/`
  subdirectory — see `2634250` there for the structurally-broken case.

## Why a subdirectory and not flat alongside the synthetic set?

Same reason as `curated/` and the other sibling subdirectories — the M2
integration test's non-recursive `glob("*.xml")` on the parent
`sample-marcxml/` directory means files in subdirectories are invisible
to that test. Tests that want these records import this path explicitly.
