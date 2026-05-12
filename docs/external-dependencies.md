# External dependencies — records to request from Helmet cataloguers

The pipeline can be built autonomously through M0–M4 using synthetic MARCXML, but **M5 onwards requires real records** because the dedup quality, embedding benchmark, and gold-set development all depend on realistic field content. Surface these asks at the milestones below — don't proceed past the gates listed on synthetic data alone.

A Finnish-language snapshot of the cataloguer-facing requests is available in `docs/archived/cataloguer-asks-fi.md` for forwarding directly to Helmet staff. The English version here is the live source — regenerate the Finnish copy from this document if the asks change.

## Ask 1 — Curated development sample (~15 records, before M5)

Hand-picked records exercising the cases the pipeline must handle. For each: a Helmet bib ID, a one-sentence note explaining what it demonstrates, and an **expected outcome in plain language** (e.g. "should merge with record X as the same Work" / "should remain distinct from record Y despite identical title"). The expected-outcome annotations seed the gold set (M12). Stored under `tests/data/sample-marcxml/` once received.

The list to request:

**Work/Expression cases** (drive M3 + M6):

1. A simple Finnish-language original monograph, single creator.
2. The same Finnish original as a Swedish translation — different Helmet record, same RDA Work.
3. A Russian-original work translated into Finnish (transliteration + translation).
4. An English-original work translated into Finnish (different transliteration profile from Russian).
5. **A common-title collision pair** — two records by the same Finnish author with the same generic title but different works (e.g. an early "Runot" vs a posthumous "Runot" selected works).
6. A novel-to-screenplay or novel-to-graphic-novel adaptation pair (same source, different content type).
7. An abridgment of a longer work (the hardest "different work" case).

**Material-type coverage** (drives BFFI subclass routing):

8. A music recording (sound).
9. Sheet music or a score of a different musical work (notated music).
10. A cartographic resource (map).
11. A serial / continuing resource (optional but useful if production corpus contains serials).

**Edge cases**:

12. A record with a corporate body as creator.
13. A record with multiple co-creators of equal billing.
14. A record for an aggregate work / collection.
15. A "deliberately problematic" record — one the cataloguer finds embarrassing for any reason (cataloguing oddity, missing fields, encoding quirks). Stress-tests the validation boundaries.

### Status (2026-05-09): partially fulfilled

Cataloguers returned **13 bib IDs** — fixtures committed under
`tests/data/sample-marcxml/curated/` (see `curated/README.md` for the
full slot mapping and per-record notes). The mapping below is **inferred
from MARCXML content**, since per-record case notes were not supplied
alongside the IDs; confirming with cataloguers before the gold set
freezes is recommended.

| Slot | Bib ID | Inferred fit |
|------|--------|--------------|
| 1 — Finnish original monograph | `2628274` | Liisa Louhela, *Mies joka kantoi aurinkoa sylissään* |
| 2 — FI ↔ SV translation pair | — | **Outstanding** (needs two paired bibs) |
| 3 — Russian → Finnish | `2371438` | Pushkin selection, two translators |
| 4 — English → Finnish | `2372028` | Kate Morton, *Kellontekijän tytär* |
| 5 — Common-title collision pair | — | **Outstanding** (needs two paired bibs) |
| 6 — Adaptation pair | `2564382` | *Natural light* (film) ← Závada Pál novel |
| 7 — Abridgement | `2360958` | *Sagor från Mumindalen* ← Tove Jansson novels |
| 8 — Music recording (sound) | `2452306` | Steven Wilson, *Get all you deserve* |
| 9 — Notated music / score | `2616222` | Mozart piano works, Edition Peters |
| 10 — Cartographic resource | — | **Outstanding** |
| 11 — Serial / continuing | — | **Outstanding** |
| 12 — Corporate body creator | `2484550` | Big Country, *Out beyond the river* (`110 2`) |
| 13 — Multiple co-creators | `1059592` *(secondary)* | Three editors of equal billing |
| 14 — Aggregate / collection | `2620193` | Dickens essay collection in Finnish |
| 15 — Deliberately problematic | `1769634`, `2602288`, `2576727` | Trilingual original; Cyrillic `880`; PS5 game |

**Follow-up to request from cataloguers:**

1. **Slots 2, 5, 10, 11** — four unfilled slots. Slots 2 and 5 each need
   a *paired* bib ID; the gold-set value comes from the relationship
   (same Work / colliding title) and is impossible to construct from a
   single record.
2. **Per-record one-sentence case notes** — the original ask included
   "a one-sentence note on why it's interesting" per record. These were
   not supplied. Without them the slot mapping in
   `tests/data/sample-marcxml/curated/README.md` is the maintainer's
   inference, not authoritative cataloguer judgement.
3. **Expected outcomes for adjacency cases** — for `2564382`/`2360958`
   we need the source-novel bib IDs to be confirmed as separate Works
   in the gold set (and added to the corpus if not already present).

## Ask 2 — Reconciliation seed batch (~15 records, before M9)

Records chosen to develop and test agent and subject reconciliation:

- **5–10 records** with creators known to be in KANTO under their authorised forms (happy path).
- **3–5 records** with creators not in KANTO — typically non-Finnish authors of works held in Helmet (VIAF fallback path).
- **3–5 records** where the MARC heading differs from the KANTO authorised form: variant transliteration, different birth/death dates, alternate spelling. These are the cases where the LLM picker adds value over lexical matching alone.
- **A few records** carrying YSO subject headings in 650 fields, ideally a mix of `$2 yso/fin` and `$2 yso/swe` forms.

## Ask 3 — Corpus characterisation (before M5 production run)

Summary statistics about the production corpus. Required before kicking off any production-scale operation:

- Total record count (we assumed 800k; confirm).
- Distribution of material types (monograph / music recording / AV / map / serial / etc.).
- Distribution of languages.
- Estimated fraction of translations vs originals.
- Single dump or incremental updates? (Affects whether M2's idempotency requirements are phase-1 or phase-2.)
- Any records flagged for exclusion (provisional, pending-deletion, cataloguing-error).
- Any records under embargo or with policy restrictions on linked-data republication.

## Ask 4 — Policy confirmation (before M10 production publish)

Before publishing to a public-facing Skosmos instance, explicit confirmation from the Helmet consortium that:

- The bibliographic metadata is OK to republish as linked open data.
- The chosen URN namespace `http://urn.fi/URN:NBN:fi:bib:work:` is acceptable / coordinated with NLF.
- No specific records or record categories must be excluded from the production publish.
- The license for the published RDF data is settled (CC0 to match Finto conventions; confirm).

## Notice — RDA 336/337/338 synthesis at Sierra export time

Pre-RDA records (broadly: anything catalogued before Helmet adopted
RDA around 2015-16) often lack the `336/337/338` content / media /
carrier triple that M2's `marcxml-content-minimum` gate requires.
~11 % of the 2026-05-12 5 000-record production-style run dropped on
exactly this. To recover those records, the pipeline's Sierra-export
stage **synthesises** the missing 33X datafields from the MARC
signals already on the record:

1. MARC `007` (physical-form fixed-field), when present.
2. The `(leader/06, 008-form-of-item)` pair — universal default for
   the manifestation. Covers ~100 % of the corpus.
3. The bib-level Sierra `material_code`.
4. The item-level Sierra `itype_code_num`.
5. MARC `300$a` extent regex — last resort.

The cascade fires **only when the bib carries none of `336/337/338`**.
Cataloguer-coded 33X always wins — adding an explicit 33X datafield
in Sierra is the per-record opt-out.

### Synth-provenance marker

Every synthesised 33X datafield is tagged with a MARC `$5` subfield
carrying the institution code and synth version:

```xml
<datafield tag="336" ind1=" " ind2=" ">
  <subfield code="a">teksti</subfield>
  <subfield code="b">txt</subfield>
  <subfield code="2">rdacontent</subfield>
  <subfield code="5">FI-HELME/synth-v1</subfield>
</datafield>
```

(MARC `$5` = "institution to which field applies".) Cataloguer-coded
33X carries no such marker. The marker lets downstream tooling find
synth-coded fields and replace them deterministically when the
cascade version bumps, without disturbing cataloguer edits.

The version integer lives at `SYNTH_VERSION` in
`src/marcxml_export_pipeline/sierra/rda_signals.py` and is bumped
manually whenever the cascade's emitted tuple for an existing record
would change (new layer ordering, table updates, etc.). Bumping the
version is a signal to re-cascade existing synth-coded records on
the next full Sierra export.

The cascade engine itself lives at
`src/marcxml_export_pipeline/sierra/rda_signals.py`; the table
contents (`LEADER_008_TO_RDA`, `LEADER_06_FALLBACK`,
`MATERIAL_TO_RDA`, `ITYPE_TO_RDA`, and the 300$a token list) are
the load-bearing tunable surface.

## How to surface these asks

These are dependencies on a human cataloguer, not technical decisions Claude Code can make:

- **Before starting M5:** stop and remind the user that Asks 1 and 3 are needed before proceeding past synthetic data. If real records aren't available, M5 can be developed against synthetic input but cannot be declared done.
- **Before starting M9:** remind about Ask 2.
- **Before the M10 production publish (not the sample publish):** remind about Ask 4.

If asks are not yet fulfilled, proceed with synthetic data where possible and explicitly note in the PR description and runbook which milestone is currently "blocked on external input." The user decides whether to wait or work around.

## Phrasing for the cataloguer

When the user asks for help drafting the request to Helmet, the phrasing that works:

> I'm building a tool that converts Helmet bibliographic records into linked data and clusters them by RDA Work. To test it, I need a curated set of about 15 records exercising specific cases — translations, adaptations, common-title collisions, music recordings vs scores, corporate-body authors, and a few records with known cataloguing quirks. For each record I need the Helmet bib ID and a one-sentence note on why it's interesting. I've drafted the list of cases needed [share Ask 1]. Could you suggest specific Helmet bib IDs that fit each slot? You'll know the catalog far better than I do.

Frame it as a 30-minute task, not an ongoing engagement. Cataloguers usually appreciate the specificity. A Finnish-language snapshot of this request is in `docs/archived/cataloguer-asks-fi.md`.
