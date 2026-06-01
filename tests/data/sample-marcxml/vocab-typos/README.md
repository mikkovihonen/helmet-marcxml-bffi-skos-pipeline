# Vocabulary-reference data dirt — real Helmet records

Seven records sampled on 2026-06-01 from the 30k-record cataloguer
discovery corpus
(`scratchpad/cataloguer-pick-2026-06-01.{txt,csv,md}`). All seven carry
**malformed vocabulary references** of one kind or another — typo'd `$2`
codes, structural MARC encoding bugs that swallow subfield delimiters
into a value, etc. Together they pin BFFI-pipeline behaviour on real
production cataloguing dirt the cataloguers will not always be able to
fix at source.

The 30k discovery corpus surfaces these patterns at non-trivial scale:
394 records with `slm/fin/` (trailing slash), 37 with `slm//fin` (double
slash), 6 with `slm/fi`, 5 with `sml/fin`, 4 with `kauno/ifn`, 3 each
with `kaunofin` / `lm/fin`, plus the lone structural delimiter-swallow
in `2634250`.

A separate cataloguer-facing report —
[`scratchpad/cataloguer-vocab-typos-2026-06-01.md`](../../../scratchpad/cataloguer-vocab-typos-2026-06-01.md)
— summarises the survey with bib IDs cataloguers can use to clean the
Sierra source records.

## Records

| Bib ID | Title | Typo pattern in `$2` | Co-existing valid `$2` | Notes |
|--------|-------|----------------------|------------------------|-------|
| `2492140` | *Mõrkjasmagus kirsihooaeg* | `slm/fin/` + `kaunofin` | `kauno/fin`, `slm/fin`, `local`, `yso/fin` | Two distinct typos in one record. The correct `slm/fin` and the typo'd `slm/fin/` co-exist on different `655` fields — the cataloguer caught the right form half the time. `kaunofin` (no slash) is a separate typo on a `650`. |
| `2627743` | *Emma* (Jean Reno, 2025) | `slm/fin/` | `slm/fin` | The single typo'd `655` (`esikoisteokset`) sits next to a correctly-tagged `655` (`romaanit`, `slm/fin`). Same record, same cataloguer, same vocabulary, different `$2` strings. Tightest "is-it-the-same-vocabulary?" assertion in the set. |
| `2378083` | *Ruth Maiers dagbok* | `slm/fi` | `slm/fin` | One letter clipped (`fin` → `fi`). The other `655` on the same record is correctly `slm/fin`. |
| `2493430` | *Kätketty valtakunta* (Sutherland) | `sml/fin` | `slm/fin` | Letters transposed (`slm` → `sml`). Bonus: this record also carries KANTO `$0` on its translator (Meri Kapari, `(FI-ASTERI-N)000129312`) — exercises KANTO-on-700 simultaneously with a vocab typo. |
| `2636949` | *Lonesome dove* (McMurtry, 2025) | `kauno/ifn` | `kauno/fin`, `slm/fin`, `local`, `yso/fin` | Letters transposed in vocabulary suffix. The same typo appears on `2496749` + `2496750` (Eternals films, video records, leader6=`g`) — so it is *not* a one-off typo, it is a stable misspelling that recurs across cataloguing batches. |
| `2413136` | *Ja vas ne slyšu* (Ronina) | `lm/fin` | `local` | Single letter dropped (`slm` → `lm`). Russian-language record with `880` Cyrillic alt-script — exercises typo handling on a non-Latin-target record. |
| `2634250` | *A vision in a dream* (Edward Gregson concertos) | **Structural delimiter-swallow** | `slm/fin` | The `655` field's `$a` value is literally `taidemusiikki‡2slm/fin‡0http://urn.fi/URN:NBN:fi:au:slm:s...` — the cataloguer pasted text-MARC into the `$a` subfield rather than properly subfielding. `‡` (U+2021 DOUBLE DAGGER) is the MARC subfield delimiter. Distinct from the other six: this is a **structural** MARC bug, not a vocabulary-string typo. M2 must tolerate it without crashing; M3 should log and skip rather than try to recover the swallowed `$2` / `$0`. |

## What this set exercises

- **`$2` vocabulary canonicalisation.** M3's vocabulary mapper must
  recognise that `slm/fin`, `slm/fin/`, `slm//fin`, `slm/fi`, `sml/fin`,
  `lm/fin` all denote the same SLM vocabulary, and either canonicalise
  silently or emit a structured warning. Recommendation:
  *canonicalise + warn* for stripped-form-of-known-vocab, *fail* for
  unrecognisable strings.
- **Multi-form-in-one-record.** `2492140`, `2627743`, `2378083`,
  `2493430` each carry both a typo'd `$2` and a correctly-tagged `$2`
  on different `6XX` fields of the same record. M3 must converge them
  onto the same `bf:Topic`, not emit two separate topics.
- **Cross-record typo recurrence.** `kauno/ifn` (`2636949`) appears
  identically on at least two other 2022 Marvel-film records
  (`2496749`, `2496750`). Whatever mapping table M3 uses should be
  built once and cached; one record's typo recovery should benefit
  the others.
- **Structural malformation.** `2634250`'s `‡`-in-`$a` is a structurally
  malformed `<subfield>` element. M2's XML parser parses it as a single
  long `$a` value (because that is what the XML says). M3 must
  recognise the `‡<code>` pattern and either skip the subject or
  reconstruct the intended `$2`/`$0`. We do not pin behaviour here —
  the fixture exists to *surface* the case; product choice (skip vs.
  reconstruct) is left to the M3 author.
- **Cataloguing dirt at non-trivial scale.** Combined, the seven typo
  patterns named in this set affect ~450 records in the 30k discovery
  corpus (`slm/fin/` alone is 394). Treating these as edge cases would
  silently drop ~1.5 % of vocabulary references on real production
  data.

## What this set does **NOT** exercise

- A vocabulary `$2` that is *unknown* to any known vocabulary
  (genuinely unrecognisable, e.g. `foo/bar`). All seven typos in this
  set are close-misspellings of `slm/fin` or `kauno/fin`. A "genuinely
  unknown vocab" case would require synthesis, not a real record.
- A vocabulary `$0` URI that is malformed (e.g. truncated URL, wrong
  prefix). The `2634250` delimiter-swallow is the closest analogue, but
  the surrounding fields are not malformed at the URI level.
- Vocabulary mismatches between sibling records of the same Work
  (where one Expression tags subjects in `yso/fin` and another in
  `kaunokki`). That is a *categorisation*-quality concern, not a
  *typo*; surface as a separate fixture if it becomes a real
  pipeline concern.

## Why a subdirectory and not flat alongside the synthetic set?

Same reason as `curated/` and the other sibling subdirectories — the M2
integration test's non-recursive `glob("*.xml")` on the parent
`sample-marcxml/` directory means files in subdirectories are invisible
to that test. Tests that want these records import this path
explicitly.
