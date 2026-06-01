# Multilingual originals — `041` multi-`$a` with NO `$h`

Three records sampled on 2026-06-01 from the 30k-record cataloguer
discovery corpus
(`scratchpad/cataloguer-pick-2026-06-01.{txt,csv,md}`). All three carry
`041` with **multiple `$a` codes and no `$h`** — documents that are
*originally* multilingual, not translations of an earlier
single-language Work.

The 30k discovery corpus contained **1,503 such records (5.0 %)**. The
existing `curated/` and `role-divergence/` sets have zero coverage for
this pattern — `2495433` in `role-divergence/` has `041 $a fin $a swe
$h fin`, which is a *translation* of a bilingual original, not the
bilingual original itself.

## Why this matters

Bilingual / multilingual originals are common in Helmet's catalogue:

- 193 records with `041 $a swe $a fin` (Finland-Swedish bilingual
  originals)
- 123 with `eng+zxx` (English text + non-linguistic content like
  illustrations or maps)
- 87 with `eng+swe+fin` (trilingual)
- 53 + 49 = 102 with `eng+fin` or `fin+eng` (bilingual academic)
- 33 with `chi+eng` (Chinese-English bilingual learners' material)

These are **single Works** with multiple co-equal languages, not
translations. The BFFI Expression graph for a bilingual original
should NOT carry a `bf:translationOf` triple; instead the Work's
language should be modelled as a *set* of languages, and each
Expression carries the single language it expresses (or possibly the
multilingual nature is expressed at Instance / carrier level if both
languages share one bound volume).

## Records

| Bib ID | Title | `041` shape | Pattern |
|--------|-------|-------------|---------|
| `1824159` | *Merkintätaulukot = Besticktabeller ; Vuorovesitaulukot = Tidvattentabeller* (2006) | `041 0 $a fin $a swe` | **fi+swe bilingual technical tables**, no main entry, parallel `=` title shape in `245 $a / $b`. Both `allars` (Swedish subject-vocab) and `yso/fin` / `ysa` co-used on the same record — parallel subject access in both languages. Cleanest "no-main-entry bilingual technical resource" case. |
| `1834311` | *Anarâškielâ ravvuuh* (Inari Sámi grammar by Matti Morottaja, 2007) | `041 0 $a smi $a fin` | **Sámi+Finnish bilingual scholarly work**, `100 1 $a Morottaja, Matti` (single author, no `$e` — pre-RDA), `008` primary-language code `smi`. Inari Sámi (`smi` is the umbrella code; the specific variant is `smn` per ISO 639-3) co-exists with Finnish in this Kotimaisten kielten tutkimuskeskus / KKTK publication. Tests handling of: (a) `smi` umbrella vs specific Sámi-language codes, (b) bilingual scholarly authorship in Finland's minority-language publishing tradition. |
| `1834588` | *Hiekkaympyrä (keskeneräinen) = Sand circle (unfinished)* (Hannele Rantala, 2007) | `041 0 $a eng $a fin` | **English+Finnish bilingual art book**, with the twist that `100 1 $a Rantala, Hannele, $e kuvittaja` — author is the photographer/illustrator, not the writer. `700 1 $a Garner, Michael, $e kääntäjä` provides the English translation of accompanying Finnish text — so this is a bilingual-original where the secondary language was added by a translator, blurring the "original" boundary. Useful as an "ambiguous original vs translation" case. |

## What this set exercises

- **`041` multi-`$a` parsing.** M3's language extractor must enumerate
  all `$a` codes, not stop after the first. The Expression's
  `bf:language` should be a set, not a single value.
- **No `bf:translationOf`.** Despite multiple languages, no `041 $h`
  means no source Work in another language. The Work graph is
  self-contained.
- **Parallel-title shape (`245 $a = $b`).** `1824159` and `1834588` both
  use the `=` separator pattern within `245`: `$a Finnish-title = $b
  Swedish-title /` or `$a English = $b Finnish`. M3's title walker
  must recognise this as two parallel titles, not a title-and-subtitle.
- **Parallel subject access.** `1824159` carries both `allars` (Swedish
  vocab) and `yso/fin` / `ysa` (Finnish vocab) `650`s — the *same
  subject* expressed in both vocabulary systems. M3 should NOT collapse
  these into a single subject contribution but should preserve the
  vocabulary-language pairing for Skosmos facet display.
- **Sámi-language handling.** `1834311` is the only `smi`-coded record
  in any current fixture. The `smi` umbrella code (Sámi languages,
  collective) is distinct from the specific Sámi-language codes
  (`smn` Inari, `sme` Northern, `sms` Skolt, etc.). Cataloguer used
  the umbrella code; M3 should preserve that distinction or
  canonicalise it explicitly.
- **Ambiguous original vs translation.** `1834588` has a translator
  credit (`700 $e kääntäjä`) but no `041 $h` and no `240` uniform
  title — the cataloguer modelled it as a single bilingual Work, not
  as a Work + translated-derivation. M3 should follow that cataloguer
  judgement and *not* synthesise a derivation edge from the
  translator presence alone.

## What this set does **NOT** exercise

- **`041` multi-`$a` with `$h`** (a translation of a multilingual
  original). Already covered by `role-divergence/2495433`.
- **3+ language originals** (`eng+swe+fin` is the dominant trilingual
  pattern at 87 records, but a representative was not picked here). If
  trilingual Work-modelling becomes a concern, a fourth record should
  be added.
- **Bilingual sound recordings** (audio with two co-equal language
  tracks). The 30k corpus had several but they were in `i`/`j`
  leader6 classes and harder to disambiguate from translation chains
  via inventory alone.
- **Codeswitching / mixed-language single-Expression cases** — where a
  single Expression mixes languages within its text. Not addressed by
  `041` at all; would require text-content analysis.

## Why a subdirectory and not flat alongside the synthetic set?

Same reason as `curated/` and the other sibling subdirectories — the
M2 integration test's non-recursive `glob("*.xml")` on the parent
`sample-marcxml/` directory means files in subdirectories are
invisible to that test. Tests that want these records import this
path explicitly.
