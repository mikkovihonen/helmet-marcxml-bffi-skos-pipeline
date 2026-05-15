"""M2 marc2bibframe2 XSLT cache + LoC converter invocation.

The LoC ``marc2bibframe2`` XSLT lives under
``third_party/marc2bibframe2/xsl/marc2bibframe2.xsl`` (git submodule).
We:

- cache the parsed XSLT object once per process (``_xslt`` /
  ``lru_cache``) so the XSL parse cost is amortised across the per-record
  loop;
- record the submodule's git commit + tag at startup
  (``marc2bibframe2_version``) so every M2 provenance record carries the
  converter version that produced it.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Final

from lxml import etree

_BFFI_PIPELINE_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_MARC2BIBFRAME2_DIR: Final[Path] = _BFFI_PIPELINE_REPO_ROOT / "third_party" / "marc2bibframe2"
_XSLT_PATH: Final[Path] = _MARC2BIBFRAME2_DIR / "xsl" / "marc2bibframe2.xsl"


@lru_cache(maxsize=1)
def _xslt() -> etree.XSLT:
    if not _XSLT_PATH.exists():
        raise RuntimeError(
            f"marc2bibframe2 XSLT not found at {_XSLT_PATH}. "
            "Run `git submodule update --init --recursive`."
        )
    return etree.XSLT(etree.parse(str(_XSLT_PATH)))


@lru_cache(maxsize=1)
def marc2bibframe2_version() -> str:
    """Return the marc2bibframe2 commit SHA (and tag if reachable)."""
    if not _MARC2BIBFRAME2_DIR.exists():
        return "unknown"
    try:
        sha = subprocess.run(
            ["git", "-C", str(_MARC2BIBFRAME2_DIR), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    try:
        tag = subprocess.run(
            ["git", "-C", str(_MARC2BIBFRAME2_DIR), "describe", "--tags", "--always"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return f"{tag}+{sha[:12]}"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return sha
