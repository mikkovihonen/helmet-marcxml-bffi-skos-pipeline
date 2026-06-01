# Curated MARCXML — real Helmet records

These are **real** Helmet bibliographic records hand-picked by Helmet
cataloguers in response to **Ask 1** in `docs/external-dependencies.md`.
Initial batch received 2026-05-09 (13 records covering 11 slots); second
batch received 2026-06-01 closed the unfilled slots 2/10/11 with four
more records (1354066, 1353996, 2394080, 1109760). Filenames are the
Helmet bib IDs as supplied by cataloguers; contents are unmodified
MARCXML from `helmet-sierra-data-tools/output/marcxml/`.

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
| 2 | Same Finnish original ↔ Swedish translation (different record) | `1354066` + `1353996` | Mauri & Tarja Kunnas, *Koirien Kalevala* (fi, 1992) ↔ *Hundarnas Kalevala* (swe, 1994). 1353996 carries `240 10 $a Koirien Kalevala, $l svenska` pointing at the Finnish original; `041 1 $a swe $h fin`; Lars Huldén in `700 $e översättare`. Both records have `100 $a Kunnas, Mauri` with no KANTO `$0` (pre-RDA in the 1994 record; the 1992 record adds `$e taiteilija, $e kirjoittaja` but still no `$0` — useful for exercising the no-KANTO branch). |
| 3 | Russian-original translated into Finnish (transliteration) | `2371438` | Pushkin, *Aatelisrosvo Dubrovskij + Laukaus ym. kertomuksia* — `041 1` `a:fin h:rus`, transliterated author (Puškin) and four transliterated work titles in `700 $i "Sisältää (teos):"`, two translators. |
| 4 | English-original translated into Finnish | `2372028` | Kate Morton, *Kellontekijän tytär* — `041 1` `a:fin h:eng`, `240 $l suomi`, two co-translators (Pekkanen). |
| 5 | Common-title collision pair (same author, different works) | — | **UNFILLED.** Need a paired bib ID. |
| 6 | Adaptation pair (novel → screenplay/film/graphic novel) | `2564382` | Hungarian film *Natural light* (Nagy Dénes, 2021). Adaptation modelled via `700 $i "Elokuvaversion perustana (teos):"` linking to Závada Pál's novel *Természetes fény*. Source-work bib not in this sample. |
| 7 | Abridgement of a longer work | `2360958` | *Sagor från Mumindalen* — children's book "after 3 stories by Tove Jansson", with `700 $i "Verk baserat på:"` to two source Jansson novels and `700 $i "Innehåller (verk):"` for the constituent stories. |
| 8 | Music recording (sound) | `2452306` | Steven Wilson, *Get all you deserve* — Leader byte 6 = `j`, 2 CDs + Blu-ray, performer in `100`, many `730` track-level analytical entries. |
| 9 | Sheet music / score | `2616222` | Mozart, *Meisterwerke am Klavier* (Edition Peters) — Leader byte 6 = `c`, `041 a:zxx`, single piano score, MUSO-style uniform titles in `730` (Sonaatit/Menuetit/Muunnelmat with KV numbers). |
| 10 | Cartographic resource | `2394080` | *Abisko - Kebnekaise: Kungsleden karta & guide* (Calazo, 2019). Leader byte 6 = `e`, RDA `336 cri / 337 n / 338 nb`, parallel `041 0 $a swe $a eng`, no main entry, `710 2 $a Calazo förlag` publisher, two `740` variant titles. Subjects mix `yso/fin`, `yso/swe`, `allars`, `ysa`, `slm/fin`, `slm/swe` — the only record in the set exercising the bilingual fi/swe subject-vocabulary cluster simultaneously. |
| 11 | Serial / continuing resource | `1109760` | *Tekniikan maailma* (Vantaa, 1974–). Leader byte 7 = `s`, `008` date-type `c` with `19749999fi` (ongoing), no main entry, `710 2 $a Otavamedia` publisher, seven `650 7 yso/fin` subject headings. Only ongoing-serial record in the set; no `776`/`780`/`785` continuation chain (single-record serial, not a title-change cluster). |
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
- **Legacy non-yso vocabularies (`ysa`, `kaunokki`, `bella`, `local`, `allars`)** — useful for testing the YSO migration path: `1059592`, `2452306`, `2484550`, `2360958`, `2620193`, `2628274`, `2394080`, `1844820`.
- **Multi-relator on a single `100 $e`**: `1354066` (`$e taiteilija, $e kirjoittaja`). The Slot 7 `2360958` does this too; `1354066` adds an artist-first author-second variant. Both records are also no-KANTO-`$0` on the main entry — useful for the unreconciled-agent path.
- **Pre-RDA legacy with no relator at all on `100`**: `1353996` (`100 1 $a Kunnas, Mauri` without `$e`). Tests the relator-inference / default-to-`author` fallback when the source MARC predates the RDA `$e` convention.
- **Pre-2000 records spanning multiple decades**: `1844820` (1955), `1850100` (1982). Counter-balance to the otherwise modern (2000s+) majority of the set; the cataloguers explicitly asked for "different years" (`eri vuosilta`) coverage. Both records exercise pre-RDA / partial-RDA cataloguing conventions.
- **Non-Finnish translation chain (Italian → German)**: `1850100` Umberto Eco *Der Name der Rose* (1982). The other translation slots (3 / 4) are foreign-language → Finnish; `1850100` is foreign → foreign with neither end in Finland's languages. Useful for testing that BFFI's translation-graph code does not implicitly assume a Finnish endpoint.
- **Bare `700 $i ind2=2 $a $t` analytical entries (no `$i "Sisältää teos:"` wrapper)**: `1844820` (1955 anniversary publication with four constituent-work `700`s; each is `ind1=1 ind2=2` with bare `$a + $t`, no `$i`). Complements the wrapper-form analytical entries in `2620193` / `2371438` / `2360958` with the older bare-`$t` cataloguing convention used by ~366 records in the discovery corpus.
- **Pathological contributor count (perf stress)**: `2339093` *Mozart 225 : Theatre* (2016 box set, audio recording, `100 $a Mozart, Wolfgang Amadeus`, `f700_count = 128`). Largest contributor count in the discovery corpus's 30k pick. Useful as a perf-smoke fixture for the M3 contributor walker — confirms no O(n²) regression at the high end of real-world cataloguing.
- **3D artefact / board game (leader byte 6 = `r`)**: `2088800` *Race for the galaxy* (2007 designer-board-game, `100 $a Lehmann, Tom, $e designer`, RDA `336 tdf` = "three-dimensional form"). Adds the leader6=`r` resource type to the existing leader6 coverage in the set (`a`/`c`/`e`/`g`/`j`/`m`/`s`).
- **Estonian-target translation (`041 $a est $h fin`)**: `2605258` *Imepoiss Leon* (2024 children's book, Estonian translation of a Finnish original by Mervi Heikkilä). Estonian is uncovered by the rest of the set; this record adds an Estonian-target case with KANTO `$0` on the main entry and one Estonian translator.

## Unfilled slots (follow-up requests for cataloguers)

A second batch from cataloguers on 2026-06-01 closed three of the four
previously outstanding slots: **2** (Kunnas, fi↔swe translation pair),
**10** (Kungsleden cartographic), and **11** (*Tekniikan maailma*
serial). The set now covers 14 of the 15 slots; outstanding is **5**
(common-title collision pair).

Slot 5 has a plausible candidate from the same 2026-06-01 batch —
`1497063` + `2262433`, both *Runot* by Kaarlo Sarkia, the 2016 record an
aggregate of four constituent works via `700 $i "Sisältää teos:"`
(*Kahlittu*, *Velka elämälle*, *Unen kaivo*, *Kohtalon vaaka*). This is
tighter than the original Slot 5 framing because the *aggregate vs
selection* axis sits on top of the title-collision axis; confirm with
cataloguers whether that is the collision they had in mind before
committing those records.

## Per-record case-note request (open)

The cataloguers supplied bib IDs but not the per-record case notes that
Ask 1 requested ("a one-sentence note on why it's interesting"). The
slot mapping above is **inferred from the MARCXML content**, not
authoritative cataloguer judgement. Confirming the mapping with
cataloguers before freezing the gold set is recommended.
