"""Run-directory convention.

Every pipeline invocation writes its output into a ``runs/<run-id>/``
directory, where ``<run-id>`` follows the canonical shape
``yyyymmdd-hhmm-<6hex>`` (UTC timestamp + 6 random hex chars to
disambiguate concurrent runs in the same minute).

Example: ``runs/20260610-1715-a3f2c1/``

The directory typically contains sub-paths for each stage's output:

  runs/20260610-1715-a3f2c1/
    ├── bibframe/        # marc-to-bibframe output
    ├── bffi/            # bibframe-to-bffi output
    ├── reconstructed/   # bffi-to-marc output
    ├── report.html      # roundtrip-eval output
    └── stage-events.jsonl   # observability sidecar (when wired)

The convention is enforced by the CLI: any ``--output-dir`` / ``--html``
path passed to a stage subcommand must resolve to a path inside a
canonical run directory. Inputs (the original Helmet MARCXML, a Sierra
dump, etc.) come from anywhere; outputs land in canonical run dirs so
the dashboard can pick them up consistently and so the operator can
reason about run identity at a glance.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

#: Canonical run-directory name: ``yyyymmdd-hhmm-<6hex>``.
#: Example: ``20260610-1715-a3f2c1``.
RUN_DIR_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{8}-\d{4}-[0-9a-f]{6}$")

#: Directory name under the repo root that contains all run directories.
RUNS_PARENT: Final[str] = "runs"


class InvalidRunDirError(ValueError):
    """Raised when a path doesn't resolve to a canonical run directory."""


def mint_run_dir(parent: Path | None = None) -> Path:
    """Mint a fresh run directory and return its path.

    Creates ``<parent>/<run-id>/`` on disk (``parent`` defaults to
    ``Path("runs")``). The run ID combines a UTC timestamp
    (``yyyymmdd-hhmm``) with 6 random hex chars from
    :func:`os.urandom` — enough to disambiguate concurrent runs in the
    same minute without coordinating across operators.

    Raises :exc:`FileExistsError` on the (vanishingly-unlikely) hash
    collision; the caller can retry.
    """
    parent_dir = parent if parent is not None else Path(RUNS_PARENT)
    now = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    random_hex = os.urandom(3).hex()
    run_dir = parent_dir / f"{now}-{random_hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def is_canonical_run_dir(path: Path) -> bool:
    """Return True iff ``path`` is itself a canonical run directory."""
    return RUN_DIR_PATTERN.fullmatch(path.name) is not None and path.parent.name == RUNS_PARENT


def validate_under_run_dir(path: Path) -> Path:
    """Raise :exc:`InvalidRunDirError` if ``path`` isn't under a run dir.

    A path passes validation when:

    - it is itself a canonical run directory (``runs/<run-id>``), OR
    - one of its ancestors is a canonical run directory.

    Returns the resolved canonical run directory ancestor for the
    caller's convenience (e.g. for emitting the run ID into
    observability events).
    """
    resolved = path.resolve()
    candidates = (resolved, *resolved.parents)
    for candidate in candidates:
        if is_canonical_run_dir(candidate):
            return candidate
    raise InvalidRunDirError(
        f"{path} is not under a canonical `{RUNS_PARENT}/<yyyymmdd-hhmm-<hash>>/`"
        " directory. Mint a fresh run with `bffi-pipeline new-run` and use that"
        " path (or a sub-path under it) as the --output-dir target."
    )
