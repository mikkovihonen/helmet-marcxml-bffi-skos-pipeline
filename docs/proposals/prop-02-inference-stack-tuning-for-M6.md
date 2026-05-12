# P-02 — Inference-stack tuning for the M6 cascade

**Status**: planning (graduated). See
[`docs/plans/completed/p-02-inference-stack-tuning.md`](../plans/completed/p-02-inference-stack-tuning.md)
for the full plan with sequenced phases, verification checkpoints,
and rollback procedures.
**Proposal-base commit**: `334294a` (initial draft as part of the
combined `performance-enhancements.md`; content collapsed to this
stub once the plan was drafted). The plan carries its own
`Plan-base commit` for the drift-against-current-state check.

Origin: external feedback raised speculative decoding and prompt
prefix caching as M6 wall-time wins. The deferred-rationale half of
the same feedback was already shipped as `--full-rationale` (commit
`491c1b5`); the speculative-decoding and prefix-caching halves
graduated into the plan.
