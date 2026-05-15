"""M2 pre-XSLT byte-level MARCXML repairs.

Two cataloguer-data fixes applied before the marc2bibframe2 XSLT
runs, so the XSLT processes a well-formed tree:

- :func:`_sanitize_subfield_separators` — splits cataloguer-pasted
  ``‡<code>`` separators into proper ``<marc:subfield code="N">``
  elements. Triggered by legacy ILS displays where the operator
  copied a single value containing visible-separator daggers.
- :func:`_sanitize_language_tags` — strips trailing ``-`` from
  ``xml:lang`` attributes. marc2bibframe2 sometimes emits ``ru-`` /
  ``uk-`` when its MARC-country → BCP-47-region lookup falls through;
  rdflib's RDF/XML parser raises on the malformed tag otherwise.

P-38 Phase D: extracted from m2/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

import re
from typing import Final

from lxml import etree

#: MARCXML element/attribute names. lxml expanded form keeps the
#: comparison cheap and namespace-correct under
#: ``http://www.loc.gov/MARC21/slim``.
_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_SUBFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}subfield"

#: Regex for the cataloguer-pasted subfield separator. ``‡`` (U+2021,
#: DOUBLE DAGGER) was used as a visible separator in some legacy ILS
#: displays — operators sometimes copy-paste from those displays into
#: a single ``$a`` value, producing strings like
#: ``"taidemusiikki‡2slm/fin‡0http://urn.fi/URN:NBN:fi:au:slm:s474"``.
#: The capture group is the MARC subfield code (single alphanumeric);
#: the alternation excludes bare ``‡`` (e.g. a footnote dagger inside
#: a title) so we don't split legitimate uses.
_TAGGED_DAGGER_RE: Final[re.Pattern[str]] = re.compile(r"‡([0-9a-z])")

#: Length of the ``re.split`` result that has at least one capture
#: group fired — ``[leading_text, code1, content1]``.
_MIN_SPLIT_PARTS: Final[int] = 3

#: ``xml:lang`` attribute in expanded form (the XML namespace, not the
#: RDF/XML one). Walked over the XSLT output to repair malformed
#: BCP-47 tags before rdflib parses them — see
#: :func:`_sanitize_language_tags`.
_XML_LANG_ATTR: Final[str] = "{http://www.w3.org/XML/1998/namespace}lang"

#: One or more trailing ``-`` characters; the marc2bibframe2 XSLT
#: emits these (``ru-``, ``uk-``) when it can't map a MARC 008
#: country code into a BCP-47 region subtag.
_TRAILING_DASH_RE: Final[re.Pattern[str]] = re.compile(r"-+$")


def _sanitize_subfield_separators(tree: etree._ElementTree) -> int:
    """Split cataloguer-pasted ``‡<code>`` separators into proper
    MARCXML subfields, in place.

    Walks every ``<marc:subfield>`` element; for each value containing
    ``‡`` followed by a MARC subfield code (a-z / 0-9), splits the
    text at every such marker and rewrites the parent ``<datafield>``
    so the recovered subfield codes / values appear as proper
    sibling ``<subfield code="N">value</subfield>`` elements. The
    original subfield is kept in place (with its truncated leading
    value); the new subfields are inserted right after it, preserving
    cataloguer-intended order.

    Returns the count of *original* subfields rewritten, for operator
    visibility. The marc2bibframe2 XSLT then processes the corrected
    tree as if the cataloguer had typed the subfields correctly —
    proper ``bf:source`` and ``$0`` URI handling fall out automatically.
    """
    fixed = 0
    for subfield in tree.iter(_SUBFIELD_TAG):
        text = subfield.text or ""
        if "‡" not in text:
            continue
        parts = _TAGGED_DAGGER_RE.split(text)
        # re.split with one capture group yields:
        #   [leading_text, code1, content1, code2, content2, ...].
        # If no markers matched, parts == [text] — leave alone.
        if len(parts) < _MIN_SPLIT_PARTS:
            continue
        leading, *pairs = parts[0], *parts[1:]
        subfield.text = leading
        parent = subfield.getparent()
        if parent is None:
            continue
        insertion_index = list(parent).index(subfield) + 1
        # pairs alternates (code, content); build sibling subfields.
        for i in range(0, len(pairs), 2):
            code = pairs[i]
            content = pairs[i + 1] if i + 1 < len(pairs) else ""
            new_sf = etree.SubElement(parent, _SUBFIELD_TAG)
            new_sf.set("code", code)
            new_sf.text = content
            # SubElement appends; move into the right position.
            parent.remove(new_sf)
            parent.insert(insertion_index, new_sf)
            insertion_index += 1
        fixed += 1
    return fixed


def _sanitize_language_tags(tree: etree._ElementTree) -> int:
    """Strip trailing ``-`` from ``xml:lang`` attribute values, in place.

    marc2bibframe2 occasionally synthesises BCP-47 tags of the form
    ``<lang>-`` (e.g. ``ru-``, ``uk-``) when it tries to combine a MARC
    008 publication-country code with the language code and the region
    lookup falls through (the country is present in 008 positions
    15-17 but not in the converter's MARC-country → BCP-47-region
    lookup table). The trailing-hyphen tag is **invalid BCP-47** and
    rdflib's RDF/XML parser raises ``ValueError`` on it, killing the
    whole conversion before any other recovery can fire.

    This sanitiser trims any trailing hyphen run on every ``xml:lang``
    so ``ru-`` becomes ``ru`` — a valid bare-language tag.  Discovered
    in the P-02 5k production-style run (7 of 5000 records, all
    Russian / Ukrainian sources, hit this).

    Returns the count of attributes rewritten.
    """
    fixed = 0
    for el in tree.iter():
        lang = el.get(_XML_LANG_ATTR)
        if lang is None:
            continue
        repaired = _TRAILING_DASH_RE.sub("", lang)
        if repaired != lang:
            el.set(_XML_LANG_ATTR, repaired)
            fixed += 1
    return fixed
