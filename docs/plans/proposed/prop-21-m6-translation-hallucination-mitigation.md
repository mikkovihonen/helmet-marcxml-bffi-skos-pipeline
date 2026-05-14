# P-21 — Curb M6 LLM hallucinated-translation false positives + 100-is-subject-not-creator misreads

**Status**: proposed.
**Scope**: Phase A (prompt hardening): half a day. Phase B (100-as-subject demotion): 1-2 days. Phase C (corroborating-signal validation): 1 day. Phases independently shippable.
**Proposal-base commit**: `3ae3da6`. To gauge drift before acting, run
`git diff 3ae3da6..HEAD --
prompts/picker_v1.txt
prompts/judge_v1.txt
src/bffi_pipeline/stages/judge.py
src/bffi_pipeline/stages/marc_to_bf.py
src/bffi_pipeline/stages/bf_to_bffi.py`.

## Motivation

The 2026-05-13 20 k overnight bench surfaced two distinct M5/M6 false-positive merges, both involving Alvar Aalto. The first (`b1499110x` ↔ `b18086238`) was an M5 auto-merge band failure that **prop-20** addresses. The second is the focus of *this* proposal: the M5 cascade correctly *escalated* a similarity-0.844 pair to M6, and **M6's LLM judge confidently said `same_work` at confidence 0.95** with a hallucinated rationale:

- **b23008490** — "Alvar Aalto : **taide ja moderni muoto**" — Ateneum 2017 exhibition catalog about Aalto's art, edited by Sointu Fritze, Finnish, multi-author.
- **b24731298** — "Alvar Aalto : **Maison Louis Carré**" — Fondation Aalto 2018 catalog about a specific building, by Laaksonen + Ólafsdóttir, French.

The two records share *nothing meaningful* — different subtitles, different authors (in 245$c), different publishers, different topics, different languages, different publication years. The only point of overlap is the MARC 100 primary author, set to `Aalto, Alvar, arkkitehti` on both. **Aalto is not the author of either book — he is the subject** (he died in 1976; the books are *about* his work).

M6's LLM picker wrote:

> "Both records share the same creator, 'Aalto, Alvar', and the same content_type 'sti'. The preferred_titles differ, but the original_language and expression_language fields indicate that Record B is a French Expression of the original Finnish work. **This suggests a translation, which is considered the same Work under RDA rules.**"

Two separate cognitive failures in one rationale:

1. **The "translation" claim is unsupported.** Title divergence + language difference does not imply translation. The LLM applied an RDA Work-Expression heuristic ("same creator + different language ⇒ translation") to data that doesn't justify it. Worse, the prompt structure (which exposes `original_language` and `expression_language` as comparison fields) actively *invited* the inference.

2. **The "same creator" claim is true at the MARC level but false at the bibliographic level.** Both records put Aalto in `100` because Helmet cataloguers use the field for the *subject* of art monographs. The actual authors are in `245$c` (statement of responsibility) and `700`. The pipeline trusts `100 = creator` literally, propagates that into `bffi:contribution`, and M6's prompt treats the creator match as evidence.

### Class of failure

**Aalto-as-creator in art/architecture monographs** is a *systemic* cataloguing convention in Helmet, not an isolated quirk. Every Finnish library cataloguer with a substantial art collection follows this pattern for deceased subjects. On the 800 k corpus there are likely **hundreds to low thousands** of similar pairs where the pipeline cluster-matches by Aalto / Sibelius / Mannerheim / Tove Jansson / etc. as "creator", then escalates to M6, then M6 hallucinates a Work relationship.

The Schildt case (prop-20) was a real same-author-different-book pair where the embedding vector itself was the failure. **The Aalto-as-subject case is different**: the source-data shape is misleading, AND the LLM's prompt template makes the misleading shape attractive to a "translation" reading. Two failure layers compounding.

## Approach

Three phases addressing different layers. Independently shippable; cumulative effect.

### Phase A — prompt hardening (~ half day)

Rewrite the relevant section of `prompts/picker_v1.txt` (and `judge_v1.txt` if it carries the same shape) to:

1. **Demote the translation inference**: "Title divergence — including translation of the subtitle into a different language — is by itself NOT sufficient evidence of a same-work translation relationship. Inferring translation requires explicit evidence: a `bf:translationOf` link, a `bf:Note` mentioning translation, or near-identical original-language titles (Levenshtein ≤ 5 on normalised forms)."
2. **Discount creator match for deceased persons**: "When the supplied creator is a deceased person AND the bib's publication year is after their death year, the creator match is likely a 100-as-subject Helmet cataloguing convention rather than authorship. Treat the creator-match signal as *neutral*, not as evidence for same_work."
3. **Add an explicit anti-example**: include the Aalto art-and-modern-form / Maison Louis Carré pair in the prompt's few-shot examples as a `different_work` case, with the rationale "same MARC-100 person but he is the subject of both books; different sub-titles point at different topics (art retrospective vs. specific building)."

Surface: ~20-40 lines in `prompts/picker_v1.txt`, ~5 lines in tests pinning the prompt-hash if it changes.

### Phase B — 100-as-subject detection at M2/M3

Detect the pattern at the conversion layer so it never reaches M6:

- At M2 / M3 time, when MARC 100 carries a person AND the bib's 008 publication year is after that person's known death year (from a small Aalto/Sibelius/Mannerheim/etc. lookup, or from cross-referencing KANTO's birth-death dates via the `$0` asteri-id), demote the person from `bffi:contribution` to `bffi:subject` (or to a new `bffi:subjectPerson` if we want to keep the distinction).
- The canonical-Work mint then doesn't see Aalto as the creator — it falls through to the actual authors in `700`, or fails to find a creator (→ `canonical-conflicts.jsonl` per prop-05's territory).

**Pros:**
- Architecturally correct: 100-is-subject becomes a permanent fact in BFFI, not just a M6 prompt-time judgment call.
- Composes cleanly with prop-15's authority-URI preservation: when `$0 = (FI-ASTERI-N)000068760` is on a 100 carrying a deceased person, the M3 pipeline can look up KANTO's death date and decide.

**Cons:**
- Needs a KANTO date-lookup mechanism. For records without `$0` on the 100, we'd need a small in-repo lookup table for the most common cases (Aalto, Sibelius, Mannerheim, Kalevala-era authors, etc.) — manageable but cataloguer-curated.
- Risk of false demotion: a deceased author's posthumously-published works. Mitigation: only demote when the publication year is > N years after death (say 5+), where N is large enough to clear posthumous-publication windows.

Surface: ~80-120 lines spread across the M2 post-process step, a new `data/deceased-authors.jsonl` lookup file (or a KANTO `bf:dateOfDeath` resolver), and the M3 SPARQL CONSTRUCT branching.

### Phase C — corroborating-signal validation at M6

For pairs where the LLM picker returns `same_work` but the title-distance (245$a + 245$b combined, normalised) exceeds a threshold (say Levenshtein > 15 chars), require an additional corroborating signal:

- `bf:translationOf` link present, OR
- Same `bf:isbn` (extremely rare to share across distinct works), OR
- Operator-listed signal from `picker_v1.txt`'s reply structure (the LLM has to cite a specific translation triple from the input)

If none of those are present, **downgrade `same_work` to `uncertain`** post-pick. Tier-3 fallback then either falls back to highest-lexical (still a needs-review bind) or — combined with prop-16 Knob A (raise `BFFI_M9_LEXICAL_FALLBACK_FLOOR`, or Knob C disable_fallback) — returns `no-candidate`.

**Pros:**
- Catches the residual hallucinated-translation case even if Phase A's prompt update doesn't fully suppress it.
- Treats LLM confidence as the suggestion it is, not as a ground truth — enforces structural validation.

**Cons:**
- More code surface (~50 lines in `judge.py` post-decision validation).
- The Levenshtein threshold needs tuning. Too aggressive demotes legitimate translation pairs.

## Recommendation

**Ship Phase A first** — it's a prompt change, the smallest surface, and addresses the specific cognitive failure (RDA-translation over-application) that fired on tonight's case. Re-bench the 20 k sample; verify the b23008490 ↔ b24731298 pair no longer auto-merges via M6.

**Phase B is the architecturally-correct fix** — flip Phase A from "prompt papering over the data shape" to "data shape no longer triggers the LLM in the first place". Worth the larger surface if the cataloguer-engagement (P-14 Phase A) can produce a deceased-persons + posthumous-publication-window lookup.

**Phase C as a safety net** — ship together with Phase A if there's appetite, since Phase A alone might not catch every prompt-imagination failure mode.

A + B together would address Aalto-class records permanently; C would also catch creator-different but title-prefix-similar legitimate-author cases the pipeline hasn't yet seen.

## Prerequisites

- The 2026-05-13 overnight-bench artefacts at `scratchpad/overnight-sample-2026-05-13/` provide the regression-test fixtures: the b23008490 ↔ b24731298 pair (this proposal's specific case) plus the b1499110x ↔ b18086238 pair (prop-20's case) as a pin for the prompt change.
- `prompts/picker_v1.txt` exists and is the active prompt template for M6's picker. Phase A's surgery is on that file; the prompt hash automatically propagates into provenance + cache keys.
- KANTO's bf:dateOfDeath lookup (Phase B) requires a quick check of the loaded Finto dumps — if the dump carries death dates, the lookup is local. If not, an external Finto API call is needed (the pipeline already talks to Finto for tier-0 reconciliation, so the infra is in place).
- prop-15 is already shipped (M3 preserves `madsrdf:isIdentifiedByAuthority`), so KANTO authority URIs flow through to canonical. Phase B can use them.

## Risks

- **R1 — Phase A is a prompt change**. Prompt regressions are notoriously hard to detect without a gold set. Mitigation: extend `gold/judge-gold.jsonl` with the Aalto pair (and similar known-different cases) before shipping; run `make eval` and confirm the regression test stays green for *known good* same_work pairs.
- **R2 — Phase B false demotion**. Posthumously-published works (e.g. Aalto's collected papers, edited and published after 1976) would lose Aalto as creator and be misrouted. Mitigation: tunable death-to-publication-year window (default 5 years); operator override via cataloguer-side mark; keep the original 100 as `bffi:subject` so the cataloguer view doesn't lose information.
- **R3 — Phase C threshold tuning**. Too aggressive — legitimate translations with rewritten subtitles get demoted. Too permissive — Aalto-case slips through. Levenshtein > 15 is a starting point; bench-tune from the 20 k sample's known cases.
- **R4 — Cache invalidation**. Phase A's prompt change rewrites the `prompts/picker_v1.txt` hash → invalidates the M6 judge cache + M9 reconcile cache (both key on prompt hash per P-10 Phase B + B.1). Mitigation: documented prerequisite (`make clean-caches`); next run re-pays the LLM cost. Acceptable, especially since the cache currently encodes the *buggy* LLM behaviour.

## Open questions

- Should Phase B's deceased-author detection look at MARC 100$d (birth/death date subfield) when present, rather than always going to KANTO? Pros: avoids the KANTO lookup. Cons: many records (including the Aalto cases tonight) don't carry $d. Probably do both — prefer $d when present, fall back to KANTO when not.
- Does Phase C compose with prop-16's fallback knobs? Yes — demoting `same_work` → `uncertain` is exactly what prop-16's `BFFI_M9_LEXICAL_FALLBACK_FLOOR` and `BFFI_M9_DISABLE_FALLBACK` were designed to handle downstream. They make the resulting bind either `no-candidate` (strict) or `fallback` with `needs-review` (moderate).
- Should the few-shot examples in `picker_v1.txt` also include a legitimate-translation case so the LLM doesn't over-correct in the opposite direction (rejecting actual translations)? Yes — a balanced 4-example set: one same_work translation, one same_work re-edition, one different_work series, one different_work subject-vs-creator. Worth lining up with the P-06 gold-set growth backlog.

## Acceptance criteria

### Phase A
- [ ] `prompts/picker_v1.txt` updated with the three additions (demote translation inference, discount creator-match for deceased-persons, add the b23008490/b24731298 anti-example).
- [ ] Re-run M6 on tonight's `embed-candidates.jsonl` (or the audit subset that includes the pair); assert the pair now returns `different_work` (or `uncertain`).
- [ ] No regression on `gold/judge-gold.jsonl` evaluation.

### Phase B
- [ ] M2 / M3 detect 100-is-subject pattern when `bib.publication_year - person.death_year > 5` AND demote the person from `bffi:contribution` to `bffi:subject` (or `bffi:subjectPerson`).
- [ ] Audit re-run: b23008490 and b24731298 no longer share an `aalto|alvar|sti` blocking key (one or both have Aalto as subject; their actual authors land in `bffi:contribution`).
- [ ] Spot-check ≥ 50 random "100-as-subject" candidates from the deceased-persons lookup; confirm the demotion is correct on all (or document the failures and tune the death-window).

### Phase C
- [ ] `judge.py` adds a post-pick validation step: title-Levenshtein > 15 + no `bf:translationOf` + no shared ISBN → downgrade `same_work` to `uncertain`.
- [ ] Unit test: synthetic fixture with the Aalto pair's text → assert downgrade fires.
- [ ] Re-bench: no regression on legitimate translation pairs (pin via gold set).

## What this proposal does NOT do

- Doesn't replace prop-20's M5 auto-merge mitigations. The two proposals compose: prop-20 catches the high-similarity cases at M5, this one catches the M6-LLM-hallucination cases that escape M5.
- Doesn't redesign the M6 cascade (single → fallback model) or the picker's I/O shape. Phase A is a prompt update only; Phase B is at the M2/M3 layer; Phase C is a post-pick validation, not a pre-pick reshape.
- Doesn't address the broader "MARC cataloguing conventions vary by library" issue. This proposal targets the specific Helmet 100-as-subject pattern; library-agnostic generalisation belongs in P-09 (library-agnostic source).
- Doesn't add a cataloguer-side annotation workflow for "this 100 is actually a subject". That UX work, if it ever happens, would be a P-06 gold-set-growth follow-up.
