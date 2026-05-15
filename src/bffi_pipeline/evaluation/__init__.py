"""Evaluation / benchmarking CLI surface for the bffi pipeline.

Houses the M12 gold-set scoring and M5 embedding-benchmark commands.
These aren't pipeline-stage invocations — they're development /
benchmarking tooling that produces operator artefacts (JSON
summaries, JSONL candidate sets, model-comparison reports). The
implementations live under :mod:`bffi_pipeline.eval` (the actual
scoring + benchmarking code); this package provides the CLI wrapper
layer.

P-38 Phase C-3: created so the eval/M12 CLI commands live next to
their own package boundary rather than sitting at the top of
``cli.py`` next to the orchestration commands.
"""
