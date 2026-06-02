# P-42 — Universalise the P-41 salvage-regex surfaces

**Status**: proposed.

**Scope**: 1-2 weeks. Phase A (audit + extract the hand-curated tables into a single config) is 1-2 days. Phase B (replace the ad-hoc matching logic with a tokenize-then-extract cascade) is 3-5 days. Phase C (decide whether to add a probabilistic-NER tier, authority-driven matching, or both) is the scope-dependent piece — could land as a small follow-up or could grow into a multi-week sub-project depending on which alternative the evaluation picks.

**Proposal-base commit**: `d92083c`. The proposal reasons about the code as it stood at the end of the 2026-06-02 P-41 work — six commits of regex / lookup-table refinement after the initial Phase B.1 implementation (`4602aad`), each driven by a specific corpus-found failure mode. Re-run `git diff d92083c..HEAD --
src/bffi_pipeline/stages/m2/salvage_245c.py
src/bffi_pipeline/stages/m2/salvage_245c_llm.py
src/bffi_pipeline/stages/m2/salvage.py
src/bffi_pipeline/stages/m2/salvage_publisher.py
src/bffi_pipeline/stages/m2/salvage_sentinel.py`
before acting on this proposal — the seven lookup tables Phase A would consolidate may have grown further if production has surfaced more language-specific gaps in the interim.

## Motivation

P-41 Phase B.1's deterministic 245$c parser shipped as a thin regex over hand-curated lookup tables. The 2026-06-02 5 k corpus-bench cycle surfaced **six** distinct rule-coverage gaps in a single afternoon, each requiring a separate commit:

| Commit | What was missing | Where it lived |
|---|---|---|
| `0e0d554` | German `"herausgegeben von"` + 8 more English/Finnish/Swedish role markers | `_ROLE_MARKERS` |
| `2d2f79d` | Single-pass role-marker stripping missed `"ED. BY X"` (nested markers); 5 music-domain markers missing (`"arranged by"`, `"recordings by"`, etc.) | `_ROLE_MARKERS` + `_split_role_prefix` semantics |
| `57a7da5` | German `und` + French `et` missing from multi-author separators | `_SEPARATORS_RE` |
| `cee4d21` (3-in-1) | (a) B1-LLM stopword filter missing; (b) leading ISBD punctuation blocked role-marker detection; (c) name-order mismatch on synthesised creators | `_LLM_REJECT_NAMES` + `_LEAD_PUNCT_RE` placement + new `_NAME_PARTICLES` |

The pattern is the issue, not any single gap. **Seven hand-curated lookup tables are spread across three files**, each indexed by language / cultural-context / role enum, with non-uniform matching logic (startswith, lowered-substring, regex alternation, token-set membership), and each new corpus exposure surfaces another N gaps that need their own commit:

| Table | File | Indexed by | Match shape |
|---|---|---|---|
| `_ROLE_MARKERS` (≈28 entries) | `salvage_245c.py` | language × phrase | case-insensitive `startswith` |
| `_SEPARATORS_RE` (6 conjunctions) | `salvage_245c.py` | language | regex alternation |
| `_CORPORATE_MARKERS` (≈20 entries) | `salvage_245c.py` | language | case-insensitive substring |
| `_NAME_PARTICLES` (≈20 entries) | `salvage.py` | culture | token-set membership |
| `_LLM_REJECT_NAMES` (≈25 entries) | `salvage_245c_llm.py` | language | exact lowered |
| `_RELATOR_TERMS` (6 entries) | `salvage.py` | role enum | dict lookup (Finnish-only output) |
| `_NAME_TOKEN_RE` + token-count bounds | `salvage_245c.py` | (universal) | regex + len() |

Adding French coverage today means edits in **5 of those 7 files**: French role markers, French separator (`et` — landed at `57a7da5`), French corporate markers, French particles, French stopwords. Adding Estonian, Russian, Polish, or Korean coverage as the corpus expands beyond the FI/SE/EN core multiplies the same per-language fan-out. The data-vs-code split has eroded — language data is hard-coded inside the matching logic.

Two deeper issues amplify the lookup-table sprawl:

1. **The role enum is too coarse for the cataloguer's actual relator vocabulary.** B1's role tags are `{author, editor, translator, illustrator, compiler, unknown}`, while MARC relator codes (used by P-41 Phase B's sibling `contrib_extract_llm.py` cascade at M3) include `aut / trl / ill / pht / edt / cmp / prf / aft / aui / ctb / nrt / drt / aus / pro / arr / lyr / cnd / mus / adp / sng` — 20 codes. Music-domain markers like `"arranged by"`, `"recordings by"`, `"performed by"` had no place to land in the enum and were mapped to `"unknown"` as the best available fit, costing 0.2 confidence and an explicit `"surname-first"` reformat skip on a per-role basis. The narrower enum is an artefact of "we'll fill it in later"; later is now.
2. **B1 regex and B1-LLM solve the same problem with different rules.** Both extract personal-name agents from 245$c. B1 regex's 2-token shape check + corporate-marker rejection + verbatim-name preservation is a *conservative* version of what the LLM does. The duplication means a corpus pattern can be caught by one tier but not the other (e.g. mononyms like `"Blanasi"` — B1 rejects, B1-LLM accepts), and the operator can't tell from a per-record audit which logic should have fired. A unified rule set with a confidence dial (deterministic-high / deterministic-medium / LLM-cascade) is a cleaner architectural shape than two parallel implementations with overlapping responsibilities.

P-41 is *working* — RUN3's 674 synthesised agents, 70 surname-first reformats, zero hallucinations, zero stopword leaks. The proposal isn't to fix bugs; it's to consolidate the lookup-table sprawl before the next language expansion forces six more single-purpose commits.

## Approach

Three sequenced phases. Phase A is independent and could ship on its own (the consolidation is valuable even without changing logic). Phase B is the largest change; Phase C is scope-dependent and could be deferred to a follow-up plan if Phase A + B already moved the maintainability needle far enough.

### Phase A — Consolidate the seven lookup tables into one config file

- New file `config/salvage-locale.yaml` (or `.toml`) keyed by ISO 639-1 / 639-3 language code, with sub-blocks for `role_markers`, `separators`, `corporate_markers`, `name_particles`, `stopwords`, and `relator_terms`. Each block has a uniform shape so the loader doesn't need a per-table branch.
- Loader at `src/bffi_pipeline/stages/m2/salvage_locale.py` reads the file at module-import time, validates the schema (Pydantic, like everywhere else in `bffi_pipeline`), and exposes a single `Locale` value object per language plus a `MergedLocale` view that flattens all configured languages (matching the current "any-language-matches" semantics).
- Match-shape uniformity: all tables become token-set or phrase-set membership. The few cases where order matters (role-marker longest-first) get an explicit `priority: int` field per entry so the YAML drives the order rather than file-position.
- No logic change in this phase. The five files under `stages/m2/` import from the loader instead of carrying their own constants. Phase A's success criterion is a byte-identical RUN3 re-bench output.

### Phase B — Replace the ad-hoc matching with a tokenize-then-extract cascade

The current parser tangles three concerns inside `parse_245c`:

1. Detach the role marker (`_split_role_prefix`).
2. Split on separators (`_SEPARATORS_RE`).
3. Validate each candidate's shape (`_validate_name_shape`).

Phase B separates these into a pipeline:

```
245$c text
   │
   ▼
[tokenize]    — language-agnostic; uses ICU word-segmentation
   │           (or Python's existing locale-aware split where ICU is overkill)
   ▼
[tag tokens]  — role-marker / separator / corporate / particle / name-token /
   │           punctuation, lifted from the Locale config
   ▼
[extract]     — group adjacent name-tokens into candidates;
   │           role tag = the rightmost role-marker tag before the group;
   │           reject candidates containing corporate tokens;
   │           reject candidates failing min/max-token bounds.
   ▼
list[ParsedAgent]
```

The cascade replaces the current regex-with-postprocess shape with a clearly-staged pipeline whose individual stages are testable in isolation. The recursive role-marker stripping currently in `_split_role_prefix` becomes "find ALL role-marker tokens and use the closest one to the candidate" — naturally handles `"ED. BY X"` (closest role to X is `"by"` → author, but `"ed."` is also nearby → editor wins per the existing first-match-wins rule, encoded as a priority).

The role enum widens to MARC relator codes (~20 entries) so music-domain markers no longer have to map to `"unknown"`. The relator-code-to-role-tag table in `_RELATOR_TERMS` becomes a thin pass-through: B1's output is a MARC relator code, which the synthesised `7XX$e` carries verbatim; the Finnish display labels become consumer-side rendering (Skosmos overlay), not B1's responsibility.

### Phase C — Decide whether to add probabilistic NER or authority-driven matching

Three alternatives the evaluation should weigh against the Phase A + B baseline:

- **NER tier**: spaCy's multilingual `xx_ent_wiki_sm` (or Flair) for person-name extraction. Slots between B1 regex and B1-LLM as a deterministic-but-probabilistic middle tier. Faster than the LLM, more permissive than the regex. Cost: one extra dependency (~1 GB model file in the pre-existing local-inference cache), plus calibration work to set a confidence threshold above which the NER tier's verdict is trusted. The verbatim-substring constraint still applies — same defence as B1-LLM.
- **Authority-driven matching**: for each 245$c, query KANTO + VIAF for personal / corporate candidates whose preferred labels are substrings of 245$c. The authority match gives us not just "is this a name?" but "which canonical agent is it?" — eliminating M9's reconciliation pass for the salvaged records. Cost: HTTP-bound (KANTO / VIAF are external); already-cached calls via M9's existing infrastructure could be re-used.
- **Drop B1 regex entirely; rely on B1-LLM + B3.** Simpler architecture (one less tier, one less code path). The deterministic regex pass currently handles 28 / 75 (37 %) of non-B3 salvages — those records would move to B1-LLM. Cache-warmed re-runs would be near-zero-cost; first runs would pay more LLM latency. Loses the "fast deterministic path" defence-in-depth that the regex provides today. Worth weighing only if Phase B's tokenize-then-extract cascade still feels excessive after the consolidation.

The proposal doesn't pick a winner — that's Phase C's evaluation. A small bench against the RUN3 corpus (preferences: cost, latency, accuracy, additional dependencies, audit-trail clarity) drives the decision.

## Prerequisites

- **P-41 shipped** (it has; see `docs/plans/completed/p-41-minimum-bibliographic-synthesis-and-export-report.md`). P-42 builds on the salvage layer's existing dispatch, cache, provenance, and TSV surfaces — none of those change.
- **An updated 5 k bench corpus that has been through P-08's 33X synthesis.** The RUN1-3 numbers in P-41's body are against a pre-P-08 corpus where 22.22 % of records dropped at the 33X gate before P-41's salvage layer could see them. A Phase B regression test against the same population that the baseline 2026-05-14 sample drew from would let us see the salvage-layer-only delta without the 33X-drop noise. Sequencing: rerun the Sierra export through P-08 (operator action), then snapshot the resulting MARCXML as a versioned fixture under `tests/fixtures/`.
- **No active plan touches `stages/m2/salvage*.py`** at proposal-base time. None today; revisit before acting.

## Risks

- **Different agent names from the same 245$c input.** Phase B's tokenize-then-extract cascade may produce a slightly different `ParsedAgent.name` than the current regex on edge cases (e.g. how internal punctuation is preserved). The downstream impact is on M5 blocking — same-author records may end up in different blocks if the tokenisation changes the surname token. **Mitigation:** Phase A's byte-identical-RUN3-output success criterion confirms the consolidation doesn't change behaviour; Phase B's regression test pins the per-record-output diff to a manageable size (target: ≤ 5 % of agents change form) and surfaces every change for cataloguer review before merge.
- **The role-enum widening is a vocabulary version bump.** Going from 6 role tags to ~20 MARC relator codes changes the values consumers see in `bffi-prov:syntheticMethod` and on synthesised `7XX$e`. This is `bffi-prov v2` territory if P-09's vocabulary-version-bump landing rule still holds. **Mitigation:** stage the bump as P-09 did — add the new codes alongside the old, transition the salvage emit, drop the old enum in a follow-up release.
- **A YAML-driven config is a new operator-facing surface.** Adding French now means editing the config rather than the source — a different operational ergonomic. **Mitigation:** the config validates on startup (Pydantic), and a `make validate-salvage-locale` target can run against test fixtures. The CI gate stops bad configs from shipping.
- **Phase C's NER / authority alternatives have a non-trivial dependency cost.** spaCy is a ~1 GB model + ~100 MB code; KANTO/VIAF authority hits add HTTP latency to every salvage event. **Mitigation:** Phase C is explicitly scoped as an *evaluation* gate, not a "do all three" plan. The decision criterion is a cost/benefit bench against RUN3-equivalent corpus state; the result might be "neither — Phase A + B is enough".

## Open questions

- **Is YAML the right config format?** The project uses Pydantic Settings for env-var config and Jinja2-templated SPARQL for query parametrisation. YAML for static lookup tables is idiomatic Python but not yet established in `bffi_pipeline`. Alternatives: TOML (Python 3.11+ stdlib reader, no extra dep); a Python module that just exports the dicts (no parsing, fully typed). **Lean YAML** — operators are already comfortable with YAML for the Skosify overlay (`config/bffi.cfg`) and the docker-compose; mixing Python literals into a config file feels harder to lint.
- **Does the role-enum widening happen in Phase B or in a separate plan?** The widening is required for the music-domain markers to stop being mapped to `"unknown"`, but it ripples into the test fixtures + the SynthesisRecord's method tag format + Skosmos overlay rendering. **Lean: same plan, Phase B's job** — the widening is the bottleneck that makes Phase B's "tokenize then tag" architecture pay off; doing them separately means the cascade keeps emitting `"unknown"` for cases the role enum should already cover.
- **Counterpoint — is this over-engineering a working system?** P-41 RUN3 has 1.83 % drop rate, 643 salvaged records, 70 surname-first reformats, zero hallucinations. The lookup tables work. The proposal isn't responding to a bug; it's responding to *the velocity of bug-fix commits per corpus exposure*. If the corpus expansion timeline is "Helmet only, no further languages in scope" the lookup-table sprawl is bounded and Phase A alone is plenty. If the expansion timeline includes other Finnish-library corpora (per the P-09 library-agnostic-source proposal), Estonian/Russian/Polish coverage will need to land cleanly. The decision threshold is: *do we expect to add a new language to the salvage layer within 6 months?* If yes, P-42 is on the critical path; if no, P-42 is "nice to have" and Phase A is the right scope.
- **Should Phase B replace `_to_surname_first` with a per-locale name-formatting strategy?** The current implementation hard-codes "last token is surname" + a Western-particle skip-list. Hungarian / Chinese / Japanese / Korean naming conventions put the family name first. Today's corpus is Helmet-only (Finnish + Swedish + English-dominant), so the Western assumption holds for 99 % of records; widening would invert that. **Lean: keep the Western heuristic as the default, add per-locale overrides in the YAML config.** A name with a declared MARC 041 language code of `hun` / `chi` / `jpn` / `kor` would skip the reformat or use a locale-specific reformatter.
- **Where does the B1-LLM cascade fit in the unified architecture?** Phase B's tokenize-then-extract cascade is for the deterministic tier; the LLM stays as the fallback for tokens that don't match any rule. The boundary becomes "rule-based extraction returns ≥1 agent → use it; returns 0 → invoke LLM". The verbatim-substring + stopword + plausibility checks already established in `_enforce_verbatim_substring` survive unchanged.
- **Alternative — would a corpus-driven rule miner be simpler than hand-curating rules?** Mine the cataloguer-coded MARC 245$c → 100/700 mappings from the corpus (records where BOTH the structured 1XX/7XX and a parseable 245$c exist) to learn the "this 245$c word → this role" associations statistically. Output: an auto-generated lookup table that ships alongside the hand-curated one. Higher coverage at the cost of less-explainable rules; could land as a Phase B variant. Worth a 1-day spike against the corpus to see if the signal exists.

## Cross-references

- `docs/plans/completed/p-41-minimum-bibliographic-synthesis-and-export-report.md` — the plan whose regex-sprawl this proposal addresses; the "Verification status" + "Post-mortem" sections enumerate the six lookup-table-related fixes the bench cycle surfaced.
- `src/bffi_pipeline/stages/m2/salvage_245c.py` — `_ROLE_MARKERS`, `_SEPARATORS_RE`, `_CORPORATE_MARKERS`, `_NAME_TOKEN_RE`, `_split_role_prefix`.
- `src/bffi_pipeline/stages/m2/salvage.py` — `_NAME_PARTICLES`, `_RELATOR_TERMS`, `_to_surname_first`.
- `src/bffi_pipeline/stages/m2/salvage_245c_llm.py` — `_LLM_REJECT_NAMES`, `_is_plausible_name`. The plausibility filter would survive Phase B unchanged; the stopword list would move to the locale config.
- `src/bffi_pipeline/contrib_extract_llm.py` — the M3-stage 245$c contributor cascade that uses the full MARC relator code set (`VALID_RELATOR_CODES`); Phase B's role-enum widening should converge on the same set so the two stages share vocabulary.
- `docs/plans/proposed/p-09-library-agnostic-source.md` — composes: P-09 decouples `bffi_pipeline` from FI-HELME-specific URIs; P-42 decouples salvage from FI-locale-specific lookups. Both proposals point at the same multi-library / multi-locale future.
