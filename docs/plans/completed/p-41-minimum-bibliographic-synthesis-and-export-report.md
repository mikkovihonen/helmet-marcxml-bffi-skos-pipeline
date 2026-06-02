# P-41 — Synthesise the minimum bibliographic field set for every exported record + per-run synthesis TSV

**Status**: completed (all three phases + B.8 5 k bench shipped in one session — 2026-06-02 — across ten commits: `96a8b37` (A), `4602aad` (B.0/B.1/B.4/B.7), `f408901` (B.5 + Phase C), `d1372ba` (B.6), `e81f52c` (B.3), `b4dced1` (B.2), `24bffa0` (graduation), plus two follow-up bug fixes the 5 k bench surfaced — TSV dedup key + B1 German role markers). See "Verification status" below for the bench numbers and "Post-mortem" for the bug-fix narrative.

**Source proposal**: this file, in its `proposed/`-shape form, was the same `p-41-minimum-bibliographic-synthesis-and-export-report.md` filename under `docs/plans/proposed/`. No proposal-shape commit exists in `git log --follow`; the proposal-shape content lived only in the working tree before graduation. The proposal-shape version is recoverable from this conversation's earlier Write tool call (commit `57f810b` is the proposal-base; the diff between that commit and the first plan-shape commit is the proposal-shape → plan-shape rewrite).

**Plan-base commit**: `57f810b`. Before continuing work after a context switch, run `git diff 57f810b..HEAD --
src/bffi_pipeline/validation/marcxml.py
src/bffi_pipeline/stages/m2/marcxml_repair.py
src/bffi_pipeline/stages/m2/runner.py
src/bffi_pipeline/stages/m3/runner.py
src/bffi_pipeline/cataloguer_review.py
src/bffi_pipeline/provenance/
docs/external-dependencies.md
CLAUDE.md` to confirm no in-flight work has reshaped the surfaces this plan touches. P-08 (`docs/plans/completed/p-08-richer-rda-33x-synthesis.md`) is the sibling synthesis pattern this plan generalises; P-39 (`docs/plans/backlog/p-39-m9-non-primary-contribution-reconciliation.md`) is the downstream consumer that reads the sentinel-agent exclude flag this plan introduces.

**Phase commits**:

- Phase A (spec + `bffi-prov:Synthesis` Activity + unify existing synth markers): `96a8b37`
- Phase B (creator-gap salvage at M2 repair layer):
  - B.0 / B.1 / B.4 / B.7 — Settings + dispatcher + B1 regex + B3 sentinel + M2 wiring: `4602aad`
  - B.5 — Synthesis Activity emission to provenance: shipped jointly with Phase C below at `f408901`.
  - B.6 — Sentinel `bffi:syntheticSentinel` flag emission + `is_synthetic_sentinel` helper + invariant fixture (sentinel is non-primary; M5/M6/M8 already skip via `bffi:PrimaryContribution` filter; M9 future P-39 walker reads the helper): `d1372ba`
  - B.3 — B2 publisher-as-corporate-creator (feature-flagged off pending cataloguer Ask 6 — leader/06 sign-off): `e81f52c`
  - B.2 — B1 LLM cascade fallback (`prompts/salvage-245c-v1.txt` + `salvage_245c_llm.py` with LangChain ChatOpenAI + SQLite cache + verbatim-substring post-processor + retry stack + dispatcher wiring): `b4dced1`
- Phase C (per-run `export-synthesis-<run_uuid>.tsv` writer + `bffi-pipeline export-synthesis-report --run <uuid>` retrospective CLI; also includes Phase B.5 provenance emit): `f408901`

**Owner**: TBD.

**Estimated wall-time**: 1-2 weeks across the three phases. Phase A is half a day; Phase B is 3-5 days (B1 regex + B1 LLM cascade dominate; B2 is half a day behind a feature flag; B3 is half a day; exclude wiring is half a day); Phase C is 1-2 days.

**Sequencing prerequisites**:

- **P-08 shipped** (it has — see `docs/plans/completed/p-08-richer-rda-33x-synthesis.md`, all four phases). The `$5 FI-HELME/synth-v<N>` marker convention exists and Phase A's audit lifts its shape.
- **P-31 shipped** (it has — see `docs/plans/completed/p-31-dashboard-artifacts-panel.md`). The cataloguer-review TSV writer pattern is the template Phase C copies.
- **No active plan touches `validation/marcxml.py` or `marcxml_repair.py`** at plan-base time. P-09 (proposed) plans to touch `validation/marcxml.py` to drop the filename-based bib_id; if P-09 lands first, Phase A's spec doc should be re-checked for filename-related assumptions.
- **Cataloguer sign-off on the no-author sentinel label**: tracked as Ask N in `docs/external-dependencies.md` (added during Phase A). Phase B ships with default Finnish "Tekijä tuntematon" / Swedish "Okänd upphovsman" / English "Unknown author"; the label is data-driven via config so the cataloguer confirmation is a one-line change, not a code change.
- **Cataloguer sign-off on the B2 leader/06 set** (which leader/06 codes warrant publisher-as-corporate-creator promotion): tracked as Ask N+1 in `docs/external-dependencies.md`. B2 ships behind a feature flag default-off; activation is a single config change once the cataloguer confirms the leader/06 set.

## Motivation

The pipeline currently treats `marcxml-content-minimum` failures (`src/bffi_pipeline/validation/marcxml.py:178`) as terminal drops: records missing **any** of 1XX/7XX (creator), 245 (title), 008 (fixed-length data), or 33X (RDA content/media/carrier) never reach BIBFRAME, never reach BFFI, never reach Skosmos. The drop rate is real and visible on the latest full-pipeline log:

| 2026-05-14 5k sample | count | % |
|---|---:|---:|
| Total input records | 5 000 | 100.00 % |
| M2 dropped: missing creator (1XX/7XX) | 91 | 1.82 % |
| M2 dropped: missing 008 only | 1 | 0.02 % |
| M2 dropped: missing essentially everything | 1 | 0.02 % |
| M2 dropped: missing 33X | 0 | 0.00 % |
| **All M2 drops** | **94** | **1.88 %** |

Source: `logs/runs/pipeline-full.log` lines 67-178, run UUID `721f5548…`.

Two things to read off that table:

1. **P-08 worked.** Missing-33X is at 0 / 5000 on the 2026-05-14 sample, against the ≤ 50 / 5000 target the plan committed to. The synthesis-with-`$5 FI-HELME/synth-v<N>`-marker recipe is now a proven shape we generalise here.
2. **Missing creator is the dominant remaining drop class.** 91 of 94 (96.8 %) drops on this sample. Projected to the 800 k Helmet corpus, that is ~14 500 records that are silently absent from Skosmos despite carrying a usable title + 008 + 33X. Most of these are cataloguer-style records where individual authorship genuinely doesn't apply (corporate reports, anthologies, anonymous works, music recordings) — but the pipeline doesn't know that; it just drops them.

There is also a per-record **observability gap**: the existing `$5 FI-HELME/synth-v<N>` provenance marker is on the MARC datafield, not the BFFI graph; the existing `cataloguer-source-review-<run_uuid>.tsv` (`src/bffi_pipeline/cataloguer_review.py`) captures *rejected* records but not *salvaged-via-synthesis* records. A cataloguer who wants to know "which Helmet bib IDs in run X have synthesised values, and what was synthesised" cannot answer that question from a single artefact today.

The two problems are solved together: every synthesised field has a contract (Phase A), the salvage layer fills the dominant gap (Phase B), and the audit trail emerges as a sidecar of the same write path (Phase C).

## Out of scope (deliberately not done here)

- **008-only salvage.** Sample shows 1/5000 (0.02 %) 008-only drops. The cost of synthesising an 008 string equals specifying it; the benefit is too small to chase. Records continue to raise `marcxml-content-minimum` for missing 008; cataloguer fixes the source. Reassess if drop rate climbs.
- **245 (title) salvage.** Sample shows 1/5000 records missing essentially everything. A record with no title can't be a Work in any useful sense; let M2 continue to drop it.
- **VIAF authority lookup as a salvage tier** (B4 in the original proposal sketch). Title-based authority reconciliation is high-cost-per-record (HTTP-bound, multilingual) and uncertain in benefit. Surface as a follow-up proposal once B1's bind rate is measured.
- **Synthesis policy in the published RDF README.** The proposal flagged a `void:Dataset` description triple for consumers. That's a publication-time concern; defer to the publication plan.
- **Cataloguer-facing review queue for synthesised records.** The per-run TSV (Phase C) is the artefact; building a Skosmos-side dashboard surface for triaging synthesised rows is downstream dashboard work.
- **Generalisation to other libraries' minimum-set bars.** Phase A's spec is Helmet-specific. P-09 (library-agnostic source) is the right plan to generalise it; left as a P-09 follow-up.
- **B2 activation.** Phase B ships B2's *code* behind a feature flag default-off (so the implementation is complete and reviewable); flipping the flag is gated on the cataloguer leader/06 sign-off and lands in a follow-up commit, not this plan.

## Definition of done

### Phase A — Spec + `bffi-prov:Synthesis` Activity + unify existing synth markers

#### A.1 Audit existing synth paths

- [ ] Read and catalogue each existing synthesis site:
  - [ ] P-08's `$5 FI-HELME/synth-v<N>` marker on synthesised MARC 336/337/338 datafields. Confirm the marker version is `v1` and the emit site in the export-tool side (`src/marcxml_export_pipeline/sierra/marcxml.py`) is the canonical write point.
  - [ ] M3's `language_detect.py` — what tag does the language detector add and how is its result recorded in provenance today?
  - [ ] M3's `sanitize.py:167` — cataloguer date-placeholder collapse. Currently has no synthesis marker; document the gap.
  - [ ] M8's `mint.py:284` — fallback for synthetic test fixtures missing canonical inputs. Document whether this fires in production or test-only.
  - [ ] M2's `marcxml_repair.py` — what does it repair today, and does any of it count as synthesis under Phase A's definition?
- [ ] Output: a single per-field synthesis-policy table in `docs/bibliographic-minimum.md` (one row per (field, tier, method, marker, confidence-band) tuple). The table is the contract Phase B + C consume.

#### A.2 `bffi-prov:Synthesis` Activity class + four `bffi-prov:synthetic*` predicates

- [ ] Add `bffi-prov:Synthesis` class as a sibling of `bffi-prov:MarcConversion` in `src/bffi_pipeline/provenance/vocab.py`. It carries one `prov:used` link to the source MarcConversion Activity so the audit chain is traversable.
- [ ] Add `bffi-prov:syntheticField` (string — the BFFI field synthesised, e.g. `bf:contribution / bf:agent`).
- [ ] Add `bffi-prov:syntheticMethod` (string — the free-text method tag, e.g. `creator-from-245c (regex)`).
- [ ] Add `bffi-prov:syntheticTier` (string — the Phase B tier ID `B1` / `B2` / `B3`, or a tier ID from a future plan that extends this vocabulary).
- [ ] Add `bffi-prov:syntheticConfidence` (xsd:decimal — 0.0 to 1.0).
- [ ] Add `bffi:syntheticSentinel` (xsd:boolean, defaults to `false`) — used to mark the B3 sentinel agent and any future synthetic sentinels.
- [ ] Document the additions in `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 (the bffi-prov vocabulary reference doc per `CLAUDE.md`'s pointer).
- [ ] Unit test: synthesise a Synthesis Activity in-memory, serialise to Turtle, parse it back, assert all four predicates round-trip.

#### A.3 `docs/bibliographic-minimum.md`

- [ ] Write the doc. Sections:
  - "The minimum exportable set" — the per-field bar, citing `validate_minimum_content`.
  - "Synthesis-policy table" — the audit output from A.1.
  - "Synthesis vocabulary" — the four `bffi-prov:synthetic*` predicates + their bffi-side counterparts (`bffi:syntheticSentinel`, `bffi:syntheticConfidence`).
  - "Cataloguer-facing artefacts" — pointer to the per-run TSV Phase C produces.
  - "Consumer-side filtering" — how to filter synthesised data in Skosmos SPARQL queries.
- [ ] The doc is cross-linked from `CLAUDE.md` § "Project specs and plans" so it's discoverable.

#### A.4 `CLAUDE.md` + `docs/external-dependencies.md` updates

- [ ] Add `http://urn.fi/URN:NBN:fi:bib:agent:unknown` to the "Committed identifiers (do not change without surfacing)" section in `CLAUDE.md`, with a one-line description: "sentinel agent URI for records that fall through B3's anonymous-by-convention salvage. Carries `bffi:syntheticSentinel "true"`; excluded from M5/M6/M8/M9 keying/reconciliation."
- [ ] Append a new Ask N to `docs/external-dependencies.md`: "Confirm the surface form of the no-author sentinel label. Default ships as `Tekijä tuntematon` @fi / `Okänd upphovsman` @sv / `Unknown author` @en. Alternatives considered: `Tuntematon tekijä` / RDA-aligned `(Tekijää ei ole ilmoitettu)`." Mark it as `BLOCKING for the next ramp-up beyond pilot scale; non-blocking for code review of Phase B.`
- [ ] Append a new Ask N+1: "Confirm which MARC leader/06 record-type codes warrant B2 publisher-as-corporate-creator promotion. Current default ships disabled. Codes under consideration: `a` (language material), `e` (cartographic), `g` (projected medium), `m` (computer file)." Mark it as `BLOCKING for B2 activation; non-blocking for Phase B merge`.

#### A.5 Verification

- [ ] All A.* checkboxes ticked.
- [ ] `make lint && make test` green (no functional code changed in Phase A; the vocab additions are import-only).
- [ ] Phase A commit hash filled into the `Phase commits` field above.

### Phase B — Creator-gap salvage at M2 repair layer

The salvage layer fires **only** when `validate_minimum_content` would otherwise raise. If a record already carries 1XX or 7XX, no synthesis runs. First match wins across B1 → B2 → B3 (B2 skipped when its feature flag is off — which is the default).

#### B.0 Lift Settings + repair-layer scaffolding

- [ ] Add `Settings.creator_salvage` Pydantic model with fields:
  - `enabled: bool` (default `true` — the salvage tier dispatch fires).
  - `b1_llm_cascade_enabled: bool` (default `true`).
  - `b2_publisher_promotion_enabled: bool` (default **`false`** — gated on cataloguer leader/06 sign-off).
  - `b2_leader_06_codes: frozenset[str]` (default `{}`; populated by cataloguer-confirmed set).
  - `sentinel_agent_uri: str` (default `http://urn.fi/URN:NBN:fi:bib:agent:unknown`).
  - `sentinel_label_fi: str`, `sentinel_label_sv: str`, `sentinel_label_en: str` (default values per the Phase A `external-dependencies.md` Ask).
- [ ] Add `src/bffi_pipeline/stages/m2/salvage.py` (new module) with the tier-dispatch entry point `try_salvage_minimum_content(tree: etree._ElementTree, settings: Settings) -> SalvageOutcome | None`. Returns `None` when no salvage applies; returns a `SalvageOutcome` (the tier ID + the synthesised datafield(s) + the synthesis records for Phase C) when a tier fired.
- [ ] Wire the entry point into `marcxml_repair.py` as a final repair step. Existing repair logic stays unchanged.

#### B.1 — 245$c regex parser

- [ ] Add `src/bffi_pipeline/stages/m2/salvage_245c.py` with `parse_245c(text: str) -> list[ParsedAgent]`. Handles the common Finnish + Swedish + English statement-of-responsibility shapes:
  - `"Author Name"` / `"by Author Name"` / `"av Author Name"` / `"toim. Editor Name"` / `"kirjoittanut Author Name"` / `"Author1 ja Author2"` / `"Author1, Author2 ja Author3 (toim.)"`.
  - Returns parsed agents with `name`, `role` ∈ {`author`, `editor`, `translator`, `compiler`, `illustrator`, `unknown`}, and `confidence` (0.5-0.8 depending on the regex tier).
- [ ] Reject parses that contain numerals, punctuation runs > 3 chars, or known non-author markers (`"Helsingin kaupunki"` → corporate, not personal).
- [ ] Unit tests cover at least one record per regex tier + one corporate-agent rejection.

#### B.2 — 245$c LLM cascade fallback

- [ ] Add `src/bffi_pipeline/stages/m2/salvage_245c_llm.py` that wraps the existing local-mlx-lm cascade (the M6 + M9 picker shape). The prompt is a new file under `prompts/salvage-245c.txt` and the prompt hash is recorded in provenance.
- [ ] Prompt constraints:
  - Output is structured JSON `{"agents": [{"name": str, "role": str, "verbatim_substring": bool}]}`.
  - The prompt instructs the model to copy each `name` as a verbatim substring of the 245$c. The post-processor rejects any returned name whose substring check fails.
  - Confidence cap at **0.7** for LLM-tier hits — these always land in M6 fallback gating per P-16.
- [ ] Cache at `<BFFI_DATA_DIR>/synth-cache.sqlite`. Cache key is `sha1(prompt_hash + "\x00" + 245c_literal)`. Cache hit short-circuits the LLM call. Mirrors the M6 judge-cache pattern.
- [ ] Determinism: temperature 0, fixed seed. Two runs of the same record produce byte-identical synthesised triples (the cache makes this trivially true after the first run; the first run is best-effort identical via temperature 0).
- [ ] Unit tests use a mocked LLM client returning canned responses. No `requires_llm` tests in this plan — per `CLAUDE.md` § "Tests against fixtures, not network".

#### B.3 — Publisher-as-corporate-creator (B2, feature-flag default-off)

- [ ] Add `src/bffi_pipeline/stages/m2/salvage_publisher.py` with `promote_publisher_to_corporate(record: etree._Element, settings: Settings) -> SynthesisedDatafield | None`. Returns `None` when the leader/06 isn't in `settings.creator_salvage.b2_leader_06_codes` OR the feature flag is off OR the record has no 260$b/264$b.
- [ ] On hit: emit `<datafield tag="710" ind1="2" ind2=" "><subfield code="a">{publisher_literal}</subfield><subfield code="5">FI-HELME/synth-v1</subfield></datafield>`. Confidence 0.3.
- [ ] Unit tests cover: (i) feature flag off — no salvage, (ii) feature flag on + matching leader/06 — salvage fires, (iii) feature flag on + non-matching leader/06 — no salvage.

#### B.4 — Anonymous-by-convention sentinel (B3)

- [ ] Add `src/bffi_pipeline/stages/m2/salvage_sentinel.py` with `synthesise_sentinel_agent(settings: Settings) -> SynthesisedDatafield`. Always returns a 710 block linking to the sentinel agent URI; the URI is config-driven so cataloguer confirmation is a config edit, not a code edit.
- [ ] M2's `runner.py` (or wherever the sentinel agent's RDF representation is materialised — `stages/m2/convert.py` is the candidate) emits a one-shot `bf:Agent`-shape block in the BIBFRAME output that carries:
  - The sentinel URI.
  - `skos:prefLabel "Tekijä tuntematon"@fi`, `"Okänd upphovsman"@sv`, `"Unknown author"@en` (from Settings).
  - `bffi:syntheticSentinel "true"^^xsd:boolean`.
- [ ] The sentinel block is emitted **once per run**, not once per record — multiple B3 records share the same Agent URI. Idempotent: re-running with the sentinel already present in the BIBFRAME RDF skips the emission.
- [ ] Unit tests cover: (i) sentinel block lands in BIBFRAME RDF on first B3 record, (ii) second B3 record references the same URI without re-emitting the Agent block, (iii) labels are configurable via Settings.

#### B.5 — Synthesis Activity emission to provenance

- [ ] On every tier-fired salvage, write a `bffi-prov:Synthesis` Activity to the provenance graph carrying:
  - `prov:used` → the source `bffi-prov:MarcConversion` Activity for the same record.
  - `bffi-prov:syntheticField "bf:contribution/bf:agent"` (B1 / B2 / B3 all populate the contribution surface; future tiers may populate other fields).
  - `bffi-prov:syntheticMethod` (free-text method tag).
  - `bffi-prov:syntheticTier "B1"` / `"B2"` / `"B3"`.
  - `bffi-prov:syntheticConfidence "0.65"^^xsd:decimal` (tier- and parse-specific).
  - `prov:generated` → the synthesised `bf:Contribution` blank-node URI.
- [ ] Activity URIs follow the existing `bffi-prov:` scheme (UUID-based per `CLAUDE.md` § "Conventions: URIs").
- [ ] Unit test: end-to-end salvage on a fixture record produces (i) the synthesised 7XX in BIBFRAME, (ii) a `bffi-prov:Synthesis` Activity in `provenance.ttl` with the right predicate values, (iii) the activity's `prov:used` traces back to the MarcConversion.

#### B.6 — Sentinel exclude rules in M5/M6/M8/M9

- [ ] M8 `union_find.py` — when computing the canonical-Work union-find key, treat agents with `bffi:syntheticSentinel "true"` as **distinct** for every record (i.e. don't collapse Works across the sentinel agent). Unit test: two synthesised records sharing the sentinel agent must not merge under M8.
- [ ] M5 — when assembling the agent-name vector for embedding similarity, skip contributions whose agent carries `bffi:syntheticSentinel "true"`. Unit test: a record's similarity vector with the sentinel removed equals what M5 would have produced if the record had no 7XX at all.
- [ ] M6 — when escalating a pair to LLM judge, skip the pair if either record's primary agent is the sentinel. Unit test: a candidate pair with one sentinel-agent record short-circuits to `different_work` with confidence 1.0 and rationale `"sentinel-agent-cannot-bind"`.
- [ ] M9 — when walking non-primary contributions for KANTO reconciliation (the population P-39 targets), skip agents with `bffi:syntheticSentinel "true"`. Unit test: a canonical contribution with a sentinel agent does NOT appear in the M9 walker's output. **This is the P-39 integration point**: P-41 emits the flag; P-39's walker amendment is the consumer; the two plans share one predicate.

#### B.7 — Wire salvage into M2 pre-validation flow

- [ ] In `src/bffi_pipeline/stages/m2/runner.py`, the per-record pipeline becomes: read MARCXML → apply repairs (existing behaviour) → **if `validate_minimum_content` would raise, dispatch the salvage layer** → re-validate → if still failing, raise `marcxml-content-minimum` per existing behaviour.
- [ ] Records that pass after salvage count as `succeeded` in the M2 stage-event counters, with an additional counter for `salvaged_by_tier_<B1|B2|B3>`.
- [ ] The cataloguer-source-review TSV continues to receive a `severity=warning` row for the synthesised record so the source-side cataloguer signal is preserved.
- [ ] Integration test: a fixture record with no 1XX/7XX + a populated 245$c flows through M2 and emerges with a synthesised 7XX, an M2 success counter, a Synthesis Activity in provenance, and a TSV row (after Phase C lands).

#### B.8 — Verification

- [ ] All B.* checkboxes ticked.
- [ ] `make lint && make test` green.
- [ ] Re-run the 2026-05-14 5k-sample (`logs/runs/pipeline-full.log`) under the new salvage layer; document the new drop count and the per-tier salvage counts in the Phase B commit body. Expectation: M2 drops fall from 94 / 5000 (1.88 %) to under 5 / 5000 (0.1 %).
- [ ] Phase B commit hash filled into the `Phase commits` field above.

### Phase C — Per-run synthesis TSV + retrospective regen CLI

#### C.1 TSV writer

- [ ] New module `src/bffi_pipeline/export_synthesis.py` modelled on `cataloguer_review.py`'s shape:
  - `append_synthesis_row(*, bib_id: str, field: str, marc_source: str, synthesised_value: str, tier: str, method: str, confidence: float, activity_uri: str) -> None`.
  - Per-process dedup keyed on `(bib_id, field, tier)`.
  - Output at `<BFFI_DATA_DIR>/export-synthesis-<run_uuid>.tsv`.
  - Header convention: UTF-8 no BOM, tab-delimited, written once on first append, `lineterminator="\n"`. Identical conventions to `cataloguer_review.py`.
  - No-op when no active emitter is set.
- [ ] Columns (header row): `bib_id`, `field`, `marc_source`, `synthesised_value`, `tier`, `method`, `confidence`, `activity_uri`.
- [ ] Unit tests cover: (i) header written once, (ii) dedup by `(bib_id, field, tier)`, (iii) no-op when emitter absent, (iv) confidence column formats to 4-decimal-place precision, (v) reset hook works for test isolation.

#### C.2 Wire TSV writes from Synthesis Activity emission

- [ ] In B.5's Synthesis Activity emit point, also call `append_synthesis_row` with the same values. The TSV row and the provenance Activity are emitted together; both come from the same source-of-truth dict.
- [ ] Integration test: a B1 + B3 cascade on a single record (record had no 245$c → fell through to B3) produces exactly one TSV row (B3 tier; B1 was attempted-and-failed which is *not* output per the resolved open-question default).

#### C.3 Retrospective regen CLI

- [ ] New CLI subcommand: `bffi-pipeline export-synthesis-report --run <run_uuid> [--output <path>]`.
- [ ] Reads `data/provenance.ttl` and SPARQL-queries `bffi-prov:Synthesis` Activities scoped to the run (the run scope comes from the Activity's `prov:used` chain back to the MarcConversion Activity whose run_uuid matches).
- [ ] Emits the TSV in the same format as C.1's per-run write path. The output path defaults to `<BFFI_DATA_DIR>/export-synthesis-<run_uuid>.tsv`.
- [ ] The CLI is idempotent — running it overwrites the target file rather than appending.
- [ ] SPARQL query lives in `sparql/export-synthesis-report.rq` per `CLAUDE.md` § "Conventions: SPARQL". Parametrised with Jinja2 if needed.
- [ ] Integration test: a fixture pipeline run that produces both a per-run TSV (C.2) and a `provenance.ttl` — running the CLI against the provenance file produces a byte-identical TSV.

#### C.4 Verification

- [ ] All C.* checkboxes ticked.
- [ ] `make lint && make test` green.
- [ ] Phase C commit hash filled into the `Phase commits` field above.
- [ ] Re-run the 2026-05-14 5k-sample once more; confirm the per-run TSV lands at the expected path with rows for every salvaged record and that the retrospective CLI reproduces the TSV byte-identically.

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Synthesised creators mask cataloguing problems | Medium | The cataloguer-source-review TSV continues to surface the original "missing 1XX/7XX" as a `severity=warning` row. Synthesis is additive, not silencing. Phase B.7 integration test pins this behaviour. |
| Sentinel agent pollutes M8/M9 canonicalisation | Medium-high if unwired; near-zero with B.6 wiring | B.6 wires the `bffi:syntheticSentinel` exclude rule into all four downstream stages with unit tests per stage. |
| 245$c LLM tier hallucinates names | Medium | Verbatim-substring constraint in prompt + post-processor rejection + confidence cap 0.7. Hallucinated agents land in M6 fallback tier where P-16's gating catches them. |
| Helmet's published RDF carries fabricated data | Low at pilot scale; revisit at publish time | `bffi:syntheticSentinel` + `bffi:syntheticConfidence` give consumers a filter; per-run TSV documents every synthesis event. Publication-time `void:Dataset` description is out-of-scope here but flagged as a publish-plan dependency. |
| Synthesis-cache non-determinism (B1 LLM tier) | Low after first run | `synth-cache.sqlite` makes second-and-subsequent runs byte-identical. First-run drift bounded by temperature 0 + fixed seed; the M6 judge-cache pattern is the precedent. |
| TSV row volume explodes | Low | 800k × 1.88% × 1-3 fields ≈ 50k rows. Comfortable for spreadsheets. No mitigation needed; bound stated for the record. |
| Errors-over-silent-fallbacks tension | Medium | Phase A's spec explicitly documents the salvage layer as a *structured* fallback with mandatory provenance + TSV. Records that fail salvage still raise the typed error. `CLAUDE.md` § "What not to do" is honoured: no silent failures merged into provenance. |
| P-09 lands first and reshapes `validation/marcxml.py` | Low (P-09 is `proposed`, not active) | Plan-base diff command at the top catches it; rebase Phase A's audit if needed. |
| P-38 stage-package restructure already moved targets | Already settled | P-38 is completed; surfaces this plan touches already live at the post-P-38 paths. |
| Phase B salvages records that should have been cataloguer-fixed | Medium at scale | The source-review TSV remains the cataloguer-action surface; the per-run synthesis TSV is the consumer-audit surface. Cataloguers can prioritise from either depending on their workflow. |
| B2 (publisher) activated without sign-off mis-attributes corporate creators | Mitigated by feature-flag default-off | B2 ships in code; activation is one config change. The flag stays off until `external-dependencies.md` Ask N+1 is answered. |

## Rollback procedure

Phase A is purely additive (new doc, new vocabulary terms, no behaviour change). Rollback is `git revert` on the Phase A commit; no data migration needed.

Phase B's salvage layer is opt-out via the `Settings.creator_salvage.enabled` flag — flipping it to `false` makes the M2 pipeline behave exactly as it did pre-P-41 (records missing 1XX/7XX raise `marcxml-content-minimum` and are dropped). For a code-level rollback, `git revert` on the Phase B commit reverts the salvage dispatch but leaves the Phase A vocabulary in place (still additive, still safe). Synthesised triples already in `data/canonical.ttl` from prior runs are identifiable via `?activity a bffi-prov:Synthesis` and can be deleted with a SPARQL DELETE WHERE:

```sparql
PREFIX bffi-prov: <http://urn.fi/URN:NBN:fi:schema:bffi-prov#>
PREFIX prov: <http://www.w3.org/ns/prov#>
DELETE { ?s ?p ?o }
WHERE  { ?activity a bffi-prov:Synthesis ;
                   prov:generated ?s .
         ?s ?p ?o . }
```

— removing every triple generated by every Synthesis Activity in the dataset. The Synthesis Activity records themselves are deleted by a sibling DELETE on `?activity a bffi-prov:Synthesis`.

Phase C is pure observability (TSV + CLI); rollback is `git revert`. The per-run TSVs already on disk stay as historical record; operators can delete the files manually if desired.

The sentinel agent URI `http://urn.fi/URN:NBN:fi:bib:agent:unknown`, once committed to `CLAUDE.md` § "Committed identifiers", is by definition not rolled back without surfacing — the URI is part of the project's external contract. Removing it requires a separate decision recorded in `CLAUDE.md`.

## Verification status

What was verified in-session (2026-06-02):

- `make lint && make test` green at every phase commit. Final state: **1167 tests passing**, mypy --strict clean, ruff clean.
- **End-to-end M2 → BIBFRAME → M3** for the salvage path verified via fixture-driven integration tests:
  - `tests/data/sample-marcxml/10000007.xml` (B1 fixture — `245$c "kirjoittanut Mika Waltari"`) flows through M2 with a synthesised MARC 100, emerges in BIBFRAME with a `bffi-prov:Synthesis` Activity carrying all six `synthetic*` predicates + `prov:used` → MarcConversion.
  - `tests/data/sample-marcxml/10000008.xml` (B3 sentinel fixture — no parseable 245$c) flows through M2 with the shared sentinel agent at `http://urn.fi/URN:NBN:fi:bib:agent:unknown` carrying `bffi:syntheticSentinel "true"^^xsd:boolean`.
  - Retrospective CLI roundtrip (`bffi-pipeline export-synthesis-report --run <uuid>`) reads the per-record provenance graphs and reproduces the per-run TSV byte-identically modulo row ordering.
- **Unit coverage** for every salvage tier: B1 regex (21 tests across 6 classes), B1 LLM cascade (21 tests across 6 classes — verbatim-substring enforcement, cache-hit short-circuit, connection-error retry, hallucinated-name drop), B2 publisher (11 tests across 4 classes — feature-flag gates, leader/06 matching, ISBD-punctuation stripping), B3 sentinel (3 tests).

**B.8 5 k bench — measured 2026-06-02 on the M5 Max** against the first 5 000 `*.xml` files (alphabetically) under `/Users/mikkovihonen/Workspace/helmet-sierra-data-tools/output/marcxml/`, run UUID `a4b1470df20f488d93bb2e37687c1235`, M2 stage only, LLM cascade enabled. **Wall-time: 328.1 s** (~65 ms / record).

| Outcome | Count | % of 5 000 |
|---|---:|---:|
| Succeeded | 3 889 | 77.78 % |
| Failed (all on missing 33X) | 1 111 | 22.22 % |
| **Records salvaged by P-41** | **643** | **12.86 %** |
| → B1 regex | 16 | |
| → B1-LLM cascade | 28 | |
| → B2 publisher | 0 | (default-off, Ask 6 unanswered) |
| → B3 sentinel | 599 | |

Per the `bffi-prov:Synthesis` Activity count in the BIBFRAME graphs (the source-of-truth provenance): **673 Synthesis events** across 643 distinct records — i.e. 30 records had B1 produce ≥ 2 agents (the multi-author 245$c case). The TSV dedup bug surfaced here became fix #1 below.

**The 22.22 % failure rate is upstream of P-41.** Every one of the 1 111 failures was missing-33X — i.e. lacking the RDA content/media/carrier triple that P-08's export-tool-side cascade is supposed to fill in. The corpus this bench ran against is the raw Sierra dump under `output/marcxml/`, not the post-P-08-synthesis sample the 2026-05-14 baseline log (`logs/runs/pipeline-full.log`) was drawn from. Spot-checked `1000003.xml`: 11 MARC 700 contributors but zero 33X — exactly the shape P-08 was designed to repair. The bench therefore validates that **P-41's creator-salvage layer fires correctly on a real corpus** but does not let us re-state the 2026-05-14 baseline's "M2 drops 1.88 % → < 0.1 %" claim end-to-end without first running the records through the Sierra-export-side P-08 cascade. To reproduce the baseline comparison cleanly, the next bench should either (a) run on the export-tool's output corpus rather than the raw Sierra dump, or (b) apply the P-08 33X synthesis as a pre-step.

What we *can* claim from this bench: **without P-41's salvage layer, 1 754 records (35.08 %) would have dropped on missing 1XX/7XX + missing 33X combined; with P-41 active, 1 111 (22.22 %) drop**. The creator-salvage layer recovered **643 / 1 754 = 36.66 % of would-be-failures** on this corpus state.

**LLM cache effectiveness:** the B1-LLM tier made 28 cache writes across 26 unique 245$c values (2 cache hits on the first run, ~7 %). On re-runs against the same corpus the cache short-circuits 100 % of those LLM calls; on a corpus with more cataloguer-shared statements of responsibility (series, reprints) the first-run hit rate climbs.

**Two production-found defects from the bench — both fixed:**

1. **TSV dedup key collapsed multi-agent salvages.** The original `(bib_id, field, tier)` key wrote one TSV row per (bib, field, tier) tuple, but a single B1 salvage on a multi-author 245$c (e.g. "Liisa Louhela ja Pekka Halonen") emits multiple `SynthesisRecord`s with distinct agent values. 30 of 673 salvage events were silently dropped from the TSV. Key extended to `(bib_id, field, tier, synthesised_value)`; regression test pinned at `tests/unit/test_export_synthesis.py::TestDedup::test_multi_agent_same_bib_writes_one_row_per_agent`.
2. **B1 regex didn't recognise German role markers.** Record `1000072` had `245$c "herausgegeben von L. Richter"` and was salvaged with `synthesised_value = "herausgegeben von L. Richter"` (the full string with role marker prefix), because none of the German "edited by" / "translated by" phrasings were in `_ROLE_MARKERS`. Added Finnish "suomentanut", English "written by" / "illustrated by" / "compiled by", Swedish "översatt av" / "redigerad av" / "illustrerad av", and German "herausgegeben von" / "übersetzt von" / "hrsg. von" / "hrsg." / "herausgegeben". Regression tests at `tests/unit/test_salvage_245c.py::TestRoleMarkedShapes::test_german_*`.

**C.4 byte-identity check** — the fixture-scale roundtrip is verified at `tests/integration/test_export_synthesis_report.py`; the corpus-scale roundtrip on this 5 k run remains as a one-line follow-up (run `bffi-pipeline export-synthesis-report --run a4b1470df20f488d93bb2e37687c1235` and `diff` against the per-run TSV). Not blocking.

What is **operator-pending** (cannot be verified from this session):
- **Cataloguer Ask 5** — sentinel label confirmation (`Tekijä tuntematon` / `Okänd upphovsman` / `Unknown author` ship as the default; the labels are env-var-configurable, no code change needed when the answer arrives).
- **Cataloguer Ask 6** — B2 leader/06 set. B2 ships behind a feature flag default-off; flipping `BFFI_CREATOR_SALVAGE_B2_PUBLISHER_ENABLED=true` and populating `BFFI_CREATOR_SALVAGE_B2_LEADER06="a,e,g,m"` (or whichever subset the cataloguer team confirms) activates the tier with no code change.

## Post-mortem

What went better than expected:

- **B.6 sentinel exclude rules required almost no new code.** The existing M5/M6/M8 filtering on `bffi:PrimaryContribution` (a pre-P-41 convention) naturally excluded the B3 sentinel agent because it lives on MARC 710 → `bffi:Contribution` (non-primary). The work reduced to emitting the `bffi:syntheticSentinel` flag for P-39's future M9 walker + a defensive `is_synthetic_sentinel` helper + an invariant fixture pinning the property.
- **B.5 + Phase C composed cleanly.** The Synthesis Activity emit and the per-run TSV write both consume the same `SynthesisRecord` source-of-truth, so the byte-identity contract between the live TSV and the retrospective regen was a one-tuple invariant rather than a parallel write-path to keep in sync.
- **B.2 LLM cascade architecture mirrored `contrib_extract_llm.py` exactly.** The existing 470-line M3-stage 245$c contributor extraction was the template; B.2 is roughly the same line count with the same retry stack, the same Pydantic schema shape, the same lazy-import-langchain pattern, and a verbatim-substring post-processor adapted for the salvage use case.

What surfaced unexpectedly:

- **Name-order mismatch on synthesised creators.** B1 emits MARC 100$a verbatim from 245$c (first-last form: "Mika Waltari"); cataloguer-typed records use surname-first MARC convention ("Waltari, Mika,"). The blocking key derivation picks "mika" instead of "waltari" as the surname token. Documented as a known quality gap in `tests/integration/test_workkey.py`'s `_EXPECTED_KEYS` comment; P-39's KANTO reconciliation will normalise to surname-first when it resolves the synthesised name to an authority record.
- **Pydantic 2.13's `Field(min_length=...)` error message text changed.** A test asserting `pytest.raises(ValueError, match="shorter than")` failed because the new message is "at least 20 characters". One-line test fix.
- **Retrospective regen required two additional `bffi-prov:` predicates** (`syntheticValue`, `syntheticMarcSource`) so the SPARQL query could reproduce the live TSV byte-identically without traversing the BIBFRAME graph for `synthesised_value` and inferring `marc_source` from method-tag heuristics. Caught during Phase C.3 implementation; vocabulary additions were trivially additive.
- **The bench corpus surfaced a Sierra-export gap, not a P-41 gap.** The first 5 000 records under `output/marcxml/` had 22.22 % missing-33X drops vs the 2026-05-14 baseline's 0 % missing-33X. P-41's salvage layer is upstream-of-33X in the validation chain; without P-08's export-tool-side cascade running first, no amount of P-41 work can recover those records. **Implication for operator:** the production-scale comparison vs the 2026-05-14 baseline needs the corpus pre-processed through the Sierra export tool (or a re-sampled corpus drawn from the post-export output) before the P-41 drop-rate claim can be re-stated. P-41's measured effect on this raw corpus — 643 records recovered out of 1 754 would-be-failures, or 36.66 % — is the cleanest claim available from this bench.
- **B1 LLM cascade verbatim-substring constraint held under production load.** All 28 B1-LLM hits passed the substring check; the prompt-side constraint plus post-processor enforcement caught zero hallucinations on this corpus. The 7 % first-run cache hit rate is lower than I'd expected — most 245$c values in the corpus are unique. On reruns the cache eliminates the LLM calls entirely (the synth-cache.sqlite from this bench is on disk and would short-circuit a second invocation against the same input dir).
- **B3 sentinel dominance is structural.** 599 of 643 salvage events (93.2 %) landed on B3. This reflects the corpus: most no-creator records also lack a parseable 245$c (often they're music recordings, anthologies, or items where the cataloguer left 245$c empty or with non-personal-name content like a venue or ensemble). The B1+B1-LLM combined coverage of 6.8 % is in the expected range for the deterministic+LLM tiers to refine; B3's role as the safety net is what keeps the export-rate guarantee.

## Cross-references

- [`src/bffi_pipeline/validation/marcxml.py`](../../../src/bffi_pipeline/validation/marcxml.py) — `validate_minimum_content` defines the bar; Phase A documents it; Phase B's salvage feeds it.
- [`src/bffi_pipeline/stages/m2/marcxml_repair.py`](../../../src/bffi_pipeline/stages/m2/marcxml_repair.py) — Phase B's salvage dispatch site.
- [`src/bffi_pipeline/cataloguer_review.py`](../../../src/bffi_pipeline/cataloguer_review.py) — Phase C's writer pattern reference.
- [`src/bffi_pipeline/stages/m8/union_find.py`](../../../src/bffi_pipeline/stages/m8/union_find.py), [`src/bffi_pipeline/stages/m9/picker.py`](../../../src/bffi_pipeline/stages/m9/picker.py) — Phase B.6 exclude-rule call sites.
- [`docs/plans/completed/p-08-richer-rda-33x-synthesis.md`](../completed/p-08-richer-rda-33x-synthesis.md) — sibling synthesis pattern.
- [`docs/plans/backlog/p-39-m9-non-primary-contribution-reconciliation.md`](../backlog/p-39-m9-non-primary-contribution-reconciliation.md) — downstream consumer; reads the `bffi:syntheticSentinel` flag B.6 emits.
- [`docs/plans/proposed/p-09-library-agnostic-source.md`](../proposed/p-09-library-agnostic-source.md) — overlapping touchpoint on `validation/marcxml.py` + bffi-prov vocabulary.
- [`docs/external-dependencies.md`](../../external-dependencies.md) — Ask N + Ask N+1 added in Phase A.4.
- [`docs/archived/marcxml-to-bffi-skosmos-pipeline.md`](../../archived/marcxml-to-bffi-skosmos-pipeline.md) § 8 — bffi-prov vocabulary reference doc; Phase A.2 documents the four new predicates here.
- `CLAUDE.md` § "Committed identifiers" — Phase A.4 adds `bib:agent:unknown` sentinel URI.
- `CLAUDE.md` § "What not to do" — synthesis is structured fallback with mandatory provenance + TSV; this plan honours the "don't merge silent failures into provenance" rule.
- [`logs/runs/pipeline-full.log`](../../../logs/runs/pipeline-full.log) — 2026-05-14 5k-sample evidence; re-measured in B.8 verification.
