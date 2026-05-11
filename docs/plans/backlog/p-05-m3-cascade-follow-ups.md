# P-05 — M3 cascade follow-ups: F1, F2, F3

**Status**: backlog.
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

- Phase F1 (M8 non-primary propagation): `<unfilled>`
- Phase F2 (transliteration sidecar + M9 binding): `<unfilled>`
- Phase F3 (M9 walks non-primary contributions): `<unfilled>`

**Owner**: TBD.
**Estimated wall-time**: 1.5-2 days for F1; 1-1.5 days for F2;
2-3 days for F3 (mostly bench + cataloguer-gating, not code).

## Goal

Three sequenced follow-ups to the M3 contributor-extraction cascade,
each gated on the previous one. Together they make cataloguer-
visible canonical Works show the contributions the cascade extracted
and bind the variant forms to their KANTO authorities.

| Phase | Touches | LOC | Bind / reconciliation effect at 800 k scale |
|---|---|---|---|
| **F1** — M8 propagates non-primary contributions onto canonical Expressions | M8 | ~150 | Pure plumbing; unblocks F2 and F3 by making cascade-emitted entities cataloguer-visible. |
| **F2** — Transliteration-variant binding (sidecar + M9 reader) | M3 sidecar emitter, M9 reader | ~400 | ~15 k–30 k variant pointers per cascade run; KANTO bind rate ~70-90 % on these. Highest leverage per cataloguer-hour saved (dedupes review queues). |
| **F3** — M9 walks non-primary contributions on canonical Expressions for general KANTO reconciliation | M9 | ~250 | ~40 k–75 k unique Finto API calls; bind rate ~30-50 % (KANTO is Finnish — Hogwood / Spector / etc. don't resolve); ~30 k–55 k records flagged `needs-review`. Wall time ~3-5 h sequential, ~1-2 h with M9 concurrency. ~40 MB extra triples. |

The order matters: without **F1**, the entities F2 and F3 reconcile
are only visible on per-bib raw Expression pages, not on the merged
canonical Works cataloguers actually browse. **F3 is pre-gated on
M12 gold-set validation** — without that, ~5-10 hours of compute
could amplify cascade misclassifications into thousands of bad
KANTO bindings.

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
- **F3**: Every blank-node agent on a non-primary canonical
  contribution either carries a `prov:specializationOf <kanto-uri>`
  triple (resolved) or a `bffi-prov:reconciliation-status
  "needs-review"` triple (couldn't resolve). M9's
  `_iter_extracted_contribution_requests` walker yields one
  EntityRequest per such blank node; the existing M9 cache + Finto
  rate-limiting kicks in.

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

- [ ] New `_propagate_extracted_contributions` exists and is called
      from `merge.run`.
- [ ] Dedup test covers same-contribution-from-two-records case.
- [ ] `canonical.ttl` byte-stability test passes across two M8 runs.
- [ ] Pre-existing M8 + M9 tests stay green.
- [ ] On a small (~500-record) test corpus, the number of
      canonical-level non-primary `bffi:Contribution` blocks is
      non-zero (smoke that the wiring works end-to-end).

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

## Phase F3 — M9 walks non-primary contributions on canonical

Estimated wall-time: ~2-3 days, most of it in the cataloguer-review
gate and the corpus-scale bench, not coding.

### F3.0. M12 gold-set gate (pre-flight)

**Do not start F3 until** `gold/contrib.jsonl` reaches 30-50
cataloguer-vetted cases (tracked in P-06 / M12). The scaffolding is
committed but the cataloguer extension is still external work.
Without that, F3 amplifies cascade misclassifications into
thousands of bad KANTO bindings at corpus scale.

Check: `wc -l gold/contrib.jsonl` ≥ 30.

### F3.1. Walker

In `reconcile.py`, add `_iter_extracted_contribution_requests` that
yields `EntityRequest(literal=<rdfs:label>, kind="person", ...)`
per blank-node agent on a non-primary canonical contribution.

### F3.2. Linker

Add `_link_extracted_agent` that mirrors the primary-agent linker:
`<blank-node-agent> prov:specializationOf <kanto-uri>` (same shape
as the bridge M9 already emits for raw agents). The existing M9
cache by `(kind, literal)` deduplicates Finto calls automatically.

### F3.3. Bench

- Run on the 500-record test slice from F1.4 first. Confirm:
  bind rate ≥ 30 % (KANTO Finnish coverage is the limiter),
  Finto API calls dedupe via cache, walltime scales linearly.
- Then run against the production canonical graph from v2.
  Expectation:
  - ~40 k-75 k unique Finto calls (cache-deduped).
  - Wall time ~3-5 h sequential; ~1-2 h with M9 concurrency.
  - Bind rate ~30-50 %.
  - ~30 k-55 k records get `needs-review` flagged.
  - +40 MB triples.

### F3.4. Tests + acceptance

- [ ] Walker yields the right shape on a synthetic fixture
      with one canonical non-primary contribution.
- [ ] Linker emits the binding triple OR the `needs-review` flag,
      deterministically per `(kind, literal)`.
- [ ] M9 bench on the 500-record slice confirms bind rate ≥ 30 %.
- [ ] Production-scale bench numbers match the expectations within
      a factor of 2 (otherwise investigate before declaring done).

### F3.5. Rollback

Revert the M9 walker + linker. The triples added by F3 carry a
distinctive predicate combination (blank-node-agent +
`prov:specializationOf` + KANTO URI), so a SPARQL DELETE WHERE on
that pattern removes the F3 contributions cleanly without affecting
F2's variant bindings.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| F1 dedup blank-node IDs leak non-determinism | Medium | The byte-stability test is the contract. CI runs M8 twice on the same fixture and asserts identical Turtle. |
| F2 sidecar grows unbounded across re-runs | Low | M3 cascade is idempotent per (record_id, c_subfield_form); sidecar emitter uses idempotent append (skip if (record, form) already present). |
| F3 cascade misclassifications get amplified | Medium-high | F3.0 gold-set gate. M12 gold-set growth (P-06) is the prerequisite. |
| Finto API rate-limiting trips during F3 production run | Low-medium | Existing M9 rate limiter + cache should handle it. If sustained 429s appear, drop M9 concurrency. |
| KANTO bind rate is much lower than projected (< 20 %) | Medium-low | F3.3 bench surfaces this before commit. If bind rate is too low, F3 still adds the `needs-review` flag (useful) but the projected cataloguer-hour saving evaporates. Document and ship anyway. |

## Open issues to close before / during execution

- The cascade emits Contributions on raw Expression URIs because
  it runs during M3. Is there value in moving the cascade *after*
  M8 so it operates directly on canonical Works? Probably yes —
  fewer duplicate emissions, no F1 propagation needed — but it's
  a bigger refactor than F1 + F2 + F3 combined. Out of scope
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
- `docs/marcxml-to-bffi-skosmos-pipeline.md` § 8 — provenance
  stage enum; F2 may require a new value.
- `gold/contrib.jsonl` — the cataloguer-vetted dataset gating F3.
- P-06 — gold-set growth that unblocks F3.0.
