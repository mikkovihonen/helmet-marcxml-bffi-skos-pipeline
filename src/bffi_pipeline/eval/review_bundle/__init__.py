"""Review-bundle package: zip-bundled cataloguer-review materials.

The bundle is the unit of cataloguer review for any pipeline stage that
emits an LLM-output queue. The operator builds a zip via
:func:`build_bundle`, the cataloguer opens it in
``gold/cataloguer-review.html``, and the operator imports the
cataloguer's results zip via :func:`import_results`.

Bundle layout::

    review-batch-<date>.zip
    ├── manifest.json
    └── <stage>/
        ├── candidates.jsonl
        └── marc/<bib>.xml       (sidecar artefacts; per-stage optional)

Stages are pluggable: each LLM-using pipeline stage that wants
cataloguer review registers a :class:`StageHandler` under a
versioned schema id in :mod:`stages` ``STAGE_REGISTRY``. V2 ships
with one handler — ``contrib-candidate/1`` for M3 contributor
extraction. M6 judge / M9 picker / M2 salvage / M3 title-lang each
get their own handler when their candidate queue lands.

The JSON ``schema`` field on every stage entry is the dispatch key:
the HTML's stage-surface registry and the Python ``STAGE_REGISTRY``
both index on it. Versioned schema ids let the format evolve
without breaking older bundles in the wild — an unknown schema
renders as a placeholder tab rather than aborting the load.
"""

from __future__ import annotations

from bffi_pipeline.eval.review_bundle.bundle import (
    BundleBuildError,
    BundleReadError,
    BundleResult,
    ImportDispatchResult,
    build_bundle,
    import_results,
    read_bundle,
)
from bffi_pipeline.eval.review_bundle.manifest import Manifest, StageEntry
from bffi_pipeline.eval.review_bundle.stages import STAGE_REGISTRY, StageHandler

__all__ = [
    "STAGE_REGISTRY",
    "BundleBuildError",
    "BundleReadError",
    "BundleResult",
    "ImportDispatchResult",
    "Manifest",
    "StageEntry",
    "StageHandler",
    "build_bundle",
    "import_results",
    "read_bundle",
]
