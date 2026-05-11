# P-02 — Inference-stack tuning for the M6 cascade

**Status**: planning (graduated). See
[`docs/plans/p-02-inference-stack-tuning.md`](../plans/p-02-inference-stack-tuning.md)
for the full plan with sequenced phases, verification checkpoints,
and rollback procedures.

Origin: external feedback raised speculative decoding and prompt
prefix caching as M6 wall-time wins. The deferred-rationale half of
the same feedback was already shipped as `--full-rationale` (commit
`491c1b5`); the speculative-decoding and prefix-caching halves
graduated into the plan.
