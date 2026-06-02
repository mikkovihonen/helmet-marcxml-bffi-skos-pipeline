"""P-41 Phase B3 — anonymous-by-convention sentinel agent.

When the cascade can't extract a personal name from 245$c (B1 fails)
and the publisher-promotion tier is off or doesn't apply (B2 skipped),
the record falls through to the shared sentinel agent
:data:`bffi_pipeline.provenance.vocab.SENTINEL_AGENT_UNKNOWN`. Multiple
records sharing the sentinel URI is intentional: it expresses the
property "no known author," not the claim of identity. Downstream
stages (M5/M6/M8/M9) honour the ``bffi:syntheticSentinel`` flag on
the agent and skip keying / reconciling on it — see the exclude-rule
wiring added in P-41 Phase B.6.

This module builds the synthesised MARC 710 datafield that links the
record to the sentinel via ``$0`` (authority record URI). The 710
shape mirrors what a cataloguer would type for a corporate body with
an authority binding, so the marc2bibframe2 XSLT downstream emits a
clean ``bf:contribution`` block without a custom code path.

The sentinel Agent RDF block itself (``rdf:type bf:Agent``,
``skos:prefLabel`` per language, ``bffi:syntheticSentinel "true"``)
is emitted ONCE PER RUN by ``stages/m2/salvage.py`` — it's a
graph-level construct, not a per-record one. The MARC 710 emitted by
this module is the per-record link.
"""

from __future__ import annotations

from typing import Final

from lxml import etree

from bffi_pipeline.config import Settings

_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_DATAFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}datafield"
_SUBFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}subfield"


def build_sentinel_datafield(settings: Settings, *, marker: str) -> etree._Element:
    """Return a fresh ``<marc:datafield>`` element linking the record
    to the sentinel agent.

    Shape::

      <datafield tag="710" ind1="2" ind2=" ">
        <subfield code="a">Tekijä tuntematon</subfield>
        <subfield code="0">http://urn.fi/URN:NBN:fi:bib:agent:unknown</subfield>
        <subfield code="e">tekijä</subfield>
        <subfield code="5">FI-HELME/synth-v1</subfield>
      </datafield>

    ``tag="710"`` = corporate-body added entry (RDA practice for
    abstract / corporate-agent placeholders; an anonymous-author
    sentinel is, technically, neither personal nor corporate, but
    710 is the conventional pseudo-corporate slot for placeholder
    authority records). ``ind1="2"`` = name in direct order.

    ``$a`` carries the Finnish-default label so a cataloguer looking
    at the MARC record sees an immediately-recognisable
    "Tekijä tuntematon"; the multi-language labels live on the
    sentinel Agent's RDF prefLabels via the once-per-run emit (see
    ``salvage.py`` and the M3 → BFFI conversion). ``$0`` is the
    authority binding the marc2bibframe2 XSLT consumes to emit
    ``bf:agent <URI>``. ``$e`` is the relator term. ``$5`` is the
    provenance marker mirroring P-08's pattern.
    """
    df = etree.Element(_DATAFIELD_TAG, attrib={"tag": "710", "ind1": "2", "ind2": " "})
    a = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "a"})
    a.text = settings.creator_salvage_sentinel_label_fi
    z = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "0"})
    z.text = settings.creator_salvage_sentinel_agent_uri
    e = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "e"})
    e.text = "tekijä"
    s = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "5"})
    s.text = marker
    return df


__all__ = ["build_sentinel_datafield"]
