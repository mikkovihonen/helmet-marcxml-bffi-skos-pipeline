"""Subprocess entry point for M5 (embed + candidate-pair generation).

Spawned by ``bffi_pipeline.runner`` to isolate FAISS / BGE-M3 memory
from the M6 (mlx-lm) judge step. The parent passes the run context
via env vars (``BFFI_RUN_UUID`` + ``BFFI_DATA_DIR`` + ``BFFI_RUN_AS_CHILD``
per ``memory/m5_process_isolation.md``); this module reads the same
``Settings`` the parent did and calls :func:`m5.build_index` +
:func:`m5.query_candidates` with default tuning.

Invoked as:

.. code-block:: shell

    python -m bffi_pipeline.stages.m5 [--force]

Tuning parameters (model, device, batch size, top-k, cross-block) are
read from the same env vars / Settings the runner's per-stage CLI
wrappers used; they're the existing operator knobs, not new ones.

P-38 Phase C-2: replaces ``bffi-pipeline embed`` as the M5 subprocess
target so the per-stage CLI wrapper can be deleted without breaking
M5 process isolation.
"""

from __future__ import annotations

import sys

from bffi_pipeline.cli import _init_observability
from bffi_pipeline.config import get_settings
from bffi_pipeline.stages import m5


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    force = "--force" in args
    settings = get_settings()
    target = settings.data_dir
    # The parent runner's emitter context is passed through the env
    # var BFFI_RUN_UUID; _init_observability picks the run dir up
    # and installs the per-child sidecar emitter so m5 events land in
    # the shared stage-events.jsonl.
    _init_observability(settings)
    build_result = m5.build_index(
        None,
        output_dir=target,
        model_name=m5.DEFAULT_MODEL,
        device=m5.DEFAULT_DEVICE,
        batch_size=m5.DEFAULT_BATCH_SIZE,
        force=force,
    )
    print(build_result.render())
    stats = m5.query_candidates(target, top_k=m5.DEFAULT_TOP_K, cross_block=False)
    print(stats.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
