"""Low-level RDF / URI helpers shared across packages.

Functions here are deliberately small, dependency-free, and free of
domain semantics — they're toolkit utilities that any package in the
pipeline can import without creating layering cycles.

Keep this module thin. Domain-specific routines (URI minting,
ontology walks, conversion-stage logic) belong in their respective
packages — only true cross-cutting helpers land here.
"""

from __future__ import annotations

from rdflib import URIRef


def local_name(uri: URIRef) -> str:
    """Last path / fragment component of ``uri``, useful for printing.

    Splits on ``#`` first (XML-style fragment), then ``/`` (URL path),
    then ``:`` (CURIE-style local name) — whichever appears in the
    URI string. Returns the rightmost component.

    Examples:

    >>> local_name(URIRef("http://id.loc.gov/ontologies/bibframe/Isbn"))
    'Isbn'
    >>> local_name(URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:BibframeWork"))
    'BibframeWork'
    >>> local_name(URIRef("http://example.org/x#Frag"))
    'Frag'
    """
    text = str(uri)
    for sep in ("#", "/", ":"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    return text
