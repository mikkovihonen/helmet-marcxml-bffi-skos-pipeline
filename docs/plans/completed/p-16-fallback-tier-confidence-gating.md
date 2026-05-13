# P-16 — Stricter fallback-tier confidence gating to reduce common-name false-positive binds

**Status**: completed (2026-05-13).
**Source proposal**: `prop-16-fallback-tier-confidence-gating` (deleted on graduation; recover via `git show 6b62eea~3:docs/plans/proposed/prop-16-fallback-tier-confidence-gating.md`).
**Plan-base commit**: `02006b4`. To gauge drift before re-executing or backporting, run
`git diff 02006b4..HEAD --
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/config.py
src/bffi_pipeline/cli.py`.
**Phase commits**:

- Phase A (three Knobs A/B/C, plumbed through, default-off): `6b62eea` (code, 2026-05-13).

**Owner**: shipped this session.
**Estimated wall-time**: ~1-2 days. Actual: ~1 hour (Settings fields + kwargs threaded through 4 functions + 5 unit tests + lint cycles).

## Goal achieved

Add three independently-shippable operator knobs that gate when M9's tier-3 fallback (picker-uncertain → bind highest-lexical with `needs-review`) turns into a strict `no-candidate` outcome. Defaults preserve current behaviour; the knobs let the P-14 200-sample audit's verdict translate into a config flip rather than a code change.

The 2026-05-13 cataloguer-audit's one false-positive (`Williams, John` at conf 0.80 → wrong `finaf` URI) is the concrete motivation; the knobs are the policy levers that follow up audits can drive.

## Definition of done

- [x] `Settings.m9_lexical_fallback_floor` (default `0.70`, alias `BFFI_M9_LEXICAL_FALLBACK_FLOOR`) — global floor for tier-3 fallback; raising it forces sub-floor candidates to `no-candidate`.
- [x] `Settings.m9_lexical_fallback_floor_per_vocab` (default `{}`, alias `BFFI_M9_LEXICAL_FALLBACK_FLOOR_PER_VOCAB`, JSON-encoded env var) — per-source-vocabulary override of the global floor.
- [x] `Settings.m9_disable_fallback` (default `False`, alias `BFFI_M9_DISABLE_FALLBACK`) — hard-disable; any picker-uncertain outcome (including low-confidence `chose`) becomes `no-candidate`.
- [x] Kwargs threaded through `apply_reconciliation` → `_picker_phase_seq` / `_picker_phase_pool` → `_picker_call_with_budget` → `_decide_with_pick`, plus the cache-hit `_decide_with_pick` call inside `apply_reconciliation`'s Phase 1.5 lookup loop (warm-cache replays apply the same gating).
- [x] CLI populates from `Settings` at both `apply_reconciliation` call sites (with-provenance and without-provenance paths).
- [x] Unit tests pin all three knobs + backward-compatibility for the default case.
- [x] `make lint && make test` green (956 tests passing).

## Default behaviour stays unchanged

This plan ships the knobs without changing any defaults. The `Williams, John` false positive remains visible until either (a) the P-14 200-sample audit data motivates a default change, or (b) an operator opts in via the env vars on their next run. The pre-existing `needs-review` flag mitigation stays in place for the default path.

## Future work

- The P-14 Phase A 200-sample cataloguer audit will reveal whether `finaf` / `viaf` are systematically more namesake-prone than other vocabs, at which point Knob B's per-vocab default map may be worth populating (e.g. `{"finaf": 0.85, "viaf": 0.85}` as a shipped default rather than env-var opt-in).
- If a wider audit shows the tier-3 fallback's recovery value is low on this corpus, flip `m9_disable_fallback` to `True` by default. Reversible.
- Name-distinctiveness heuristics (use birth year as a tie-breaker via `$d` matching) are out of scope here; would belong in their own proposal if the audit motivates it.
