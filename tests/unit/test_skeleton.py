"""M0 smoke test: package imports and version is exposed."""

from __future__ import annotations

import bffi_pipeline


def test_package_version_is_exposed() -> None:
    assert bffi_pipeline.__version__ == "0.1.0"
