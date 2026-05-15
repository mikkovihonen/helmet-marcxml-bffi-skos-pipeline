# P-35 — M3 cascade follow-ups: F1, F2

**Status**: in-progress (Phase F1 shipped pre-renumber; F2 remaining). **Phase F3 extracted to its own plan on 2026-05-15** — see [`../backlog/p-39-m9-non-primary-contribution-reconciliation.md`](../backlog/p-39-m9-non-primary-contribution-reconciliation.md). Reason: F3's corpus-scale wall-time (3-5 h), M12 gold-set pre-flight gate (P-06), and cataloguer-review surface make it its own deliverable, not a sub-phase of M3 cascade follow-ups.

**Renumbered from P-05 on 2026-05-14** to clear a number collision
with the (now-abandoned)
`proposed/p-05-anonymous-work-canonicalisation.md`, which the
2026-05-14 prefix-unification convention exposed. The two P-05s
arose because this plan predates the unified prefix and was sitting
in `backlog/p-05-...` while a separate proposal lived in
`proposed/p-05-...`. Content is unchanged; only the number + folder
moved. `git log --follow` traces the prior numbering. Mentions of
"P-05" in older commits / archived docs (notably
`docs/archived/BUILD_PLAN.md` L253) refer to this plan; live docs
were updated in the renumber commit.

**Source**: `docs/archived/BUILD_PLAN.md` M3 unfinished item at L253 (the
"M3 cascade follow-ups, in dependency order" block). Not graduated
from a proposal — these are committed M3 follow-up work items that
deserve their own plan because they touch M8 / M9 boundaries and
because corpus-scale go/no-go is gated on M12 gold-set validation.
**Plan-base commit**: `fe0b8dd`. To gauge drift before executing,
run
`git diff fe0b8dd..HEAD -- src/bffi_pipeline/stages/bf_to_bffi.py
src/bffi_pipeline/stages/merge.py src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/contrib_extract_llm.py`.
**Phase commits**:

- Phase F1 (M8 non-primary propagation): `464247e` (initial —
  propagate non-primary `bffi:Contribution` blocks onto canonical
  Expressions), `b56d9c1` (follow-up — propagate role through to
  canonical too). Verified live at
  `stages/merge.py:_emit_canonical_work` (the
  `expression_contributions` iteration block at ~line 1012) and
  exercised by P-34 Phase A's editor-anchored recovery on the
  2026-05-14 helmet-5k bench. The plan body's F1 acceptance
  checklist (below) is met as of these commits; the documentation
  rot was caught while folding `proposed/P-05` into P-34 and
  verifying P-34 Phase A's interaction with non-primary
  contributions.
- Phase F2 (transliteration sidecar + M9 binding): `<unfilled>`.
- ~~Phase F3 (M9 walks non-primary contributions)~~ — extracted to [`P-39`](../backlog/p-39-m9-non-primary-contribution-reconciliation.md).

**Owner**: TBD.
**Estimated wall-time**: F1 was ~half-day actual; 1-1.5 days for F2. (F3's 2-3 days are tracked separately under P-39.)

## Goal

Two sequenced follow-ups to the M3 contributor-extraction cascade. Together they make cataloguer-visible canonical Works show the contributions the cascade extracted (F1) and bind the variant forms to their KANTO authorities (F2). General KANTO reconciliation for the rest of the extracted-name population is tracked separately under [P-39](../backlog/p-39-m9-non-primary-contribution-reconciliation.md), because its corpus-scale bench, M12 gold-set pre-flight gate, and cataloguer-review surface make it its own deliverable.

| Phase | Touches | LOC | Bind / reconciliation effect at 800 k scale |
|---|---|---|---|
| **F1** — M8 propagates non-primary contributions onto canonical Expressions | M8 | ~150 | Pure plumbing; unblocks F2 (and P-39) by making cascade-emitted entities cataloguer-visible. |
| **F2** — Transliteration-variant binding (sidecar + M9 reader) | M3 sidecar emitter, M9 reader | ~400 | ~15 k–30 k variant pointers per cascade run; KANTO bind rate ~70-90 % on these. Highest leverage per cataloguer-hour saved (dedupes review queues). |

The order matters: without **F1**, the entities F2 reconciles are only visible on per-bib raw Expression pages, not on the merged canonical Works cataloguers actually browse.

## Definition of done

- **F1**: Every cascade-emitted non-primary `bffi:Contribution` on a
  raw `bffi:Expression` has a structurally-equivalent twin on the
  canonical `bffi:Expression` produced by M8. Canonical-graph
  Turtle is byte-stable across re-runs (deterministic blank-node
  IDs).
- **F2**: `<BFFI_DATA_DIR>/contrib-variants.jsonl` exists with one
  row per cascade-resolved transliteration variant; M9 emits
  `<variant-bnode> prov:specializationOf <kanto-uri>` for each row
  where the canonical agent's reconciliation found a KANTO URI.
(Phase F3's Definition of Done lives in [P-39](../backlog/p-39-m9-non-primary-contribution-reconciliation.md) now.)

## Current state

- M3 cascade (contributor extraction via local LLM) is committed
  and runs as part of `bf-to-bffi`. It emits new `bffi:Contribution`
  blocks on raw Expression URIs.
- M8 already propagates *primary* contributions / prefLabels /
  identifiers / subjects / genre-forms onto canonical via
  `_propagate_primary_contributions` and siblings.
- M9 reconciles primary agents against KANTO / VIAF; non-primary
  cascade agents are currently invisible to M9.
- The 5,000-record heuristic measurement that produced the volume
  estimates is documented in M3's checklist (~73 k–88 k records
  emit new Contributions; ~40 k–75 k unique extracted names after
  dedup).

---

## Phase F1 — M8 propagates non-primary contributions onto canonical

Estimated wall-time: ~1.5-2 days including tests and a byte-
stability check.

### F1.1. Mirror `_propagate_primary_contributions` for non-primaries

Find `_propagate_primary_contributions` in
`src/bffi_pipeline/stages/merge.py`. Add `_propagate_extracted_contributions`
that walks every `bffi:Contribution` on raw `bffi:Expression` URIs
where the contribution is **not** primary (no
`bffi:role <http://id.loc.gov/vocabulary/relators/aut>` and not the
record's MARC 100 source) and re-emits it on the canonical
Expression.

Dedup key: `(canonical_expr_uri, agent_label, role_uri)`. Two cascade
runs against the same record must produce byte-identical canonical
Turtle — emit blank-node IDs deterministically as a hash of the
dedup key.

### F1.2. Tests

- Two-record fixture where each record has the same extracted
  non-primary contribution. Assert the canonical Expression carries
  exactly one Contribution block (dedup correct).
- Byte-stability: convert the same fixture twice; assert
  `canonical.ttl` is byte-identical.
- Regression: existing M8 tests must still pass (primary propagation
  unchanged).

### F1.3. Acceptance

- [x] New `_propagate_extracted_contributions` exists and is called
      from `merge.run`. — landed as `_propagate_non_primary_contributions`
      in `stages/merge.py::_emit_canonical_work`'s
      `expression_contributions` block (~line 1012), commits `464247e`
      + `b56d9c1`.
- [x] Dedup test covers same-contribution-from-two-records case.
- [x] `canonical.ttl` byte-stability test passes across two M8 runs.
- [x] Pre-existing M8 + M9 tests stay green.
- [x] On a small (~500-record) test corpus, the number of
      canonical-level non-primary `bffi:Contribution` blocks is
      non-zero — verified on the 2026-05-14 helmet-5k bench while
      validating P-34 Phase A's editor-anchored recovery (P-34 reads
      F1's propagated non-primary contributions to find the
      lex-min non-translator agent).

### F1.4. Rollback

Revert the merge.py changes. M9's primary-only reconciliation
behavior is unchanged because F1 doesn't touch reconcile.py.

---

## Phase F2 — Transliteration-variant sidecar + M9 binding

Estimated wall-time: ~1-1.5 days. Depends on F1 having shipped.

### F2.1. Sidecar emitter in M3

The M3 cascade currently discards `transliteration_of` pointers
after post-process. Add a step that persists them to
`<BFFI_DATA_DIR>/contrib-variants.jsonl` (one row per resolved
variant):

```json
{"record_id": "1234567", "c_subfield_form": "Tsaikovskij",
 "canonical_label": "Чайковский, Пётр Ильич",
 "decided_at": "2026-MM-DDTHH:MM:SSZ"}
```

Format follows the existing `helmet-map.jsonl` / `embed-candidates.jsonl`
conventions (one JSON object per line). Schema is local to the
sidecar — no new ontology terms.

### F2.2. M9 reader

In `src/bffi_pipeline/stages/reconcile.py`, add a step that runs
after the primary-agent reconciliation pass: load
`contrib-variants.jsonl`, for each row find the canonical agent's
reconciled KANTO URI on the same canonical Work, emit
`<variant-blank-node> prov:specializationOf <kanto-uri>`.

The canonical agent's URI is already resolved (primary pass
already ran), so this is zero extra Finto traffic — pure local
join.

### F2.3. Tests

- Sidecar emission: fixture cascade output with two transliteration
  variants; assert the JSONL has exactly two rows with the right
  fields.
- M9 reader: synthesised graph where the canonical agent has a
  reconciled KANTO URI, the sidecar references that agent's
  variant blank-node; assert the M9 output has the
  `prov:specializationOf` triple wired up.
- Negative: variant whose canonical agent didn't reconcile —
  assert M9 silently skips (the variant stays unbound, no error).

### F2.4. Acceptance

- [ ] Sidecar emitter writes well-formed JSONL during M3 cascade.
- [ ] M9 reader emits the variant-binding triples deterministically.
- [ ] No extra Finto API calls vs the baseline (verifiable via the
      M9 cache stats).
- [ ] Cataloguer-spot-check: pick 10 variant bindings, manually
      verify both forms point at the same KANTO authority.

### F2.5. Rollback

Sidecar is additive — deleting the file disables the feature without
breaking M9. Revert reconcile.py's reader if needed.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| F1 dedup blank-node IDs leak non-determinism | Medium | The byte-stability test is the contract. CI runs M8 twice on the same fixture and asserts identical Turtle. |
| F2 sidecar grows unbounded across re-runs | Low | M3 cascade is idempotent per (record_id, c_subfield_form); sidecar emitter uses idempotent append (skip if (record, form) already present). |

## Open issues to close before / during execution

- The cascade emits Contributions on raw Expression URIs because
  it runs during M3. Is there value in moving the cascade *after*
  M8 so it operates directly on canonical Works? Probably yes —
  fewer duplicate emissions, no F1 propagation needed — but it's
  a bigger refactor than F1 + F2 + P-39 combined. Out of scope
  here; record as a future proposal if F1 turns out to be more
  fragile than expected.
- Should the F2 sidecar live under `data/` (re-run safe) or
  `<BFFI_DATA_DIR>/` (alongside other M3 artefacts)? Decision:
  `<BFFI_DATA_DIR>/` to keep all M3 outputs co-located.
- Provenance question: should F2's `prov:specializationOf` triples
  be tagged with a stage value to distinguish them from primary
  reconciliation? Yes — `bffi-prov:stage = "reconciliation-variant"`
  per the spec § 8 enum (extend if not already there).

## Cross-references

- `docs/archived/BUILD_PLAN.md` M3 — origin checklist item.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 — provenance
  stage enum (archived spec); F2 may require a new value, and the live
  enum reference lives in `CLAUDE.md` § "Committed identifiers".
- [P-39](../backlog/p-39-m9-non-primary-contribution-reconciliation.md) — extracted from this plan's former Phase F3; tracks general KANTO reconciliation for cascade-extracted non-primary contributions.
