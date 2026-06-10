"""Unit tests for the BFFI → MARC mapping-table generator.

Locks the decorator-driven registry contract and the doc-drift guard.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from bffi_pipeline.diagnostic.marc_mapping import (
    DEFAULT_DOC_PATH,
    PENDING_BEGIN_MARKER,
    PENDING_END_MARKER,
    SHIPPED_BEGIN_MARKER,
    SHIPPED_END_MARKER,
    build_blocks,
    regenerate_marc_mapping,
)
from bffi_pipeline.stages.bffi_to_marc.runner import (
    MARC_EMIT_REGISTRY,
    MARC_PENDING_REGISTRY,
    MarcEmitMeta,
    MarcPendingMeta,
)

# --- decorator-driven registry shape -----------------------------------


def test_marc_emit_registry_includes_expected_tags() -> None:
    """Every MARC family the reverse converter currently emits is in
    the registry. Adding a new ``@marc_emit``-decorated extract
    function appends here automatically."""
    expected_tags = {
        "leader",
        "001",
        "020",
        "022",
        "041",
        "245",
        "300",
        "600/610/611/630/648/650/651/655",
    }
    actual_tags = {entry.tag for entry in MARC_EMIT_REGISTRY}
    assert actual_tags == expected_tags


def test_marc_emit_entries_are_frozen_dataclasses() -> None:
    """:class:`MarcEmitMeta` is frozen so accidental mutation can't
    desync the registry from the auto-table."""
    for entry in MARC_EMIT_REGISTRY:
        assert isinstance(entry, MarcEmitMeta)
        # Frozen dataclasses raise FrozenInstanceError on assignment.
        try:
            entry.tag = "999"  # type: ignore[misc]
        except Exception:
            continue
        raise AssertionError(f"MarcEmitMeta is mutable: {entry}")


def test_marc_pending_registry_is_non_empty() -> None:
    """Pending registry should reflect actual 20 k bench gaps. A
    zero-length list would mean either we've shipped everything (very
    nice — update this test) or someone gutted the registry."""
    assert len(MARC_PENDING_REGISTRY) > 0
    for entry in MARC_PENDING_REGISTRY:
        assert isinstance(entry, MarcPendingMeta)
        assert entry.lost_count_20k > 0, f"pending entry {entry.tag} has zero count"


# --- generator output shape --------------------------------------------


def test_shipped_block_includes_every_registered_tag() -> None:
    """Every entry in :data:`MARC_EMIT_REGISTRY` produces exactly one
    row in the shipped auto-table."""
    shipped, _ = build_blocks()
    for entry in MARC_EMIT_REGISTRY:
        assert f"`{entry.tag}`" in shipped, f"shipped table missing row for {entry.tag}"


def test_pending_block_includes_every_pending_tag() -> None:
    """Every entry in :data:`MARC_PENDING_REGISTRY` produces exactly one
    row in the pending auto-table."""
    _, pending = build_blocks()
    for entry in MARC_PENDING_REGISTRY:
        assert f"`{entry.tag}`" in pending


def test_blocks_render_leader_first() -> None:
    """The ``leader`` pseudo-tag should be the first row in the shipped
    table — sort_key puts it before all numeric tags."""
    shipped, _ = build_blocks()
    leader_idx = shipped.find("`leader`")
    first_numeric_idx = shipped.find("`001`")
    assert leader_idx != -1
    assert first_numeric_idx != -1
    assert leader_idx < first_numeric_idx


def test_blocks_show_blank_indicators_as_hash() -> None:
    """MARC blank indicators (literal space) render as ``#`` in the
    table — visible in monospace, parseable for a reader."""
    shipped, _ = build_blocks()
    # 020 ISBN has blank/blank indicators per MARC convention.
    assert "| `020` | `##` |" in shipped


def test_blocks_show_dash_for_no_indicators() -> None:
    """Control fields and the leader have no indicators — rendered as
    ``—`` in the table."""
    shipped, _ = build_blocks()
    assert "| `001` | `—` |" in shipped
    assert "| `leader` | `—` |" in shipped


# --- doc-drift guard ----------------------------------------------------


def test_doc_on_disk_matches_generator_output() -> None:
    """``docs/bffi_to_marc_mapping.md`` must equal what the generator
    would emit. If this fails: run ``bffi-pipeline regenerate-marc-mapping``
    and commit the diff."""
    _, changed = regenerate_marc_mapping(check=True)
    assert not changed, (
        "docs/bffi_to_marc_mapping.md is out of sync with the generator — "
        "run `bffi-pipeline regenerate-marc-mapping` to refresh."
    )

    on_disk = DEFAULT_DOC_PATH.read_text(encoding="utf-8")
    assert SHIPPED_BEGIN_MARKER in on_disk
    assert SHIPPED_END_MARKER in on_disk
    assert PENDING_BEGIN_MARKER in on_disk
    assert PENDING_END_MARKER in on_disk


def test_generator_is_idempotent(tmp_path: Path) -> None:
    """Running the generator twice on the same doc produces no further
    changes — proves the output is stable."""
    tmp_doc = tmp_path / "mapping.md"
    shutil.copy(DEFAULT_DOC_PATH, tmp_doc)

    text_after_first, _ = regenerate_marc_mapping(doc_path=tmp_doc)
    text_after_second, changed_second = regenerate_marc_mapping(doc_path=tmp_doc)
    assert text_after_first == text_after_second
    assert changed_second is False
