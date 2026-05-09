# Curated MARCXML — real Helmet records

These are **real** Helmet bibliographic records hand-picked by Helmet
cataloguers in response to **Ask 1** in `docs/external-dependencies.md`
(received 2026-05-09). Filenames are the Helmet bib IDs as supplied by
cataloguers; contents are unmodified MARCXML from
`helmet-sierra-data-tools/output/marcxml/`.

## Why a subdirectory and not flat alongside the synthetic set?

The M2 integration test (`tests/integration/test_marc_to_bf.py`) calls
`run(FIXTURES, ...)` on the parent `sample-marcxml/` directory, which
uses a non-recursive `glob("*.xml")`. Dropping these files flat would
flip `summary.succeeded` past the synthetic-only `VALID_IDS` set and
break that test. Keeping them under `curated/` isolates them — M5
gold-set work and any test that wants real records imports from this
path explicitly.

## Slot mapping (Ask 1 cases → records)

| Slot | Case from Ask 1 | Bib ID | Notes on what the record exercises |
|------|-----------------|--------|------------------------------------|
| 1 | Simple Finnish-language original monograph, single creator | `2628274` | Liisa Louhela, *Mies joka kantoi aurinkoa sylissään* (Otava 2026). KANTO `$0` on author, full RDA 336/337/338, kauno/fin + slm/fin + yso subjects. |
| 2 | Same Finnish original ↔ Swedish translation (different record) | — | **UNFILLED.** Need to request a paired bib ID. |
| 3 | Russian-original translated into Finnish (transliteration) | `2371438` | Pushkin, *Aatelisrosvo Dubrovskij + Laukaus ym. kertomuksia* — `041 1` `a:fin h:rus`, transliterated author (Puškin) and four transliterated work titles in `700 $i "Sisältää (teos):"`, two translators. |
| 4 | English-original translated into Finnish | `2372028` | Kate Morton, *Kellontekijän tytär* — `041 1` `a:fin h:eng`, `240 $l suomi`, two co-translators (Pekkanen). |
| 5 | Common-title collision pair (same author, different works) | — | **UNFILLED.** Need a paired bib ID. |
| 6 | Adaptation pair (novel → screenplay/film/graphic novel) | `2564382` | Hungarian film *Natural light* (Nagy Dénes, 2021). Adaptation modelled via `700 $i "Elokuvaversion perustana (teos):"` linking to Závada Pál's novel *Természetes fény*. Source-work bib not in this sample. |
| 7 | Abridgement of a longer work | `2360958` | *Sagor från Mumindalen* — children's book "after 3 stories by Tove Jansson", with `700 $i "Verk baserat på:"` to two source Jansson novels and `700 $i "Innehåller (verk):"` for the constituent stories. |
| 8 | Music recording (sound) | `2452306` | Steven Wilson, *Get all you deserve* — Leader byte 6 = `j`, 2 CDs + Blu-ray, performer in `100`, many `730` track-level analytical entries. |
| 9 | Sheet music / score | `2616222` | Mozart, *Meisterwerke am Klavier* (Edition Peters) — Leader byte 6 = `c`, `041 a:zxx`, single piano score, MUSO-style uniform titles in `730` (Sonaatit/Menuetit/Muunnelmat with KV numbers). |
| 10 | Cartographic resource | — | **UNFILLED.** Need a map record. |
| 11 | Serial / continuing resource | — | **UNFILLED.** Need a serial record (Leader byte 7 = `s`). |
| 12 | Corporate body as creator | `2484550` | Big Country, *Out beyond the river* — `110 2 Big Country, esittäjä` (corporate body main entry), 5-CD anthology, `710 2 2 ... $t` linking to two child works. |
| 13 | Multiple co-creators of equal billing | `1059592` *(secondary)* | *Leivän tähden* — three editors of equal billing in `700` (no `100`), legacy `ysa` subjects (predates yso URIs). Filled here as a secondary tag because the cataloguer-supplied list does not include a clean three-author monograph; the primary slot for `1059592` is 14. |
| 14 | Aggregate work / collection | `2620193` | Dickens, *Kävelyretkiä Lontoon kaduilla* — `240 1 0 Novellit. Valikoima. Suomi`, eight `700 $i "Sisältää (ekspressio):"` links to component expressions, all carrying KANTO `$0`. Cleanest aggregate test in the set. |
| 15 | Deliberately problematic / cataloguing oddity | `1769634`, `2602288`, `2576727` | Three records that stress the validation boundaries differently: `1769634` is a **trilingual original** (FI/EN/RU) with parallel titles in `245` and seven `740` variant titles; `2602288` is a **German→Russian translation** with full `880` alternate-script (Cyrillic) fields paired to `100/245/246/264/490/700`; `2576727` is a **PS5 video game** with `336 cop` + `336 tdi`, 8-language `041`, no main entry, and corporate developer in `710`. |

## Bonus capabilities exercised beyond the Ask 1 slot list

These are useful for the spec/tests even though they were not explicit asks:

- **Film/video material** (Leader byte 6 = `g`, `336 tdi` + `337 v` + `338 vd`): `2564382`.
- **Computer file / interactive multimedia** (Leader byte 6 = `m`, `336 cop` + `336 tdi`): `2576727`.
- **`880` alternate-script linkage**: `2602288` (Cyrillic).
- **KANTO `$0` on names**: `2628274` (author), `2372028` (publisher), `2620193` (author + translator + publisher), `1059592` (none — pre-RDA legacy), `2628274`/`2484550` partial.
- **Aggregate-work modelling via `700 $i "Sisältää (teos|ekspressio):"`**: `2620193`, `2371438`, `2360958`.
- **Adaptation/derivation via `700 $i "perustuu / baserat på / Elokuvaversion perustana":`**: `2564382`, `2360958`.
- **Legacy non-yso vocabularies (`ysa`, `kaunokki`, `bella`, `local`)** — useful for testing the YSO migration path: `1059592`, `2452306`, `2484550`, `2360958`, `2620193`, `2628274`.

## Unfilled slots (follow-up requests for cataloguers)

The 13 records cover 11 of the 15 slots. Outstanding: **2** (Finnish↔Swedish
translation pair), **5** (common-title collision pair), **10**
(cartographic), and **11** (serial). Slots 2 and 5 each require **two**
related bib IDs to be useful. These should be added to a follow-up
request to Helmet cataloguers before M5 gold-set construction.

## Per-record case-note request (open)

The cataloguers supplied bib IDs but not the per-record case notes that
Ask 1 requested ("a one-sentence note on why it's interesting"). The
slot mapping above is **inferred from the MARCXML content**, not
authoritative cataloguer judgement. Confirming the mapping with
cataloguers before freezing the gold set is recommended.
