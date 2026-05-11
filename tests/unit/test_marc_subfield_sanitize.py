"""Unit tests for the pre-XSLT MARCXML subfield-separator recovery.

Pattern E from the 200-record corpus smoke: cataloguer-pasted ``‡``
(U+2021) as a literal subfield separator inside a single ``$a``
value (likely copy-paste from a legacy ILS display that uses ``‡``
as a visible boundary marker). The sanitizer in
``stages.marc_to_bf`` splits these back into proper MARCXML
subfields so marc2bibframe2 sees the cataloguer's intended shape.
"""

from __future__ import annotations

from lxml import etree

from bffi_pipeline.stages.marc_to_bf import _sanitize_subfield_separators

_MARC_NS = "http://www.loc.gov/MARC21/slim"
_NSMAP = {None: _MARC_NS}


def _build_tree(datafield_xml: str) -> etree._ElementTree:
    """Wrap ``datafield_xml`` in a minimal MARCXML envelope and return
    a parsed ElementTree."""
    record_xml = (
        f'<record xmlns="{_MARC_NS}">'
        "<leader>00000najm a2200000ua 4500</leader>"
        '<controlfield tag="001">12345678</controlfield>'
        f"{datafield_xml}"
        "</record>"
    )
    return etree.ElementTree(etree.fromstring(record_xml))


def _datafield_children(tree: etree._ElementTree, tag: str) -> list[tuple[str, str]]:
    """Return ``(code, text)`` pairs for every subfield under the
    named ``<datafield tag="...">``."""
    parent = tree.find(f"{{{_MARC_NS}}}datafield[@tag='{tag}']")
    assert parent is not None
    return [
        (sf.get("code", ""), (sf.text or "")) for sf in parent.findall(f"{{{_MARC_NS}}}subfield")
    ]


# --- Headline case: cataloguer-pasted ‡2 / ‡0 in $a ---------------------


def test_splits_dagger_separated_subfields_into_proper_marcxml() -> None:
    """The Pattern E example: ``$a`` carries the entire intended
    ``$a $2 $0`` triple. After sanitization, three proper subfields
    appear in order, with the original ``$a`` truncated to just the
    leading value."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">taidemusiikki'
        "‡2slm/fin"
        "‡0http://urn.fi/URN:NBN:fi:au:slm:s474"
        "</subfield>"
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 1
    assert _datafield_children(tree, "655") == [
        ("a", "taidemusiikki"),
        ("2", "slm/fin"),
        ("0", "http://urn.fi/URN:NBN:fi:au:slm:s474"),
    ]


def test_preserves_well_formed_subfields_unchanged() -> None:
    """Records with proper subfield delimiters must pass through
    untouched — the sanitizer triggers only on literal ``‡`` inside
    a subfield's text."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">requiemit</subfield>'
        '<subfield code="2">slm/fin</subfield>'
        '<subfield code="0">http://urn.fi/URN:NBN:fi:au:slm:s781</subfield>'
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 0
    assert _datafield_children(tree, "655") == [
        ("a", "requiemit"),
        ("2", "slm/fin"),
        ("0", "http://urn.fi/URN:NBN:fi:au:slm:s781"),
    ]


def test_leaves_bare_dagger_alone() -> None:
    """A bare ``‡`` not followed by an alphanumeric code (e.g. a
    footnote / typography use inside a title) must NOT trigger the
    split — protects against false-positive rewrites of legitimate
    content."""
    tree = _build_tree(
        '<datafield tag="245" ind1="1" ind2="0">'
        '<subfield code="a">A title with a ‡ dagger in it</subfield>'
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 0
    assert _datafield_children(tree, "245") == [
        ("a", "A title with a ‡ dagger in it"),
    ]


def test_returns_zero_on_records_with_no_subfields() -> None:
    """Records with only controlfields produce zero rewrites without
    raising — guards against IndexError on empty datafield lists."""
    record = f'<record xmlns="{_MARC_NS}"><controlfield tag="001">99999999</controlfield></record>'
    tree = etree.ElementTree(etree.fromstring(record))
    assert _sanitize_subfield_separators(tree) == 0


def test_splits_multiple_subfields_within_same_datafield() -> None:
    """One datafield may carry several daggered subfields (different
    $a / $b lines); each gets split independently, count tracks
    rewrites (not new subfields produced)."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">A‡2x‡0urn:A</subfield>'
        '<subfield code="a">B‡2y‡0urn:B</subfield>'
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 2
    assert _datafield_children(tree, "655") == [
        ("a", "A"),
        ("2", "x"),
        ("0", "urn:A"),
        ("a", "B"),
        ("2", "y"),
        ("0", "urn:B"),
    ]


def test_splits_across_multiple_datafields() -> None:
    """Each datafield is processed independently; the per-tree counter
    aggregates across all of them."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">orkesterilaulut</subfield>'
        '<subfield code="a">taidemusiikki‡2slm/fin‡0http://urn.fi/URN:NBN:fi:au:slm:s474</subfield>'
        "</datafield>"
        '<datafield tag="651" ind1=" " ind2="7">'
        '<subfield code="a">Helsinki‡2yso/fin</subfield>'
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 2  # two subfields rewritten
    assert _datafield_children(tree, "655") == [
        ("a", "orkesterilaulut"),
        ("a", "taidemusiikki"),
        ("2", "slm/fin"),
        ("0", "http://urn.fi/URN:NBN:fi:au:slm:s474"),
    ]
    assert _datafield_children(tree, "651") == [
        ("a", "Helsinki"),
        ("2", "yso/fin"),
    ]


def test_preserves_subfield_order_relative_to_surrounding_siblings() -> None:
    """Recovered subfields appear immediately after the original
    one — not at the end of the datafield — so cataloguer-intended
    order is preserved relative to subsequent siblings."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">A‡2x</subfield>'
        '<subfield code="a">B</subfield>'
        "</datafield>"
    )
    _sanitize_subfield_separators(tree)
    assert _datafield_children(tree, "655") == [
        ("a", "A"),
        ("2", "x"),
        ("a", "B"),
    ]


def test_handles_dagger_at_start_of_value() -> None:
    """If the cataloguer's paste starts with ``‡<code>`` (no leading
    plain $a content), the original subfield's text becomes empty
    and the recovered code/content appear as siblings. Empty $a is
    legal MARCXML — marc2bibframe2 will just ignore it."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">‡2slm/fin‡0http://urn.fi/URN:NBN:fi:au:slm:s474</subfield>'
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 1
    children = _datafield_children(tree, "655")
    assert children[0] == ("a", "")
    assert children[1:] == [
        ("2", "slm/fin"),
        ("0", "http://urn.fi/URN:NBN:fi:au:slm:s474"),
    ]


def test_consecutive_daggers_treated_as_separate_markers() -> None:
    """Two adjacent ``‡<code>`` markers with no content between them
    are split as two subfields, the first carrying empty text."""
    tree = _build_tree(
        '<datafield tag="655" ind1=" " ind2="7">'
        '<subfield code="a">A‡2‡0urn:A</subfield>'
        "</datafield>"
    )
    fixed = _sanitize_subfield_separators(tree)
    assert fixed == 1
    assert _datafield_children(tree, "655") == [
        ("a", "A"),
        ("2", ""),
        ("0", "urn:A"),
    ]
