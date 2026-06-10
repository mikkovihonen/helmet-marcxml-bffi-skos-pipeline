"""xsltproc subprocess wrapper for the marc2bibframe2 XSLT.

Thin shim around the system ``xsltproc`` binary (XSLT 1.0, which is what
marc2bibframe2 uses). The vendored XSLT lives in
``third_party/marc2bibframe2/xsl/`` and is invoked unchanged — per the
CLAUDE.md rule, we wrap, never fork.

Two stylesheets matter for this stage:

- ``marc2bibframe2.xsl``  — main MARCXML -> BIBFRAME RDF/XML conversion.
- ``ConvSpec-Preprocess0-Splitting.xsl``  — optional preprocessing step
  that splits a single MARC record into multiple records when LoC's
  splitting rules detect distinct Instances; the LoC README strongly
  recommends always running this before the main conversion.

This module just runs xsltproc; the orchestration (which files, in
which order, with what parameters) lives in :mod:`runner`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Name of the xsltproc binary. Looked up in PATH each invocation; the
#: caller is responsible for ensuring it is installed.
XSLTPROC: Final[str] = "xsltproc"


@dataclass(frozen=True)
class XsltPaths:
    """Resolved paths to the vendored marc2bibframe2 stylesheets.

    Constructed once per pipeline invocation from the repo root. Tests
    can build their own instance pointing at fixture stylesheets if
    needed.
    """

    convert: Path
    preprocess: Path

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> XsltPaths:
        base = repo_root / "third_party" / "marc2bibframe2" / "xsl"
        return cls(
            convert=base / "marc2bibframe2.xsl",
            preprocess=base / "ConvSpec-Preprocess0-Splitting.xsl",
        )


@dataclass(frozen=True)
class XsltResult:
    """Outcome of one xsltproc invocation."""

    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class XsltprocError(RuntimeError):
    """xsltproc invocation failed (non-zero exit, timeout, or missing binary)."""


def run_xsltproc(
    *,
    stylesheet: Path,
    input_path: Path,
    params: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> XsltResult:
    """Run xsltproc and capture stdout/stderr/returncode.

    ``params`` are passed as ``--stringparam <key> <value>`` pairs to the
    XSLT runtime. Raises :exc:`XsltprocError` for timeout / binary-missing
    failures; non-zero exit codes are returned as a non-ok
    :class:`XsltResult` (the caller decides whether to treat them as fatal).
    """
    cmd: list[str] = [XSLTPROC]
    if params:
        for key, value in params.items():
            cmd.extend(["--stringparam", key, value])
    cmd.extend([str(stylesheet), str(input_path)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise XsltprocError(f"xsltproc timed out after {timeout}s on {input_path}") from exc
    except FileNotFoundError as exc:
        raise XsltprocError(f"xsltproc binary not found in PATH (looked for {XSLTPROC!r})") from exc

    return XsltResult(
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )
