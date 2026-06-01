# Non-Latin-source translations — real Helmet records

Five records sampled on 2026-06-01 from the 30k-record cataloguer
discovery corpus
(`scratchpad/cataloguer-pick-2026-06-01.{txt,csv,md}`). All five exercise
the same boundary: **`880` alternate-script linkage where the source
language uses a non-Latin script**.

The `curated/` set already covers Cyrillic via `2602288` (German →
Russian; Cyrillic target). This subdirectory complements that with
**non-Latin *source* languages** — Japanese, Chinese, Korean, Arabic,
Hebrew — each carrying full `880` round-trip linkage between the
romanised cataloguing form and the original-script form.

## Helmet-specific note

All five records are Russian-language editions catalogued for Helmet's
Russian-speaking patrons. That is why the `041 $a` is `rus`, not `fin` —
the cataloguer pick of jpn→fin manga records in the same 30k corpus does
not generally preserve the Japanese script via `880`. If a `fin`-target
non-Latin-source case becomes available later (e.g. a Helmet record
of a Japanese novel where the Japanese title is preserved), it should
slot in here as a sixth record.

## Records

| Bib ID | Title (245 romanised) | 041 chain | `880` count | Notes |
|--------|------------------------|-----------|-------------|-------|
| `2598278` | *Povest o dome Taira* (Heike Monogatari) | `rus ← jpn` | 4 | Japanese classical literature. `240 $a Heike monogatari. $l Venäjä` carries the romanised uniform title; `880-01` mirrors `245` with Cyrillic title and translator credit. Two `700` translators each `$6`-linked to their own `880`. |
| `2327520` | *Zadatša trjoh tel* (Three-Body Problem, Liu Cixin) | `rus ← chi` | 3 | `100 $6 880-01` + `880-01 (N: Цысин, Лю` — `100` linked to its Cyrillic `880`. `240 $a San ti, $l venäjä` uniform title. Note: the `245 $c` says "perevod s anglijskogo" — *translated from English*, so this is **chi → eng → rus**, an intermediate-translation chain, even though `041 $h` records only `chi`. M3 should flag the cataloguer-supplied translator credit as evidence of an intermediate Expression. |
| `2532596` | *Reka, gde voshodit luna* (Princess Pyeongang) | `rus ← kor` | 7 | Korean fairy tale / juvenile. `100 $6 880-01` + `100`-link to `880-01 (N: Чхве Сагю`. Rich `880` set (7 fields) covering 100/245/264/490/700 — fullest `880` linkage in the set. |
| `2418440` | *Tysjatša i odna notš* (1001 Nights, Anna Milbourne retelling) | **`rus ← eng ← ara`** | 10 | **Triple-translation chain.** `041 $a rus $k eng $h ara` — `$k` is intermediate-language Arabic→English, then `$h` is original Arabic. Modeller-test for the chain `bf:translationOf` graph: the Russian Expression's `bf:translationOf` lands on an *English* Expression, which itself has `bf:translationOf` on an Arabic source Work. Two illustrator `700` entries (taiteilija), each `$6`-linked to its own `880`. Highest `880` count in the set. |
| `2455222` | *Esav* (Meir Shalev) | `rus ← heb` | 6 | Hebrew **right-to-left** source script. `880-01 (N: Шалев, Меир` + `880-02 (N: Эсав :` for the title. Two Russian-Israeli translators with `$6`-linked `880` entries. The only RTL-script case in the curated fixture pool. |

## What this set exercises

- **`880`/`$6` round-trip.** For each `100`/`245`/`264`/`490`/`700` field
  with a `$6 880-NN` reference, the corresponding `880 $6 NNN-NN/(N`
  field must be located and merged into the BFFI model. The romanised
  form should remain the cataloguer-preferred `bf:label`; the
  alternate-script form becomes a secondary label with
  `bf:script` annotation.
- **Non-Latin script-tagging.** Each `880`'s `$6` second component
  encodes the script tag (`(N` = Cyrillic in MARC's `880` convention).
  M3 should preserve the script tag on the alternate-script literal.
- **RTL handling (Hebrew).** `2455222` is the only RTL-source case;
  any downstream BIDI rendering should treat the Hebrew literals as
  RTL while keeping the Russian/Cyrillic literals as LTR.
- **Multiple `880` per record.** All five carry 3+ `880` fields; the
  highest is `2418440` at 10. M3's `880` walker must handle the
  N-to-N linkage cleanly, not assume one `880` per record.
- **Intermediate-language translation chain.** `2418440`'s `041 $a rus
  $k eng $h ara` and `2327520`'s "translated from English" cataloguer
  note are both signals of a three-language Expression chain that the
  BFFI graph should model with two `bf:translationOf` edges, not one.

## What this set does **NOT** exercise

- A `fin`-target record translated from a non-Latin source with `880`
  preserved. Such records exist in the 30k corpus only with the
  alternate script *dropped*; cataloguers preserve `880` mainly for the
  Russian-language patron community in Helsinki.
- Devanagari, Thai, Burmese, Ethiopic, Georgian, Armenian, Syriac
  source scripts. The cataloguer pick contained none with `880`
  preservation.
- Vertical-text Japanese (`tate-gaki`) layout concerns — `880` records
  the script but does not encode layout direction.

## Why a subdirectory and not flat alongside the synthetic set?

Same reason as `curated/` and `role-divergence/` — the M2 integration
test's non-recursive `glob("*.xml")` on the parent `sample-marcxml/`
directory means files in subdirectories are invisible to that test.
Tests that want these records import this path explicitly.
