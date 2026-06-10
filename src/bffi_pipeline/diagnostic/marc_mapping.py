"""Auto-generate the BFFI → MARC mapping table in
`docs/bffi_to_marc_mapping.md`.

The generator walks
:data:`bffi_pipeline.stages.bffi_to_marc.runner.MARC_EMIT_REGISTRY`
(populated by ``@marc_emit``-decorated extract functions in the
reverse converter) and renders the table between AUTO markers in the
doc.

Run via ``bffi-pipeline regenerate-marc-mapping`` or programmatically
via :func:`regenerate_marc_mapping`. The CLI's ``--check`` flag is
the drift guard suitable for pre-commit.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Final

from bffi_pipeline.stages.bffi_to_marc.runner import (
    MARC_EMIT_REGISTRY,
    MarcEmitMeta,
)

#: Markers framing the auto-generated MARC-mapping block.
SHIPPED_BEGIN_MARKER: Final[str] = "<!-- BEGIN AUTO: shipped -->"
SHIPPED_END_MARKER: Final[str] = "<!-- END AUTO: shipped -->"

#: Default doc location.
DEFAULT_DOC_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "docs" / "bffi_to_marc_mapping.md"
)


def _format_indicators(indicators: tuple[str, ...]) -> str:
    """Render indicators for the table cell. A literal space (the MARC
    "blank indicator") is rendered as ``#`` so it's visible in
    monospace; an empty tuple becomes ``—`` for control fields / leader.
    """
    if not indicators:
        return "—"
    return "".join("#" if c == " " else c for c in indicators)


def _format_subfields(subfields: tuple[tuple[str, str], ...]) -> str:
    """Render subfields as a ``<br>``-separated single-line list. Grid
    tables don't nest well in many markdown renderers, so we keep the
    cell content flat."""
    if not subfields:
        return "—"
    return "<br>".join(f"`${code}` — {desc}" for code, desc in subfields)


def _render_shipped_table(rows: Iterable[MarcEmitMeta]) -> str:
    header = "| MARC tag | Ind1 / Ind2 | Subfields | BFFI source | Notes |\n|---|---|---|---|---|\n"
    body = "".join(
        "| `{tag}` | `{indicators}` | {subfields} | {source} | {notes} |\n".format(
            tag=row.tag,
            indicators=_format_indicators(row.indicators),
            subfields=_format_subfields(row.subfields),
            source=row.source,
            notes=row.notes or "—",
        )
        for row in rows
    )
    return header + body


def _sort_key(tag: str) -> tuple[int, str]:
    """Sort key: ``leader`` first, then numeric tags ascending, then
    any string-tag aliases (like ``"600/610/.../655"``)."""
    if tag == "leader":
        return (0, "")
    if tag[:3].isdigit():
        return (1, tag)
    return (2, tag)


def build_block() -> str:
    """Return the markdown block the generator emits."""
    sorted_emit = sorted(MARC_EMIT_REGISTRY, key=lambda e: _sort_key(e.tag))
    shipped = _render_shipped_table(sorted_emit)
    tally = f"\n_{len(MARC_EMIT_REGISTRY)} MARC tags currently emitted._\n"
    return shipped + tally


def _replace_block(doc_text: str, begin_marker: str, end_marker: str, new_block: str) -> str:
    """Replace content between markers, preserving the markers themselves."""
    begin_idx = doc_text.find(begin_marker)
    end_idx = doc_text.find(end_marker)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        msg = f"could not locate markers in doc: begin={begin_marker!r}, end={end_marker!r}"
        raise ValueError(msg)
    before = doc_text[: begin_idx + len(begin_marker)]
    after = doc_text[end_idx:]
    return f"{before}\n\n{new_block}\n{after}"


def regenerate_marc_mapping(
    *,
    doc_path: Path | None = None,
    check: bool = False,
) -> tuple[str, bool]:
    """Regenerate the BFFI → MARC mapping table in the doc.

    Returns ``(new_doc_text, changed)``. When ``check=True`` the file
    is not written — the caller compares the on-disk text against the
    returned text to decide pass / fail.
    """
    target = doc_path or DEFAULT_DOC_PATH
    original = target.read_text(encoding="utf-8")
    block = build_block()

    new_text = _replace_block(
        original,
        SHIPPED_BEGIN_MARKER,
        SHIPPED_END_MARKER,
        block,
    )

    changed = new_text != original
    if changed and not check:
        target.write_text(new_text, encoding="utf-8")
    return new_text, changed
