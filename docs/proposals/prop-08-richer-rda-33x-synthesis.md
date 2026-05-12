# P-08 — Richer RDA 33X synthesis from leader / 007 / 008 / 245$h

**Status**: planning (graduated). See
[`docs/plans/backlog/p-08-richer-rda-33x-synthesis.md`](../plans/backlog/p-08-richer-rda-33x-synthesis.md)
for the full plan with sequenced phases (A coverage analysis,
B leader/06 + 007 cascade, C 008/006/245$h/300$a layers, D `$5`
provenance marker), verification checkpoints, and a phase-by-phase
rollback procedure.
**Proposal-base commit**: `19e09e4` (initial draft, content collapsed
to this stub once the plan was drafted). The plan carries its own
`Plan-base commit` for the drift-against-current-state check.

Origin: the bib-material + itype cascade shipped in `3f92a09` +
`46b0f8a` recovers most pre-RDA records that dropped on the M2
``marcxml-content-minimum`` gate, but leaves bibs with no mapped
signal still dropping. P-08 layers MARC's more precise signals
(leader/06, 007, 008, 006, 245$h GMD, 300$a) above the existing
material/itype tables to lift recovery to "near-deterministic per
record that carries any MARC manifestation signal." The plan is
gated on a Phase A coverage analysis confirming the projected
recovery rate before any production code lands.
