# P-08 — Richer RDA 33X synthesis from leader / 007 / 008 / 300$a

**Status**: done. See
[`docs/plans/completed/p-08-richer-rda-33x-synthesis.md`](../plans/completed/p-08-richer-rda-33x-synthesis.md)
for the full plan, all four phase commits, and the per-phase
findings (Phase A's coverage analysis, Phase B's load-bearing
(leader/06, 008-form) layer, Phase C's 300$a fallback, Phase D's
`$5 FI-HELME/synth-v<N>` provenance marker).
**Proposal-base commit**: `19e09e4` (initial draft, content collapsed
to this stub once the plan was drafted).

Origin: the bib-material + itype cascade shipped in `3f92a09` +
`46b0f8a` recovered most pre-RDA records that dropped on the M2
``marcxml-content-minimum`` gate, but left bibs with no mapped
material / itype signal still dropping (566 records on the P-02 5k
sample). P-08 layered MARC's more precise signals above the existing
material/itype tables.

What shipped (different from the original proposal):

- **245$h GMD** and **006** layers were **deferred** — Phase A's
  coverage analysis showed 0 % yield on the 5k sample. They can be
  added later if a larger corpus or new cataloguing practice shows
  non-trivial yield.
- **Leader/06** and **008-form** were combined into a single
  `(leader/06, 008-form)` lookup layer (instead of two separate
  layers). Phase A measured 100 % coverage on the drop list for
  this pair, so the combined layer became the load-bearing one.
- **300$a extent** ships as a last-resort fallback for the
  long-tail leader/06 distribution on the full 800k corpus (no
  yield on the 5k sample, but cheap insurance).
- The shipped goal was tightened from `≤ 100` (original proposal)
  to `≤ 50` (revised after Phase A) and beaten — the 566 drops
  close to 0 under the shipped cascade.
