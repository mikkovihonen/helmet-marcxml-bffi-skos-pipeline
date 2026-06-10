"""Closed-namespace discipline for the BFFI -> MARC reverse converter.

Cardinal rule (stated in `CLAUDE.md`): the reverse converter MUST NOT
consult the pipeline-internal provenance vocabulary when deciding what
content to emit. Pipeline-internal data is fair for UI / pairing
machinery (diff comparator lineage tokens, etc.) but never for
bibliographic content reconstruction.

This static-source scan walks the Python AST of every module under
``src/bffi_pipeline/stages/bffi_to_marc/`` and fails the build if it
finds *executable* code that imports or references the pipeline-internal
provenance vocab. Module / function / class docstrings are filtered out
so the rule can be enforced on code while still being documented in
prose.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_STAGE_DIR: Final[Path] = _REPO_ROOT / "src" / "bffi_pipeline" / "stages" / "bffi_to_marc"

#: Identifier names that constitute a contract violation when they appear
#: in executable code (Name / Attribute / ImportFrom alias nodes).
_FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset(
    {"BFFI_PROV", "bffi_prov", "BFFI_PROV_NAMESPACE"}
)

#: Substrings that constitute a contract violation when they appear in a
#: string literal in executable code (not a docstring).
_FORBIDDEN_STRING_FRAGMENTS: Final[tuple[str, ...]] = (
    "bffi-prov:",
    "schema:bffi-prov",
)


@dataclass(frozen=True)
class Violation:
    """One AST-detected contract violation."""

    file: str
    lineno: int
    detail: str


def _docstring_constant_linenos(tree: ast.AST) -> set[int]:
    """Return the ``lineno`` of every node that's recognised as a docstring."""
    out: set[int] = set()
    for parent in ast.walk(tree):
        if isinstance(
            parent,
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        ):
            doc = ast.get_docstring(parent, clean=False)
            if doc is not None and parent.body:
                first = parent.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    out.add(first.value.lineno)
    return out


def _collect_violations(path: Path) -> list[Violation]:
    """Walk one module's AST; return the executable-code violations.

    Skips docstring-positioned ``Constant`` nodes so a module that
    *documents* the rule (mentioning the forbidden namespace by name in
    its docstring) doesn't trip the scan.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstring_lines = _docstring_constant_linenos(tree)
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            violations.append(
                Violation(
                    file=path.name,
                    lineno=node.lineno,
                    detail=f"references forbidden name {node.id!r}",
                )
            )
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.append(
                Violation(
                    file=path.name,
                    lineno=node.lineno,
                    detail=f"references forbidden attribute {node.attr!r}",
                )
            )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _FORBIDDEN_NAMES:
                    violations.append(
                        Violation(
                            file=path.name,
                            lineno=node.lineno,
                            detail=(f"imports forbidden name {alias.name!r} from {node.module!r}"),
                        )
                    )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in docstring_lines:
                continue
            for token in _FORBIDDEN_STRING_FRAGMENTS:
                if token in node.value:
                    violations.append(
                        Violation(
                            file=path.name,
                            lineno=node.lineno,
                            detail=(f"string literal contains forbidden token {token!r}"),
                        )
                    )
    return violations


def test_bffi_to_marc_source_does_not_reference_bffi_prov() -> None:
    """The reverse converter is content-only. Any executable reference to
    the pipeline-internal provenance vocab is a contract violation per
    the cardinal rule stated in ``CLAUDE.md``."""
    all_violations: list[Violation] = []
    for path in sorted(_STAGE_DIR.rglob("*.py")):
        all_violations.extend(_collect_violations(path))
    if all_violations:
        lines = "\n  ".join(f"{v.file}:{v.lineno} {v.detail}" for v in all_violations)
        msg = (
            "stages/bffi_to_marc references pipeline-internal "
            "provenance in executable code:\n  " + lines
        )
        raise AssertionError(msg)


def test_static_scan_catches_executable_violation(tmp_path: Path) -> None:
    """Sanity check: a fake module that imports + references the forbidden
    name in executable code IS flagged. Guards against the scan
    accidentally accepting violations."""
    fake = tmp_path / "violator.py"
    fake.write_text(
        "from bffi_pipeline.provenance.vocab import BFFI_PROV\nX = BFFI_PROV\n",
        encoding="utf-8",
    )
    assert _collect_violations(fake), "scan failed to flag a deliberate violator"


def test_static_scan_does_not_flag_docstring_mention(tmp_path: Path) -> None:
    """The rule is enforced on code, not prose. A module that *describes*
    the rule in its docstring (citing the forbidden namespace by name)
    must not be flagged — otherwise documenting the discipline would
    itself break the discipline."""
    fake = tmp_path / "documents_rule.py"
    fake.write_text(
        '"""This module respects the bffi-prov: rule."""\nX = 1\n',
        encoding="utf-8",
    )
    assert not _collect_violations(fake)
