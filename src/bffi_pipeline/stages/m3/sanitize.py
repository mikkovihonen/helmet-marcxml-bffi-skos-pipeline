"""M3 source-graph pre-CONSTRUCT sanitization helpers.

Two byte-level repairs that run before the SPARQL CONSTRUCT so the
CONSTRUCT doesn't trip over malformed source-graph terms:

- :func:`_sanitize_uri_whitespace` — strips and percent-encodes
  whitespace in URIRefs so rdflib can serialize the output as Turtle.
- :func:`_sanitize_date_literals` — drops the typed datatype from
  date literals whose lexical form doesn't parse, so the downstream
  M8 merge load doesn't crash on cataloguer-supplied placeholders
  like ``'19  -  -  T00:00:00'``.

P-38 Phase B: extracted from m3/runner.py to keep the runner focused
on the conversion orchestration. No logic change — moves only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Final

from rdflib import Graph, Literal, URIRef
from rdflib.term import Node

_WHITESPACE_PERCENT_ENCODE: Final[dict[str, str]] = {
    " ": "%20",
    "\t": "%09",
    "\n": "%0A",
    "\r": "%0D",
}


def _sanitize_uri(uri: str) -> str:
    """Strip leading/trailing whitespace from a URI string and percent-
    encode any remaining internal whitespace.

    Cataloguer-supplied ``$0`` values occasionally carry stray
    whitespace (trailing newlines, embedded spaces from two IDs
    accidentally concatenated). rdflib refuses to serialize those as
    N3/Turtle. Stripping is safe for leading/trailing — the URI was
    typo'd, not semantically different. Internal whitespace gets
    percent-encoded so the URI remains lexically valid and auditable
    rather than dropped silently.
    """
    stripped = uri.strip()
    if not any(ws in stripped for ws in _WHITESPACE_PERCENT_ENCODE):
        return stripped
    result = stripped
    for ws, encoded in _WHITESPACE_PERCENT_ENCODE.items():
        result = result.replace(ws, encoded)
    return result


def _sanitize_uri_whitespace(graph: Graph) -> int:
    """Rewrite URIRef terms in ``graph`` so none carry literal whitespace.

    Walks every position (subject, predicate, object) and rebuilds the
    affected triples in place. Returns the number of distinct URIs
    rewritten — callers can log this if they want to surface cataloguer
    data-quality counts.
    """
    rewrites: dict[URIRef, URIRef] = {}
    for term in set(graph.all_nodes()):
        if not isinstance(term, URIRef):
            continue
        sanitized = _sanitize_uri(str(term))
        if sanitized != str(term):
            rewrites[term] = URIRef(sanitized)
    # rdflib's predicates aren't returned by all_nodes(); walk them too.
    for _s, p, _o in graph:
        if isinstance(p, URIRef) and p not in rewrites:
            sanitized = _sanitize_uri(str(p))
            if sanitized != str(p):
                rewrites[p] = URIRef(sanitized)
    if not rewrites:
        return 0
    triples_to_replace: list[tuple[tuple[Node, Node, Node], tuple[Node, Node, Node]]] = []
    for s, p, o in graph:
        new_s = rewrites.get(s, s) if isinstance(s, URIRef) else s
        new_p = rewrites.get(p, p) if isinstance(p, URIRef) else p
        new_o = rewrites.get(o, o) if isinstance(o, URIRef) else o
        if (new_s, new_p, new_o) != (s, p, o):
            triples_to_replace.append(((s, p, o), (new_s, new_p, new_o)))
    for old, new in triples_to_replace:
        graph.remove(old)
        graph.add(new)
    return len(rewrites)


_XSD_DATETIME: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#dateTime")
_XSD_DATE: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#date")
_XSD_GYEAR: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#gYear")
_XSD_GYEAR_MONTH: Final[URIRef] = URIRef("http://www.w3.org/2001/XMLSchema#gYearMonth")

#: XSD datatypes that rdflib coerces into Python ``datetime``/``date``
#: at load time. A bad lexical form (cataloguer-supplied
#: ``'19  -  -  T00:00:00'``, etc.) raises ValueError during
#: coercion — and crashes the downstream merge load. Strip the
#: datatype on parse failure so the literal survives as plain text.
_DATE_DATATYPES: Final[tuple[URIRef, ...]] = (
    _XSD_DATETIME,
    _XSD_DATE,
    _XSD_GYEAR,
    _XSD_GYEAR_MONTH,
)

_GYEAR_LENGTH: Final[int] = 4
_GYEAR_MONTH_LENGTH: Final[int] = 7
_MAX_MONTH: Final[int] = 12


def _gyear_month_is_valid(lexical: str) -> bool:
    s = lexical.strip()
    if len(s) != _GYEAR_MONTH_LENGTH or s[_GYEAR_LENGTH] != "-":
        return False
    year, month = s[:_GYEAR_LENGTH], s[_GYEAR_LENGTH + 1 :]
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= _MAX_MONTH


def _datetime_is_valid(lexical: str) -> bool:
    try:
        datetime.fromisoformat(lexical)
    except ValueError:
        return False
    return True


def _date_is_valid(lexical: str) -> bool:
    try:
        date.fromisoformat(lexical)
    except ValueError:
        return False
    return True


def _gyear_is_valid(lexical: str) -> bool:
    s = lexical.strip()
    return len(s) == _GYEAR_LENGTH and s.isdigit()


_DATE_VALIDATORS: Final[dict[URIRef, Callable[[str], bool]]] = {
    _XSD_DATETIME: _datetime_is_valid,
    _XSD_DATE: _date_is_valid,
    _XSD_GYEAR: _gyear_is_valid,
    _XSD_GYEAR_MONTH: _gyear_month_is_valid,
}


def _is_parseable_date(lexical: str, datatype: URIRef) -> bool:
    """Return True iff ``lexical`` is a valid form for ``datatype``.

    Per-type validators in :data:`_DATE_VALIDATORS`. Unknown datatypes
    pass through (we don't know how to validate them; rdflib's own
    coercion will catch any issues).
    """
    validator = _DATE_VALIDATORS.get(datatype)
    return True if validator is None else validator(lexical)


def _sanitize_date_literals(graph: Graph) -> int:
    """Strip the typed datatype from date literals whose lexical form
    doesn't parse — keeps the value visible as plain text and stops
    downstream rdflib loads from crashing on the malformed record.

    Returns the count of literals stripped, for operator visibility.
    Cataloguer-supplied placeholders like ``'19  -  -  T00:00:00'``
    are the typical trigger (likely a date-not-yet-entered marker).
    """
    rewrites: list[tuple[tuple[Node, Node, Node], tuple[Node, Node, Node]]] = []
    for s, p, o in graph:
        if not isinstance(o, Literal):
            continue
        if o.datatype is None or o.datatype not in _DATE_DATATYPES:
            continue
        lexical = str(o)
        if _is_parseable_date(lexical, o.datatype):
            continue
        plain = Literal(lexical)
        rewrites.append(((s, p, o), (s, p, plain)))
    for old, new in rewrites:
        graph.remove(old)
        graph.add(new)
    return len(rewrites)
