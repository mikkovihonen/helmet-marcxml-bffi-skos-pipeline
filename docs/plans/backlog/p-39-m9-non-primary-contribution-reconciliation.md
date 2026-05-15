# P-39 — M9 reconciles non-primary contributions against KANTO

**Status**: backlog. Extracted from P-35 Phase F3 on 2026-05-15 because the corpus-scale wall-time (3-5 h sequential / 1-2 h with concurrency), the M12 gold-set pre-flight gate, and the cataloguer-review surface make this its own deliverable, not a sub-phase of P-35. P-35 stays as the M3 cascade follow-ups plan; F1 has shipped and F2 is the only remaining work there.

**Source-of-record**: P-35 Phase F3 content as committed at `eb74943`. Recoverable via `git show eb74943:docs/plans/in-progress/p-35-m3-cascade-follow-ups.md` if the F3 prose history needs to be traced back to its origin in the M3-cascade plan.

**Plan-base commit**: `eb74943`. Before executing, run
`git diff eb74943..HEAD -- src/bffi_pipeline/stages/reconcile.py src/bffi_pipeline/stages/merge.py src/bffi_pipeline/stages/bf_to_bffi.py gold/contrib.jsonl`
to confirm no in-flight work has reshaped the M9 surface this plan touches.

**Phase commits**:

- Phase A (M12 gold-set pre-flight gate verification): `<unfilled>`
- Phase B (walker + linker implementation): `<unfilled>`
- Phase C (corpus-scale bench): `<unfilled>`

**Owner**: TBD.

**Estimated wall-time**: 2-3 days total, most of it in the cataloguer-review gate (Phase A) and corpus-scale bench (Phase C), not coding.

- Phase A: cataloguer time, not engineering time. Gated on P-06 (gold-set growth) reaching ~30-50 contrib rows. Could be hours-of-engineering work to verify the gate is met; the cataloguer extension itself is external work.
- Phase B: ~half-day coding + tests. The walker mirrors `_iter_subject_requests`; the linker mirrors the primary-agent linker. ~250 LOC.
- Phase C: ~3-5 h sequential or ~1-2 h with M9 concurrency for the actual run; another half-day to validate the output and update the cataloguer-review surface.

**Sequencing prerequisites**:

- **P-35 Phase F2** (in P-35, in-progress). F2 binds transliteration variants to their canonical agents' reconciled KANTO URIs via a sidecar. F3's walker yields one EntityRequest per blank-node agent on a non-primary canonical contribution — F2's sidecar bindings have to be in place first because F2 covers the variant-of-canonical case and F3 covers the no-variant-pointer case; without F2 the boundary between the two is muddy.
- **P-06** (gold-set growth, backlog). `gold/contrib.jsonl` must reach 30-50 cataloguer-vetted cases. Without that, F3 amplifies cascade misclassifications into thousands of bad KANTO bindings at corpus scale.
- **P-38 Phase A** (if it lands first). P-38 renames `reconcile.py` → `m9/runner.py`. The Definition-of-Done bullets below reference `reconcile.py`; if P-38 Phase A lands first, update the path references in this plan in lockstep.

## Motivation

The M3 contributor-extraction cascade (committed and running as part of `bf-to-bffi`) emits `bffi:Contribution` blocks on raw Expression URIs for cataloguer-attributed contributors that didn't make it into MARC 100 — translators, editors, illustrators, second authors, etc. P-35 Phase F1 propagated these onto canonical Expressions via M8. P-35 Phase F2 binds transliteration variants to the canonical agent's KANTO URI when one exists. **Neither covers the general case: a cascade-emitted agent that has no MARC-form variant and whose canonical-agent equivalent hasn't reconciled against KANTO yet.**

The 5,000-record measurement documented in `docs/archived/BUILD_PLAN.md` projected ~40 k–75 k unique extracted names after dedup across the 800 k corpus. F2's transliteration sidecar is estimated to cover ~15 k–30 k of those (the variant-of-canonical case). The remaining ~25 k–60 k are the population F3 targets — names the cascade extracted, none of which currently have any KANTO binding.

**Reconciliation bind rate is the main uncertainty.** KANTO is Finnish; deceased non-Finnish contributors (Hogwood, Spector, etc.) won't resolve. Phase C's bench will tell us whether the bind rate is ~30 % (acceptable; ~7 k–18 k new bindings; the unbound ~70 % get a `needs-review` flag that's still useful for cataloguer triage) or much lower.

## Out of scope (deliberately not done here)

- **Moving the cascade to run after M8** (so it operates directly on canonical Works, eliminating F1's propagation step). This is a bigger refactor than F1 + F2 + F3 combined; surface as a separate proposal if F1 turns out to be more fragile than expected in the wild.
- **VIAF fallback for unbound names.** F3 is KANTO-only by default. Adding VIAF as a fallback adds another HTTP-bound step that materially extends wall-time. Sequence in a follow-up plan once F3's bind rate is measured and the trade-off is clear.
- **Cataloguer-facing review queue for `needs-review` records.** The triple-level flag is in scope; the operator-facing TSV / Skosmos surface for triaging those records is a separate dashboard / cataloguer-review work item.
- **Embedding-based pre-screening** of contrib labels against KANTO labels to predict no-bind cases before they hit Finto. Possible optimisation but adds an M5-shape dependency to M9; weigh after Phase C's measurement.

## Definition of done

### Phase A — M12 gold-set pre-flight gate verification

- [ ] `gold/contrib.jsonl` exists with ≥ 30 cataloguer-vetted rows. Verify with `wc -l gold/contrib.jsonl`.
- [ ] The gold rows are vetted by a cataloguer (not auto-generated). Spot-check with the gold-set's source attribution (whatever P-06 uses to record cataloguer review).
- [ ] If the gate is not met, **STOP** — do not proceed to Phase B. F3 without M12 amplifies cascade misclassifications into thousands of bad KANTO bindings at corpus scale.

### Phase B — Walker + linker implementation

#### B.1 Walker

- [ ] In `src/bffi_pipeline/stages/reconcile.py` (or `m9/runner.py` post-P-38-Phase-A), add `_iter_extracted_contribution_requests` that yields `EntityRequest(literal=<rdfs:label>, kind="person", ...)` per blank-node agent on a non-primary canonical contribution. Mirror the shape of `_iter_subject_requests` (the precedent already present in the file).
- [ ] Walker iterates the canonical graph (Phase F1 output) rather than per-bib raw output — non-primary contributions on canonical Expressions are exactly the population F3 targets.
- [ ] Walker skips contributions whose agent already carries a `prov:specializationOf <kanto-uri>` triple (F2's bindings — already reconciled; no point re-asking Finto).

#### B.2 Linker

- [ ] Add `_link_extracted_agent` mirroring the primary-agent linker (the existing `_link_primary_agent`-shape function in `reconcile.py`).
- [ ] On Finto hit: emit `<blank-node-agent> prov:specializationOf <kanto-uri>` (same shape as the bridge M9 already emits for raw agents).
- [ ] On Finto miss: emit `<blank-node-agent> bffi-prov:reconciliation-status "needs-review"`. The `bffi-prov:` namespace and the `reconciliation-status` predicate already exist per CLAUDE.md's "Committed identifiers" section; verify by inspection before relying on the predicate name (if absent, extend per the `bffi-prov:stage` enum precedent and document in this plan as a Material Update).
- [ ] Linker reuses the existing M9 cache by `(kind, literal)` — the cache already deduplicates Finto calls, so two records with the same extracted name produce one Finto call.

#### B.3 Tests

- [ ] **Walker shape:** synthetic fixture canonical graph with one non-primary contribution carrying a blank-node agent + `rdfs:label`; assert the walker yields exactly one `EntityRequest` with `kind="person"` and the right label.
- [ ] **Walker dedup:** synthetic fixture with two records' canonical Expressions both carrying the same `(label, role)` non-primary contribution; assert the walker yields one EntityRequest (the canonical graph's F1-propagated dedup already collapses duplicates, but verify).
- [ ] **Linker hit path:** mock the Finto client to return a KANTO URI; assert the canonical graph gains the `prov:specializationOf` triple wired to the blank node.
- [ ] **Linker miss path:** mock the Finto client to return no match; assert the canonical graph gains the `bffi-prov:reconciliation-status "needs-review"` triple.
- [ ] **F2-binding skip:** synthetic fixture where the agent already has a `prov:specializationOf` triple from F2; assert the walker skips it and the linker is never called for that blank node.
- [ ] **Cache integration:** two records with the same extracted name produce one Finto call in the mock. Use the existing M9 cache stats / counters to verify.

#### B.4 Determinism

- [ ] Two M9 runs on the same canonical graph produce byte-identical `reconcile.ttl` output (modulo timestamps in the provenance graph, which live in `provenance.ttl` and are out of scope here).

### Phase C — Corpus-scale bench

#### C.1 500-record slice

- [ ] Run on the 500-record test slice used by P-35 F1.4. Confirm:
  - [ ] Bind rate ≥ 30 % (the KANTO Finnish-coverage floor — sub-30 % is a red flag and warrants investigation before scaling up).
  - [ ] Finto API calls dedupe via the M9 cache (cache hit rate visible in the M9 cache stats).
  - [ ] Wall-time scales linearly with input size.

#### C.2 Production-scale bench

- [ ] Run against the production canonical graph from v2 (or whichever is current at execution time). Compare against expectations:
  - ~25 k–60 k unique Finto calls (cache-deduped from a larger pool of extracted-name occurrences).
  - Wall-time ~3-5 h sequential; ~1-2 h with M9 concurrency.
  - Bind rate ~30-50 % (KANTO Finnish-coverage).
  - ~15 k–35 k records get `needs-review` flagged.
  - +40 MB of triples on `reconcile.ttl`.
- [ ] Numbers within a factor of 2 of expectations → declare done.
- [ ] Numbers outside a factor of 2 → STOP, investigate before declaring done. Particular attention to bind rate < 20 % (Finto coverage assumption broken) or wall-time > 10 h (M9 cache or concurrency assumption broken).

#### C.3 Cataloguer-review surface

- [ ] The `needs-review` flagged records appear on the cataloguer-review TSV (per the existing P-31 surface). Verify ~15 k–35 k rows added with `severity` set appropriately.
- [ ] Spot-check 10 randomly-sampled `needs-review` rows. For each, the agent's label is a real cascade-extracted person name (not garbage) and Finto genuinely has no match (manually search Finto for the name).

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cascade misclassifications get amplified into bad KANTO bindings | Medium-high before F3.0 gate; low after | Phase A gate. M12 gold-set growth (P-06) is the structural prerequisite — without it, do not run F3 at corpus scale. |
| Finto API rate-limiting trips during F3 production run | Low-medium | Existing M9 rate limiter + cache handle it. If sustained 429s appear, drop M9 concurrency. P-19's throughput work already optimised this surface. |
| KANTO bind rate much lower than projected (< 20 %) | Medium-low | C.2 bench surfaces this. If too low, F3 still adds the `needs-review` flag (useful for cataloguer triage) but the projected cataloguer-hour saving evaporates. Document the actual bind rate in the Phase commit body. |
| `bffi-prov:reconciliation-status` predicate doesn't exist | Low | Verify by inspection during B.2 before relying on the name. If absent, extend the `bffi-prov:` namespace per the existing `bffi-prov:stage` enum precedent and document the extension as a Material Update. |
| F3 output collides with F2 sidecar bindings on the same blank node | Low | B.1's "skip if agent already has `prov:specializationOf`" guard makes the two layers strictly additive — F2 binds variants first, F3 walks the remainder. |
| P-38 Phase A renames `reconcile.py` while F3 is in flight | Low | Phase B coding work is the rebase target. The walker / linker function bodies relocate to `m9/runner.py` (or wherever P-38 lands them) with no signature change. Either land P-38 Phase A before F3 Phase B or coordinate the rebase in the same merge window. |

## Rollback procedure

F3's output triples carry a distinctive predicate combination — blank-node-agent subjects with `prov:specializationOf <kanto-uri>` OR `bffi-prov:reconciliation-status "needs-review"`, where the agent is on a non-primary canonical contribution. A SPARQL DELETE WHERE on that pattern removes F3's contributions cleanly without affecting F2's variant bindings (F2's `prov:specializationOf` triples have variant-blank-node subjects, not extracted-agent subjects — the dataset distinguishes them via the agent's role and the source contribution's primary-vs-non-primary classification).

The code revert is mechanical: `git revert` the Phase B commit. The walker and linker are additive — nothing else in M9 depends on their output. The existing primary-agent reconciliation behaviour stays unchanged.

The cataloguer-review TSV rows added in C.3 stay on disk (they're append-only artefacts of the run). Operators can either ignore them or run the SPARQL DELETE WHERE pattern against the per-run TSV to filter them out.

## Cross-references

- P-35 Phase F1 (M8 non-primary propagation, shipped at `464247e` + `b56d9c1`) — F3 depends on F1 having propagated the contributions onto canonical.
- P-35 Phase F2 (transliteration-variant sidecar + M9 binding, in P-35, in-progress) — F3 sits downstream of F2.
- P-06 (gold-set growth) — Phase A's gate.
- P-31 (cataloguer-review surface, completed) — C.3 verifies F3's output lands on this surface.
- P-38 (stage package restructure, backlog) — Phase A rename interacts with B.1's file reference.
- `docs/archived/marcxml-to-bffi-skosmos-pipeline.md` § 8 — `bffi-prov:` namespace and stage enum.
- `CLAUDE.md` "Committed identifiers" — `bffi-prov:` URI prefix.
