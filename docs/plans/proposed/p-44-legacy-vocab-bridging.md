# P-44 — Bridge legacy YSA / Allärs / MUSA / CILLA subjects into YSO via Fuseki mapping triples

**Status**: shipped as a single commit (Phase A + B + C + tech-stack note). Phase D (diagnostic update) and Phase E (eval-set pin) remain in scope but were not blocking the initial ship.

**Scope**: 1-2 days. Data layer (load_finto.py) + resolver chain (local_concept_resolver.py + requests.py routing) + tests + tech-stack docs.

**Proposal-base commit**: `fb00be4` ("M9 picker: Phase B candidate-context + eval harness + Gemma 4 26B-A4B").

## Motivation

Helmet's ~800k MARC corpus spans decades. Pre-2019 records carry subject headings under the legacy Finnish thesauri (`$2 ysa`, `$2 allars`, `$2 musa`, `$2 cilla`); only newer records use YSO / ALLFO / SLM tags. M9 currently routes any `$2 ysa` literal into the YSO reconciliation chain because the 2014-2018 YSA→YSO merge brought YSA's prefLabels into YSO unchanged — most old literals resolve at tier-0 (exact prefLabel match in Fuseki) without anyone noticing they came from a legacy vocab.

But ~4 % of subject literals slip through. These split into two failure modes:

1. **Terminology evolution** — YSA used an older Finnish term; YSO renamed it during ontologisation. Examples surfaced from the post-load Fuseki graphs:
   - `"mustalaismusiikki"` (now-offensive) → `yso:p19958 "romanimusiikki (viihdemusiikki)"`
   - `"värilliset"` (outdated) → `yso:p9959 "POC-ihmiset"`
   - `"sydämenhieronta"` → `yso:p10109 "paineluelvytys"` (CPR, modernised)
   - `"elektroniset kirjastot"` → `yso:p22354 "digitaaliset kirjastot"`
   - `"kasvainoppi"` → `yso:p12865 "onkologia"` (Finnish → Latin)
   The cataloguer's literal matches the YSA prefLabel but NOT the YSO prefLabel — so YSO tier-0 misses. The YSA SKOS dump carries `skos:exactMatch` / `closeMatch` triples directly to YSO; the bridge tier follows them.
2. **Vocabulary deprecation** — MUSA (music) and CILLA (visual arts, merged into MUSA in 2019) terms that were never renamed to YSO, but rather marked `dct:isReplacedBy` a YSA concept which IS bridged to YSO. The bridge needs to follow the two-hop chain.

What the bridge does NOT close: cataloguer-typed bare forms that don't appear in any vocabulary's prefLabel at all (these are the cases `yso-marcbib` historically resolved via bundled lookup tables, separate from the SKOS RDF). Those remain manual-triage cases via the existing `ysa-disambiguation-report` diagnostic.

At 800k records the diagnostic doesn't scale.

NLF has published the migration data we need. Three artefacts surfaced via research:

- **`NatLibFi/yso-marcbib`** (archived 2021-09, GPL). MARC21/MARCXML in-place subject heading rewriter. Reference; not consumed.
- **`NatLibFi/Finto-data`** (live, nightly cron). YSO SKOS releases at `vocabularies/yso/releases/<year>.<n>.<codename>/yso-skos.ttl`. **YSA and MUSA also published as standalone SKOS dumps under the live `api.finto.fi/download/` endpoint** that `load_finto.py` already consumes. YSA's SKOS file carries `skos:exactMatch` / `closeMatch` triples directly into YSO. MUSA's SKOS file carries `dct:isReplacedBy` triples into YSA (two-hop bridge to YSO).
- **`NatLibFi/bib-rdf-pipeline`** (archived 2025-06). NLF's own reference MARC → RDF pipeline. Architectural sanity check; not a dependency.

## Approach

Load YSA and MUSA (which contains CILLA — they're merged in the published dump) as standalone Fuseki named graphs alongside the existing YSO + Allärs + KAUNO + … graphs. Extend `FusekiConceptResolver.resolve()` with a second internal tier that fires on `kind=subject` lexical misses: SPARQL-UNIONs YSA, Allärs, and MUSA branches, filters destinations to the YSO namespace, returns the YSO URI with a `via-ysa` / `via-musa` / `via-allars` source-vocabulary tag for provenance.

**Source MARC stays pristine** — the bridge happens at reconcile time and lands as a typed step in the audit log via the new `source_vocabulary` tag (`via-*`). Aligns with the project's "delay reconciliation to runtime; never mutate cataloguer input" stance.

## Phases

**Phase A — vendor the legacy SKOS dumps** *(shipped)*

Web research confirmed `api.finto.fi/download/{ysa,musa}/…-skos.ttl` both serve HTTP 200. Existing `load_finto.py` live-fetch + cache pattern works; no need to vendor from GitHub.

**Phase B — load YSA + MUSA into Fuseki** *(shipped)*

Two new `FintoVocab` entries in `src/bffi_pipeline/stages/m10/load_finto.py`. URI prefixes:
- YSA: `http://www.yso.fi/onto/ysa/`
- MUSA: `http://www.yso.fi/onto/musa/`

Test regression pin in `tests/unit/test_load_finto.py::test_canonical_vocab_list_covers_expected_authority_vocabularies` extended with both vocab IDs.

**Phase C — M9 legacy-mapping tier** *(shipped)*

New `_build_legacy_mapping_query` + `FusekiConceptResolver._legacy_mapping_match` in `src/bffi_pipeline/stages/m9/local_concept_resolver.py`. SPARQL UNION across three branches:

1. **YSA → YSO** (direct, `skos:exactMatch` / `closeMatch`)
2. **MUSA → YSA → YSO** (two-hop via `dct:isReplacedBy` then `skos:exactMatch`)
3. **Allärs → YSO** (direct, same shape as YSA)

`FILTER (STRSTARTS(STR(?uri), "http://www.yso.fi/onto/yso/"))` enforces YSO destination on every branch. Language preference via `ORDER BY` biases toward the Finnish or Swedish form. Subject-kind only — `kind in ("genre_form", "music_form")` short-circuits before the legacy query fires.

Routing for `$2 musa` / `$2 cilla` added to `_SOURCE_TOKEN_TO_KIND` in `requests.py` (both → `"subject"`).

`LEGACY_BRIDGE_VOCABS` frozenset exported so provenance writers can test `hit.source_vocabulary in LEGACY_BRIDGE_VOCABS` and emit a `bffi-prov:via-legacy-mapping` typed step.

**Phase D — diagnostic + tech-stack docs** *(partial — docs shipped, diagnostic deferred)*

`docs/tech-stack.md` "Authority vocabularies" table extended with the YSA + MUSA rows + bridge mechanism.

Deferred to a follow-up commit: extending `src/bffi_pipeline/stages/m9/ysa_disambiguation_report.py` to flag which surfaced cases the new tier would have caught — useful as a "lift" metric but not blocking.

**Phase E — eval-set pin** *(open)*

Two new cases worth adding to `gold/picker-eval/cases.jsonl` once the corpus run confirms behaviour:

- A bare-YSA case (`"lapset"`) — expected: chose, `bffi-prov:via-legacy-mapping` to YSO disambiguated form
- A MUSA case (`"transkriptiot (musiikki)"`) — expected: chose, two-hop bridge

Pin them so a future model swap or SPARQL change can't silently regress the bridge.

## Critical files

Touched in the initial ship:

- `src/bffi_pipeline/stages/m10/load_finto.py` — `FINTO_VOCABS` extended with YSA + MUSA
- `src/bffi_pipeline/stages/m9/local_concept_resolver.py` — new `_legacy_mapping_match`, `_build_legacy_mapping_query`, `VOCAB_VIA_*` constants, `LEGACY_BRIDGE_VOCABS` set
- `src/bffi_pipeline/stages/m9/requests.py` — `$2 musa` / `$2 cilla` routed to `kind=subject`
- `tests/unit/test_local_concept_resolver.py` — 8 new tests covering the bridge tier
- `tests/unit/test_load_finto.py` — regression pin updated
- `docs/tech-stack.md` — Authority vocabularies table

Untouched (intentionally):

- `src/bffi_pipeline/stages/m9/ysa_disambiguation_report.py` — still useful as the "did anything still slip through" diagnostic after the tier lands; can be enriched in a follow-up
- `prompts/` — no prompt changes; the LLM picker doesn't see the bridge
- Source MARC — never rewritten; the bridge is runtime-only

## Verification

1. **Unit-level** *(shipped)*: 27 tests in `tests/unit/test_local_concept_resolver.py` pass under `make test`. `mypy --strict` clean. 1361 total tests pass.
2. **Live re-run** *(shipped)*: `bffi-pipeline load-finto` pulled the YSA + MUSA dumps. Post-load Fuseki state:
   - YSA: 433,048 triples (new graph at `http://www.yso.fi/onto/ysa/`)
   - MUSA: 13,537 triples (new graph at `http://www.yso.fi/onto/musa/`)
   - Bridge coverage: 36,420 YSA→YSO + 36,460 Allars→YSO + 946 MUSA→YSA→YSO concepts have mappings.
   - End-to-end smoke against Fuseki: MUSA two-hop confirmed working (`"transkriptiot (musiikki)" → yso:p2759 "nuotinnokset"`).
3. **Smoke comparison** *(shipped, run `20260605-1939-5ed1c4` vs baseline `20260605-1507-49f66b`)*:

   | Metric | Gemma baseline | + Bridge | Δ |
   |---|---|---|---|
   | Picker calls (LLM tier-2) | 216 | 194 | -22 (-10%) |
   | `llm_pick` | 196 | 186 | -10 |
   | `fallback` / `needs_review` | **20** | **8** | **-12 (-60%)** |
   | M9 wall time | 3266 s | **2246 s** | **-31%** |
   | Bridge hits in `provenance.ttl` | n/a | **206** | 140 YSA + 50 MUSA + 16 Allärs |

   All 206 bridge resolutions landed at `confidence=1.0` (deterministic), stage `reconciliation-local` — never reached the Finto API, never reached the LLM. Sample resolutions surfaced in the smoke (the legacy literal is the cataloguer's input, the YSO concept is what the bridge resolved to):

   - **via-ysa** (140 hits, 34 distinct pairs): `Itävalta → yso:p105294`, `Yhdysvallat → yso:p105078`, `Kauniainen → yso:p94199`, `englanninkielinen kirjallisuus → yso:p11223`, `esteettömyys → yso:p16241`
   - **via-musa → ysa → yso** (50 hits, 21 distinct): `heavy rock → yso:p15521`, `hip hop → yso:p14336`, `bassokitara → yso:p2666`, `baritoni → yso:p29854`, `joululaulut → yso:p16560`
   - **via-allars** (16 hits, 12 distinct): `engelsk litteratur → yso:p11223`, `Esbo → yso:p94105`, `Vanda → yso:p94474`, `Grankulla → yso:p94199`, `miljöeffekter → yso:p9862`

   Cross-language unification works as designed: `Grankulla` (Swedish) and `Kauniainen` (Finnish) both resolve to `yso:p94199`; `engelsk litteratur` and `englanninkielinen kirjallisuus` both to `yso:p11223`.

4. **Diagnostic** *(pending)*: re-run `bffi-pipeline ysa-disambiguation-report` on the new canonical graph. Expect: terminology-evolution misses (which the report does surface today) drop; the residual cases will be true bare-form cataloguer typos that no SKOS-based bridge can catch.

## What this plan deliberately doesn't do

- **Doesn't import or fork `yso-marcbib`**. The GPL tool is archived; the data it consumes is published separately under CC0. We use the data directly.
- **Doesn't rewrite source MARC**. NLF's tool stance is in-place MARC mutation; ours keeps source MARC as the cataloguer's ground truth and emits the bridge into the canonical graph + audit log.
- **Doesn't load YSA-paikat as a separate graph**. The 19.6-MB YSA place-names dump is deferred — current ship covers the bigger general-thesaurus gap; place-name misses are rarer in the audit.
- **Doesn't add a new public protocol method**. `LocalConceptResolver.resolve()` keeps its signature; the chain is internal to `FusekiConceptResolver`. Stubs and tests work unchanged.
