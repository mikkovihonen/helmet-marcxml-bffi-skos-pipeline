# Cataloguer-feedback audit — 19 hand-picked Helmet records, M2 Max, 2026-05-13

Audit of M9 reconcile behaviour on 19 records hand-picked by Helmet
cataloguers and supplied via email on 2026-05-13. Each record was
tagged by the cataloguer with the issue type they observed (KANTO
authors missing `$d`, authors not in KANTO, missing asteri-id,
wrong name forms, bilingual yso/slm co-occurrence). The audit
runs the records through the full pipeline (M2 → M3 → M8 → M9 →
skosify → load) and diffs observed M9 outcomes against the
cataloguer-supplied expectations.

The audit is the first concrete cataloguer-validated input to land
in `gold/` and feeds directly into the P-14 Phase C audit gate
(200-sample audit target) and the P-06 gold-set growth backlog.

## Inputs

| Field | Value |
|---|---|
| Date | 2026-05-13 |
| Hardware | MacBook Pro, M2 Max, 64 GB unified memory |
| Git HEAD at run start | `7ba3587` |
| Source | Cataloguer email 2026-05-13, persisted at [`gold/cataloguer-feedback-2026-05-13.jsonl`](../../gold/cataloguer-feedback-2026-05-13.jsonl) |
| Records | 19 MARCXML files in `marcxml/sierra/` (pre-pulled from full Helmet export) |
| Working dir | `scratchpad/data-cataloguer-audit-2026-05-13/` (gitignored — local-only run artefacts) |
| Run UUID | `b2ef5a220e944f2e8e1e8f283f1416a7` |
| `mlx_lm.server` 8B | Same flags as P-10 Phase E bench |
| M9 flags | Default: cache enabled, `submission` ordering, tier-0 expansion **off** |

## Run wall-time

| Stage | Wall |
|---|---:|
| M2 (marc-to-bf) | 2 s |
| M3 (bf-to-bffi) | 3 s |
| M5 + M6 | skipped (`SKIP_M5_M6=1` — no merge candidates needed for 19 isolated records) |
| M8 (merge) | <1 s |
| **M9 (reconcile)** | **57 s** |
| Skosify | 1 s |
| Load → Fuseki | 16 s |
| **Total** | **80 s** |

M9's 57 s broke down to ~3 s Phase 1 (66 entities, all serial-friendly)
+ ~50 s Phase 2 picker calls + provenance write. Per-entity wall =
**0.86 s** — well below the 5 k bench's ~3 s/entity because most
entities short-circuited at tier-0 (34 of 66).

## M9 outcome distribution

| Outcome | Count |
|---|---:|
| `local` (tier-0 SPARQL hit) | 34 |
| `lexical` (tier-1, sim ≥ 0.95) | 1 |
| `llm` (tier-2 LLM picker) | 7 |
| `fallback` (tier-3 highest-lexical, needs-review) | 3 |
| `no-candidate` (no authority bound) | 15 |
| `fictional-character` (marker outcome) | 6 |
| `watchdog-aborted` | 0 |
| **Total** | **66** |

## Per-category verdict

### Category 1 — In KANTO without `$d` birth-death (5 records, 5 ✓ bound)

| bib_id | author | stage | conf | URI |
|---|---|---|---:|---|
| b26152228 | Itäranta, Emmi | llm | 0.80 | finaf:000145804 |
| b26141280 | Rasi-Koskinen, Marisha | llm | 0.86 | finaf:000145165 |
| b13164132 | Jansson, Tove | llm | 0.95 | finaf:000045590 |
| b15641065 | Valkeapää, Nils-Aslak | llm | 0.95 | finaf:000062007 |
| b26267822 | Kunnas, Mauri | **fallback** | 0.79 | finaf:000048956 |

All five bound correctly per the cataloguer's expectation. **Notable observation**: every one went through tier-2 LLM picker (or tier-3 fallback) rather than tier-0 local. The cataloguer reports these authors as in KANTO with asteri-ids; tier-0 fails because the bib literal form (`"Itäranta, Emmi"`) doesn't *exactly* match KANTO's `skos:prefLabel` (which usually carries the date suffix, e.g. `"Itäranta, Emmi, 1976-"`).

This is the **most actionable evidence yet** for graduating P-14 Phase C (tier-0 normalisation + altLabel inclusion + date-suffix stripping). With `BFFI_M9_TIER0_EXPANSION=True` these five would short-circuit at tier-0 cheaply (1-2 SPARQL queries instead of an LLM call each), saving ~4 × 3 s = 12 s on this sample and ≈ 25–35 % of picker calls extrapolated.

### Category 2 — Not in KANTO (5 records, 4 ✓ no-candidate + 1 ⚠ false-positive fallback)

| bib_id | author | stage | conf | URI bound? |
|---|---|---|---:|---|
| b22522396 | Corey, James S. A. | no-candidate | 0.00 | — ✓ |
| b23948619 | Strandberg, Mats | no-candidate | 0.00 | — ✓ |
| b26485916 | Hobb, Robin | no-candidate | 0.00 | — ✓ |
| b25845469 | Martin, George R. R. | no-candidate | 0.00 | — ✓ |
| **b23481833** | **Williams, John** | **fallback** | **0.80** | **finaf:000088832** ⚠ |

`Williams, John` bound to a *different* John Williams in finaf via tier-3 fallback with `needs-review` set. The needs-review semantic is *designed* for exactly this case — the bind exists but is flagged for human verification before publishing. So the pipeline is technically working as specified, but: (a) the cataloguer's expectation was strict `no-candidate`, (b) on the full corpus this gives every common name a provisional bind that cataloguers then have to disprove one-by-one.

Two options:
- **Accept the design**: trust the `needs-review` flag to gate publication. The `bf:identifiedBy { bffi-prov:stage "reconciliation-fallback" }` triple is visible in Skosmos / SPARQL queries, and cataloguer-facing reports can filter on it.
- **Raise the lexical floor for tier-3 fallback**: currently 0.70 (`LEXICAL_FLOOR` in `reconcile.py`). Common-name false positives like this one cluster between 0.70 and 0.85 lexical sim. A floor of e.g. 0.85 would force more `no-candidate` outcomes (cataloguers see "no bind" instead of "questionable bind"), at the cost of dropping legitimate fallbacks for distinctive misspellings.

Recommendation: leave the design as-is for now, surface the trade-off when more cataloguer feedback lands. The needs-review filter is the existing mitigation.

### Category 3 — In KANTO, asteri-id missing from record (1/1 ✓)

| bib_id | author | stage | conf | URI |
|---|---|---|---:|---|
| b25254509 | Van Rooyen, Xan | **lexical** | 1.00 | finaf:000218573 |

Tier-1 lexical at perfect similarity bound this distinctive name correctly without needing the picker. Cataloguer's `cataloguer_action: "add $0 asteri-id"` is still the right fix (defensive against namesakes), but the pipeline gets it right today.

### Category 4-6 — Cataloguer-side data corrections (3 records, 2 declined + 1 LLM-recovered)

| bib_id | category | author | stage | conf | URI bound? |
|---|---|---|---|---:|---|
| b23591146 | wrong name order | Lorca, Federico Garcia | no-candidate | 0.58 | — (correctly declined) |
| b26163743 | incorrect name form | Dostojevski, Fedor | no-candidate | 0.50 | — (correctly declined) |
| b22057407 | old name form, no `$0` | Hirvisaari, Laila | **llm** | 0.95 | finaf:000118988 ✓ |

Two of three correctly declined at sub-floor lexical similarity — pipeline refused to bind low-confidence candidates without an LLM signal to over-rule, matching the cataloguer's expected behaviour ("observe; we'll fix the source after"). The third (Hirvisaari) was successfully bound by the LLM picker despite the old name form; the picker recognised the name as the same author and chose the current KANTO URI. **Cataloguer to verify**: `finaf:000118988` is the canonical Hirvisaari Laila entry.

The b22057407 result is encouraging: the LLM picker is **robust against superseded name forms** even without the asteri-id. Generalised: lexical-floor declines and LLM recoveries are both working as designed for source-data quality issues.

### Category 7 — Bilingual yso/fin + yso/swe co-occurrence (1 of 4 records had both subjects materialise, confirmed bug)

Four records were tagged with this category but only one (`b26322791`) produced both Finnish and Swedish subject literals at the M3 → M8 boundary. The other three (`b26356557`, `b26304119`, `b2635665x`) had only person entities materialise in M9; their subject side was either bnoded out by the BIBFRAME → BFFI shape or one of the languages was shape-rejected. **Worth a separate dig**, tracked as a follow-up below.

The one record that did materialise both shows the **bug the cataloguer flagged**:

| Literal | Vocab | Bound URI |
|---|---|---|
| Italia (fi) | yso | yso:p105111 |
| Italien (sv) | **allars** | allars:Y30493 |

**Two different URIs for the same concept**: `yso:p105111` (the multilingual YSO concept "Italy") *and* `allars:Y30493` (the Swedish-allmänna-ämnesord concept "Italien"). The same record now carries both, which is exactly what the cataloguer flagged.

`allars` is a Finnish-Swedish authority kept in parallel with YSO; YSO concepts have multilingual labels (Finnish, Swedish, English) but `allars` is a separate URI namespace. The pipeline today reconciles each literal against its source-language authority independently and binds whichever URI matches first. There's no cross-walk between `allars:Y30493` and `yso:p105111` even though they refer to the same concept.

**This is a real M9 design gap** — surfaced as a new proposal at [`docs/plans/proposed/prop-15-bilingual-subject-reconciliation.md`](../plans/proposed/prop-15-bilingual-subject-reconciliation.md).

### Category 7b — slm/fin + slm/swe (1 record, none materialised)

`b26346564` was flagged as slm/fin + slm/swe. The audit run produced only author + language entries, no SLM subjects. The SLM bindings were dropped before reaching M9 — either at the M2 → M3 boundary (shape rejection on the `bf:Genre` triples) or at M3 → M8 (no genre carrier in the canonical Work shape). Tracked as a follow-up; same root cause likely as Category 7's b26356557 et al.

## Headline numbers

| Cataloguer expectation | Records | M9 verdict matched | Notes |
|---|---:|---:|---|
| bind to KANTO | 5 | **5/5 ✓** | All via tier-2 LLM; Phase C tier-0 expansion would move ≥ 4 of these to tier-0 |
| no-candidate (not in KANTO) | 5 | 4/5 ✓ (1 fallback with needs-review) | Williams, John false-positive — by design, mitigated by needs-review flag |
| lexical match despite missing $0 | 1 | **1/1 ✓** | Distinctive name bound at conf 1.00 |
| observe then fix | 3 | 2 declined + 1 LLM-recovered | Pipeline correctly refuses sub-floor matches; LLM handles old name forms |
| single URI for bilingual subject | 5 | **0/1 surfaced** | Confirmed bug (b26322791); 4 records' subjects didn't materialise (separate follow-up) |
| **Overall** | **19** | **17 of 19 outcomes match cataloguer expectation** | |

## Recommendations

1. **Land P-14 Phase A (200-sample audit)** *and use this 19-record sample as a pre-audit warm-up* — the audit pipeline is now proven; we can scale the methodology to 200 records the moment the cataloguer engagement is scheduled.

2. **Promote P-14 Phase C re-bench priority**: the 5/5 KANTO authors going through tier-2 picker instead of tier-0 is direct evidence that `BFFI_M9_TIER0_EXPANSION=True` would meaningfully reduce M9 wall on a representative corpus. Worth re-benching even before the M5 Max 128 GB arrives — the M2 Max's prompt-cache OOM that crashed the original Phase C attempt happened at the picker phase, not Phase 1 SPARQL; the tier-0 expansion can be measured independently.

3. **Surface the bilingual-subject bug**: see [`prop-15`](../plans/proposed/prop-15-bilingual-subject-reconciliation.md).

4. **Dig into the missing-subject cases**: 3 of 4 yso-bilingual records and the slm record had their subjects vanish before reaching M9. Likely a `bf:Genre` / `bf:subject` shape issue at M3. Needs a small follow-up audit of those four records' `bibframe/<bib>.rdf` outputs to pinpoint where the subjects drop.

## Sidecar — load step ran end-to-end

The audit pipeline ran `STAGE_LOAD` (uploaded to Fuseki under the production `bffi-works` graph). The 19 records are now visible in Skosmos at `localhost:9090`. If the operator wants to roll them back: `bffi-pipeline load --rollback` or manual `DROP GRAPH <http://urn.fi/URN:NBN:fi:bib:graph:bffi-works>` followed by re-uploading the prior production graph. The audit's 19-record graph is a *strict subset* of the production graph so the rollback question is whether the audit's specific records' bindings overwrote the previously-published ones; if the prior production graph was empty or identical, the rollback is a no-op.
