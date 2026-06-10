"""Unit tests for the cataloguer-review HTML rendering."""

from __future__ import annotations

from bffi_pipeline.stages.roundtrip_eval.diff import FieldDiff, FieldRow, RecordDiff
from bffi_pipeline.stages.roundtrip_eval.html import render


def _df(tag: str, code: str, value: str) -> FieldRow:
    return FieldRow(tag=tag, ind1="0", ind2="0", subfields=((code, value),), text=None)


def _cf(tag: str, value: str) -> FieldRow:
    return FieldRow(tag=tag, ind1=None, ind2=None, subfields=(), text=value)


def test_render_emits_corpus_summary_with_each_status_count() -> None:
    cf001 = _cf("001", "b1")
    record = RecordDiff(
        bib_id="b1",
        fields=(
            FieldDiff(tag="001", status="identical", source=cf001, reconstructed=cf001),
            FieldDiff(
                tag="245",
                status="changed",
                source=_df("245", "a", "Original"),
                reconstructed=_df("245", "a", "Modified"),
            ),
            FieldDiff(
                tag="700",
                status="lost",
                source=_df("700", "a", "Andersen"),
                reconstructed=None,
            ),
            FieldDiff(
                tag="999",
                status="added",
                source=None,
                reconstructed=_df("999", "a", "New"),
            ),
        ),
    )
    out = render([record])
    # Each status appears at least once in the corpus-distribution table.
    for status in ("identical", "changed", "lost", "added"):
        assert status in out
    # Bib ID surfaces in the per-record overview.
    assert "b1" in out
    # The per-record detail panel uses a `details` element keyed by the bib ID.
    assert 'id="bib-b1"' in out


def test_render_escapes_html_entities_in_field_content() -> None:
    """A title containing `<` / `&` survives without breaking the HTML."""
    record = RecordDiff(
        bib_id="b1",
        fields=(
            FieldDiff(
                tag="245",
                status="changed",
                source=_df("245", "a", "Pride <and> Prejudice & Friends"),
                reconstructed=_df("245", "a", "x"),
            ),
        ),
    )
    out = render([record])
    assert "Pride &lt;and&gt; Prejudice &amp; Friends" in out
    # Raw unescaped content must NOT appear (would break the markup).
    assert "Pride <and> Prejudice & Friends" not in out


def test_render_handles_zero_records() -> None:
    """Edge case: empty corpus still produces a valid (if uninteresting) document."""
    out = render([])
    assert "<html" in out
    assert "0 record" in out
