# P-16 — Stricter fallback-tier confidence gating to reduce common-name false-positive binds

**Status**: proposed.
**Scope**: 1-2 days (config flag + maybe a per-vocabulary floor + bench A/B). Larger if cataloguer audit signals a richer policy.
**Proposal-base commit**: `1082d32`. To gauge drift before acting, run
`git diff 1082d32..HEAD --
src/bffi_pipeline/stages/reconcile.py
src/bffi_pipeline/config.py`.

## Motivation

The 2026-05-13 cataloguer-feedback audit ([`docs/performance/2026-05-13-cataloguer-feedback-audit.md`](../../performance/2026-05-13-cataloguer-feedback-audit.md)) flagged one false-positive bind:

- `b23481833`, contributor `"Williams, John"`.
- Cataloguer's verdict: **not in KANTO**, M9 should return `no-candidate`.
- M9's verdict: tier-3 **fallback** bind to `finaf:000088832` at confidence **0.80**, with `needs-review` flag set.

`finaf:000088832` is a *different* John Williams (a namesake). M9's design says: when the LLM picker says `uncertain` or returns confidence below `LLM_CONFIDENCE_THRESHOLD` (`0.80` in `reconcile.py`), and the highest-lexical candidate is above `LEXICAL_FLOOR` (`0.70`), bind to the highest-lexical candidate and tag `needs-review`. That contract is working: the bind exists, the flag is set, downstream tooling can filter on `bffi-prov:stage = "reconciliation-fallback"` to surface it for cataloguer review.

But the cataloguer expected `no-candidate`. The flag's mitigation power depends on a downstream consumer that filters on it (Skosmos doesn't today; cataloguer-facing reports don't yet exist). Until those downstream consumers ship, fallback binds visibly clutter the published graph and require cataloguers to disprove them one-by-one. The trade-off:

- **Pro-fallback (status quo)**: M9 recovers from lexical-similar candidates the LLM happens to time-out or hedge on. Catches misspellings, alternate transliterations, slightly-stale name forms. Confidence column tells the operator what to trust.
- **Anti-fallback (this proposal)**: For common names with many namesakes (Williams, John; Smith, John; Karjalainen, Matti — Finnish corpus has plenty), the highest-lexical candidate is often the *wrong* person at high enough lexical similarity to pass the floor. Cataloguers see a publishing-quality binding that turns out to be wrong, and must investigate before publishing.

This proposal is a knob-design question: **what knobs should the operator have to gate the fallback tier more strictly?** The audit's single false positive isn't enough evidence to mandate a default change, but it is enough to motivate adding the knobs so the next audit's verdict translates into a config flip rather than a code change.

## Approach

Three independently-shippable knobs, ordered from least-disruptive to most-disruptive.

### Knob A — `BFFI_M9_LEXICAL_FALLBACK_FLOOR` (per-stage override)

Currently `LEXICAL_FLOOR = 0.70` is hard-coded in `src/bffi_pipeline/stages/reconcile.py` and controls *both* (a) the tier-1 lexical-direct branch's lower bound and (b) the tier-3 fallback's lower bound. The audit's b23481833 case fell at lexical sim 0.80 — above both floors.

Split the floor into two settings:
- `LEXICAL_DIRECT_FLOOR` = 0.70 (unchanged) — tier-1 short-circuit threshold.
- `LEXICAL_FALLBACK_FLOOR` = 0.70 (default, unchanged) — tier-3 minimum, configurable via `BFFI_M9_LEXICAL_FALLBACK_FLOOR`.

Operator can raise `BFFI_M9_LEXICAL_FALLBACK_FLOOR` to e.g. 0.85 to force `Williams, John`-style sub-0.85 matches into `no-candidate`. No effect on tier-1 (which already requires near-perfect lexical at 0.95 via `LEXICAL_DIRECT_THRESHOLD`).

Surface: ~5 lines in `config.py` + ~3 lines in `reconcile.py` referencing the new setting. Backward-compatible: the default is the current behaviour.

### Knob B — Per-vocabulary floor

The `Williams, John` case is specifically a `finaf` (KANTO) match. The same lexical similarity threshold makes sense for a small distinctive vocabulary like `kaunokki` (fiction subject terms) but is too loose for a personal-names authority with thousands of namesakes.

Add a per-source-vocabulary floor map in `config.py`:

```python
m9_lexical_fallback_floor_per_vocab: dict[str, float] = Field(
    default_factory=lambda: {
        "finaf": 0.85,  # KANTO — namesake-rich; require stronger lexical match
        "viaf": 0.85,   # VIAF — same reason
        # yso / kauno / slm / muso use the global floor (no entry → fall through)
    },
    alias="BFFI_M9_LEXICAL_FALLBACK_FLOOR_PER_VOCAB",
)
```

Implementation: in `_pick_highest_lexical_with_fallback` (or wherever the fallback floor is checked), look up the candidate's `source_vocabulary` and use the per-vocab floor if present, else the global floor from Knob A.

Surface: ~30 lines including parse of the env var (JSON-encoded dict) + per-vocab lookup. The env var format: `BFFI_M9_LEXICAL_FALLBACK_FLOOR_PER_VOCAB='{"finaf":0.85,"viaf":0.85}'`.

### Knob C — Disable fallback entirely (`BFFI_M9_DISABLE_FALLBACK`)

The strictest setting: when the LLM picker returns uncertain or low-confidence, default to `no-candidate` instead of binding the highest-lexical candidate. Cataloguers see nothing where the pipeline would have produced a needs-review bind; they're free to add the bind manually if the LLM happened to be wrong about uncertainty.

Trade-off: catches genuine misspellings and alternate forms (`Dostojevski, Fedor` → `Dostoevsky, Fyodor`) less well. The 2026-05-13 audit had two records (b23591146 Lorca, b26163743 Dostojevski) where the top lexical candidate was *correct* but at sub-floor similarity (0.58 and 0.50); fallback didn't fire on those because they were below the floor already, so Knob C wouldn't regress them.

Surface: ~10 lines — a single boolean flag, when set, replaces the fallback outcome with `no-candidate` and emits `bffi-prov:rationale "BFFI_M9_DISABLE_FALLBACK was set"` for traceability.

### Order of consideration

Recommendation: ship Knob A first (smallest surface, defensible default change later). Then evaluate Knob B and Knob C only if a larger audit (the P-14 200-sample) confirms `finaf` / `viaf` are systematically more namesake-prone than other vocabs.

## Prerequisites

- The audit fixture `b23481833` (Williams, John → finaf:000088832 at conf 0.80) becomes the regression test for Knob A.
- The P-14 Phase A 200-sample audit's `verdict: "bind_incorrect"` column populates the input for any policy decision about whether `0.85` is the right global floor or whether per-vocabulary floors are needed.

## Risks

- **R1 — Knob C disables a working recovery path.** The cataloguer-fix-cases `b22057407` (Hirvisaari, Laila — old name form) bound successfully via the LLM picker (not fallback) — so Knob C doesn't regress it. The regression surface for Knob C is the LLM-uncertain branch, which would now produce `no-candidate` instead of `reconciliation-fallback`. Without a larger audit it's not clear how much real signal lives in that branch.
- **R2 — Per-vocab floors invite bikeshedding.** Once one vocab gets its own floor, every vocab will want one. Mitigation: only ship Knob B if Knob A's evidence on a wider audit shows the global floor doesn't fit `finaf` / `viaf`. Keep the default map small (2 entries) and resist expansion without evidence.
- **R3 — Cataloguer-visible behaviour change.** Raising the floor produces more `no-candidate` outcomes, which Skosmos renders as no bind at all. Cataloguers may have to add binds manually for cases the pipeline would have caught at the lower floor. Mitigation: document the trade-off; let the operator pick the floor.

## Open questions

- What's the right default floor for `finaf` / `viaf` once the 200-sample audit lands? Without that data, any number is a guess. This proposal commits to the knob existing, not to a specific default.
- Does the picker's *confidence* signal correlate with bind correctness on the namesake-rich vocabs, or is the lexical floor the only useful gate? If confidence ≥ 0.95 binds are reliable even on `finaf`, a per-vocab confidence floor (rather than a lexical floor) may be more useful. Worth measuring on the 200-sample audit.
- How does this interact with the picker cache (P-10 Phase B/B.1)? Cached `fallback` decisions still carry the bind URI. After raising the floor, cached decisions need to be invalidated for entities whose lexical similarity was between the old and new floors. Worst case: `make clean-caches` after a floor change. Acceptable.

## Acceptance criteria

- [ ] `BFFI_M9_LEXICAL_FALLBACK_FLOOR` env var wired through `Settings`; default `0.70` (unchanged behaviour).
- [ ] `b23481833` regression test: with `BFFI_M9_LEXICAL_FALLBACK_FLOOR=0.85`, `Williams, John` produces `no-candidate` instead of fallback bind. With the default `0.70`, it still produces the fallback bind (pinning backward compatibility).
- [ ] `make lint && make test` green.
- [ ] If/when Knobs B and C ship: their own unit tests + env-var wiring + back-compat defaults.

## What this proposal does NOT do

- Doesn't change the default `LEXICAL_FLOOR` value. That's gated on the 200-sample audit (P-14 Phase A).
- Doesn't address namesake disambiguation directly (e.g. "use birth year as a tie-breaker" — `$d` matching). That's a separate, larger surface and would belong in its own proposal if the audit motivates it.
- Doesn't redesign the LLM picker prompt or confidence calibration. Those are P-02-adjacent surfaces, out of scope here.
