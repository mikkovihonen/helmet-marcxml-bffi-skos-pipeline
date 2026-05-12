"""Unit tests for stages/marc_to_bf — the in-tree sanitisers.

End-to-end MARCXML → BIBFRAME conversion is exercised by integration
tests under ``tests/integration/``; this module covers the small in-
process repair functions that run between the XSLT and rdflib's RDF/XML
parser.
"""

from __future__ import annotations

from io import BytesIO

from lxml import etree

from bffi_pipeline.stages.marc_to_bf import _sanitize_language_tags


_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _parse(xml: bytes) -> etree._ElementTree:
    return etree.parse(BytesIO(xml))


def test_sanitize_language_tags_strips_trailing_hyphen() -> None:
    """``ru-`` → ``ru``; valid tags untouched; element without xml:lang
    untouched. P-02 5k run found 7 / 5000 records emitting ``ru-`` /
    ``uk-`` from marc2bibframe2's 008-country → BCP-47 mapping."""
    src = b"""<?xml version="1.0"?>
<root xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <a xml:lang="ru-">value</a>
  <b xml:lang="ru">value</b>
  <c xml:lang="ru-RU">value</c>
  <d xml:lang="uk-">value</d>
  <e>no attr</e>
  <f xml:lang="ru--">double dash trailing</f>
</root>"""
    tree = _parse(src)
    fixed = _sanitize_language_tags(tree)
    # ru-, uk-, ru-- are rewritten; ru and ru-RU stay; e has no attr.
    assert fixed == 3
    root = tree.getroot()
    langs = [el.get(_XML_LANG) for el in root]
    assert langs == ["ru", "ru", "ru-RU", "uk", None, "ru"]


def test_sanitize_language_tags_returns_zero_when_clean() -> None:
    src = b"""<?xml version="1.0"?>
<root xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <a xml:lang="fi">x</a>
  <b xml:lang="en-US">x</b>
</root>"""
    tree = _parse(src)
    assert _sanitize_language_tags(tree) == 0


def test_sanitize_language_tags_walks_nested_elements() -> None:
    """Nested elements (e.g. literals inside a ``bf:Language``) get
    sanitised too — the sanitiser uses ``tree.iter()`` so depth doesn't
    matter."""
    src = b"""<?xml version="1.0"?>
<root xmlns:xml="http://www.w3.org/XML/1998/namespace">
  <outer xml:lang="ok">
    <inner xml:lang="ru-">deeply nested</inner>
  </outer>
</root>"""
    tree = _parse(src)
    fixed = _sanitize_language_tags(tree)
    assert fixed == 1
    nested = tree.getroot().find("outer/inner")
    assert nested is not None
    assert nested.get(_XML_LANG) == "ru"
