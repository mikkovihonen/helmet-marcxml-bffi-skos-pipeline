# External dependencies — records to request from Helmet cataloguers

The pipeline can be built autonomously through M0–M4 using synthetic MARCXML, but **M5 onwards requires real records** because the dedup quality, embedding benchmark, and gold-set development all depend on realistic field content. Surface these asks at the milestones below — don't proceed past the gates listed on synthetic data alone.

A Finnish-language version of the cataloguer-facing requests is available in `docs/cataloguer-asks-fi.md` for forwarding directly to Helmet staff.

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

## How to surface these asks

These are dependencies on a human cataloguer, not technical decisions Claude Code can make:

- **Before starting M5:** stop and remind the user that Asks 1 and 3 are needed before proceeding past synthetic data. If real records aren't available, M5 can be developed against synthetic input but cannot be declared done.
- **Before starting M9:** remind about Ask 2.
- **Before the M10 production publish (not the sample publish):** remind about Ask 4.

If asks are not yet fulfilled, proceed with synthetic data where possible and explicitly note in the PR description and runbook which milestone is currently "blocked on external input." The user decides whether to wait or work around.

## Phrasing for the cataloguer

When the user asks for help drafting the request to Helmet, the phrasing that works:

> I'm building a tool that converts Helmet bibliographic records into linked data and clusters them by RDA Work. To test it, I need a curated set of about 15 records exercising specific cases — translations, adaptations, common-title collisions, music recordings vs scores, corporate-body authors, and a few records with known cataloguing quirks. For each record I need the Helmet bib ID and a one-sentence note on why it's interesting. I've drafted the list of cases needed [share Ask 1]. Could you suggest specific Helmet bib IDs that fit each slot? You'll know the catalog far better than I do.

Frame it as a 30-minute task, not an ongoing engagement. Cataloguers usually appreciate the specificity. The Finnish-language version of this request is in `docs/cataloguer-asks-fi.md`.
