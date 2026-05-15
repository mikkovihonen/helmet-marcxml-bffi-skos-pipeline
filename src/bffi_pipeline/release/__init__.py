"""Release-bundle tooling for the bffi pipeline.

Currently a single module: :mod:`bffi_pipeline.release.export` (the
``bffi-pipeline export`` command's backing logic — bundles canonical
output + provenance + helmet-map + run manifest into a CC0 tarball).
This package was carved out of ``stages/`` because release artefact
generation is operator-facing tooling, not a per-pipeline-run stage.
"""

from bffi_pipeline.release import export

__all__ = ["export"]
