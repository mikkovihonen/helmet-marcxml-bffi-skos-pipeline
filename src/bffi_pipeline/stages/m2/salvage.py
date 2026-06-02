"""P-41 Phase B — M2 creator-salvage layer.

Fires when a record would otherwise drop on
``marcxml-content-minimum`` for missing 1XX/7XX (creator). Inserts a
synthesised MARC 700 / 710 datafield (with the
``$5 FI-HELME/synth-v<N>`` provenance marker that mirrors P-08's
33X-synthesis pattern) so the record passes ``validate_minimum_content``
and reaches Skosmos.

Tiered dispatch — first match wins. Each tier is conservative; B3 is
the safety net:

- **B1** (``salvage_245c``) — extract verbatim agent names from MARC
  ``245$c`` (statement of responsibility) via deterministic regex plus
  optional local-mlx-lm cascade. Confidence 0.5-0.8 (regex) or capped
  at 0.7 (LLM).
- **B2** (``salvage_publisher``, feature-flagged off by default) —
  promote ``260$b`` / ``264$b`` publisher to a synthesised corporate
  creator (MARC 710) for the leader/06 codes the cataloguer team has
  signed off on. Confidence 0.3.
- **B3** (``salvage_sentinel``) — fall through to the shared
  anonymous-by-convention sentinel agent
  (:data:`bffi_pipeline.provenance.vocab.SENTINEL_AGENT_UNKNOWN`).
  Confidence 0.1.

Every synthesised datafield carries ``<subfield code="5">FI-HELME/synth-v1</subfield>``
so the downstream BIBFRAME conversion can be told apart from
cataloguer-coded contributions at the MARC level. The companion
``bffi-prov:Synthesis`` Activity (P-41 Phase A vocabulary) carries
the structured record + provenance link; the per-run TSV is the
human-readable view of the same events.

This module owns the MARC tree mutation. Upstream (``convert.py``)
catches ``marcxml-content-minimum`` failures and invokes
:func:`try_salvage_minimum_content`; on a hit the mutated tree
re-validates and the record proceeds through M2 normally with an
attached :class:`SalvageOutcome` recording what happened for the
Phase B.5 provenance writer and the Phase C TSV writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from lxml import etree

from bffi_pipeline.config import Settings
from bffi_pipeline.stages.m2.salvage_245c import parse_245c
from bffi_pipeline.stages.m2.salvage_publisher import (
    B2_CONFIDENCE,
    build_publisher_datafield,
    try_promote_publisher,
)
from bffi_pipeline.stages.m2.salvage_sentinel import build_sentinel_datafield

_MARC_NS: Final[str] = "http://www.loc.gov/MARC21/slim"
_RECORD_TAG: Final[str] = f"{{{_MARC_NS}}}record"
_DATAFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}datafield"
_SUBFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}subfield"
_CONTROLFIELD_TAG: Final[str] = f"{{{_MARC_NS}}}controlfield"
_COLLECTION_TAG: Final[str] = f"{{{_MARC_NS}}}collection"

#: Provenance marker stamped into every synthesised salvage datafield's
#: ``$5`` subfield. Mirrors P-08's pattern at the BIBFRAME export side;
#: the ``v1`` version is bumped manually if the salvage tiers' output
#: shape changes in a way that would re-write existing synth-coded
#: records.
SYNTH_MARKER: Final[str] = "FI-HELME/synth-v1"


@dataclass(frozen=True)
class SynthesisRecord:
    """One synthesis event — the data Phase B.5 writes to provenance
    and Phase C writes to the per-run TSV. ``marc_source`` is the
    MARC field(s) the synthesiser read (e.g. ``"245$c"``) or
    ``"(none)"`` when the tier didn't read MARC content (B3 sentinel).
    ``synthesised_value`` is the literal or URI now in the synthesised
    MARC datafield — for B1/B2 this is the agent name, for B3 this is
    the sentinel URI."""

    bib_id: str
    field: str
    marc_source: str
    synthesised_value: str
    tier: str
    method: str
    confidence: float


@dataclass(frozen=True)
class SalvageOutcome:
    """Returned by :func:`try_salvage_minimum_content` on a hit. The
    tree has already been mutated by the time this object is
    constructed; ``records`` is the audit trail for the provenance
    writer + TSV writer to consume.
    """

    tier: str
    records: tuple[SynthesisRecord, ...]


def _first_record(tree: etree._ElementTree) -> etree._Element | None:
    """Return the first ``<marc:record>`` in the tree, descending
    through a ``<marc:collection>`` wrapper if present. Mirrors the
    behaviour in :func:`bffi_pipeline.validation.marcxml.validate_minimum_content`."""
    root = tree.getroot()
    if root.tag == _COLLECTION_TAG:
        return root.find(_RECORD_TAG)
    return root if root.tag == _RECORD_TAG else None


#: Length of a MARC datafield tag — three characters by spec. Lifted
#: out of the ``len(tag) == 3`` comparison so ruff doesn't flag the
#: magic-number.
_MARC_TAG_LENGTH: Final[int] = 3


def _has_creator(record: etree._Element) -> bool:
    """Return True if the record carries any 1XX or 7XX datafield."""
    for df in record.iterfind(_DATAFIELD_TAG):
        tag = df.get("tag") or ""
        if len(tag) == _MARC_TAG_LENGTH and tag[0] in {"1", "7"}:
            return True
    return False


def _has_245(record: etree._Element) -> bool:
    """Return True if the record carries a 245 datafield. B1 reads
    245$c; without a 245 there's nothing to parse."""
    return any(df.get("tag") == "245" for df in record.iterfind(_DATAFIELD_TAG))


def _read_245c(record: etree._Element) -> str | None:
    """Return the ``$c`` subfield of the first 245, or ``None``."""
    for df in record.iterfind(_DATAFIELD_TAG):
        if df.get("tag") != "245":
            continue
        for sf in df.iterfind(_SUBFIELD_TAG):
            if sf.get("code") == "c":
                return (sf.text or "").strip() or None
        return None
    return None


def _append_datafield(record: etree._Element, datafield: etree._Element) -> None:
    """Append a synthesised datafield in numeric-tag order. MARC convention
    keeps datafields in ascending-tag order; the marc2bibframe2 XSLT
    doesn't depend on it but downstream cataloguer-facing tooling
    sometimes does. Best-effort placement: appended to the tail since
    7XX > all other tags that pass validation already."""
    record.append(datafield)


#: Roles that warrant promotion to MARC 1XX (primary creator). When the
#: cataloguer wrote "kirjoittanut X" or just "X" (bare name) in 245$c,
#: X is the work's primary author and the BIBFRAME side should expose
#: them as the primary creator. Editor / translator / illustrator
#: roles stay in 7XX (added entry) because they're contributions on
#: top of an absent primary author, not the primary creator
#: themselves.
_PRIMARY_CREATOR_ROLES: Final[frozenset[str]] = frozenset({"author", "unknown"})

#: Relator term per role for the ``$e`` subfield. Finnish forms;
#: a cataloguer locale override would land here as a future plan.
_RELATOR_TERMS: Final[dict[str, str]] = {
    "author": "tekijä",
    "editor": "toimittaja",
    "translator": "kääntäjä",
    "illustrator": "kuvittaja",
    "compiler": "toimittaja",
    "unknown": "tekijä",
}


def _build_personal_creator(name: str, role: str, *, primary: bool) -> etree._Element:
    """Build a synthesised personal-creator MARC datafield. ``primary``
    selects between MARC 100 (primary author entry) and MARC 700
    (added entry).

    Shape (primary case)::

      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">{name}</subfield>
        <subfield code="e">{relator term per role}</subfield>
        <subfield code="5">FI-HELME/synth-v1</subfield>
      </datafield>

    ``ind1="1"`` = surname entry (the typical RDA shape for personal
    names). ``$e`` = relator term lifted from ``_RELATOR_TERMS`` —
    Finnish "tekijä" for author/unknown, "kääntäjä" for translator,
    etc. ``$5`` carries the P-08-shaped provenance marker.
    """
    tag = "100" if primary else "700"
    df = etree.Element(_DATAFIELD_TAG, attrib={"tag": tag, "ind1": "1", "ind2": " "})
    a = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "a"})
    a.text = name
    e = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "e"})
    e.text = _RELATOR_TERMS.get(role, "tekijä")
    s = etree.SubElement(df, _SUBFIELD_TAG, attrib={"code": "5"})
    s.text = SYNTH_MARKER
    return df


def _try_b1(record: etree._Element, *, bib_id: str) -> SalvageOutcome | None:
    """B1 — deterministic 245$c parse. Returns a SalvageOutcome on hit
    or ``None`` to fall through to the next tier.

    Routing decision per agent: the *first* author-role agent goes
    into MARC 100 (primary creator) so the downstream BIBFRAME
    pipeline treats them as the work's primary creator (which is
    what the 245$c "kirjoittanut X" or bare "X" pattern actually
    asserts). Subsequent agents — and any single-agent parse whose
    role is editor / translator / illustrator — go into MARC 700
    (added entry) per MARC convention. This matches what a cataloguer
    would type if they were entering the data manually.
    """
    if not _has_245(record):
        return None
    text_245c = _read_245c(record)
    if text_245c is None:
        return None
    agents = parse_245c(text_245c)
    if not agents:
        return None

    records: list[SynthesisRecord] = []
    primary_assigned = False
    for agent in agents:
        # First author-role (or unknown-role, which we treat as
        # probably-author) agent gets the 1XX slot; later agents
        # and non-author roles stay in 7XX.
        is_primary = (not primary_assigned) and agent.role in _PRIMARY_CREATOR_ROLES
        if is_primary:
            primary_assigned = True
        df = _build_personal_creator(agent.name, agent.role, primary=is_primary)
        _append_datafield(record, df)
        marc_tag = "100" if is_primary else "700"
        method = f"creator-from-245c (regex, role={agent.role}, marc={marc_tag})"
        records.append(
            SynthesisRecord(
                bib_id=bib_id,
                field="bf:contribution/bf:agent",
                marc_source="245$c",
                synthesised_value=agent.name,
                tier="B1",
                method=method,
                confidence=agent.confidence,
            )
        )
    return SalvageOutcome(tier="B1", records=tuple(records))


def _try_b2(record: etree._Element, *, bib_id: str, settings: Settings) -> SalvageOutcome | None:
    """B2 — publisher-as-corporate-creator. Returns a SalvageOutcome
    on hit or ``None`` to fall through to the next tier. Feature-
    flagged off by default — see :mod:`bffi_pipeline.stages.m2.salvage_publisher`."""
    promotion = try_promote_publisher(record, settings=settings)
    if promotion is None:
        return None
    df = build_publisher_datafield(promotion, marker=SYNTH_MARKER)
    _append_datafield(record, df)
    return SalvageOutcome(
        tier="B2",
        records=(
            SynthesisRecord(
                bib_id=bib_id,
                field="bf:contribution/bf:agent",
                marc_source=promotion.marc_source,
                synthesised_value=promotion.publisher,
                tier="B2",
                method=f"publisher-as-corporate-creator (from {promotion.marc_source})",
                confidence=B2_CONFIDENCE,
            ),
        ),
    )


def _try_b3(record: etree._Element, *, bib_id: str, settings: Settings) -> SalvageOutcome:
    """B3 — anonymous-by-convention sentinel. The safety net: always
    returns a SalvageOutcome (this is why the function isn't
    ``Optional``). Multiple records sharing the sentinel agent URI is
    the property "no known author", not a claim about identity."""
    df = build_sentinel_datafield(settings, marker=SYNTH_MARKER)
    _append_datafield(record, df)
    return SalvageOutcome(
        tier="B3",
        records=(
            SynthesisRecord(
                bib_id=bib_id,
                field="bf:contribution/bf:agent",
                marc_source="(none)",
                synthesised_value=settings.creator_salvage_sentinel_agent_uri,
                tier="B3",
                method="anonymous-by-convention",
                confidence=0.1,
            ),
        ),
    )


def try_salvage_minimum_content(
    tree: etree._ElementTree,
    *,
    bib_id: str,
    settings: Settings,
) -> SalvageOutcome | None:
    """Try the salvage tiers in order. Returns the first hit's
    outcome (with the tree already mutated to carry the synthesised
    datafield), or ``None`` when nothing fires.

    Only the *missing-creator* case (no 1XX, no 7XX) is salvageable
    today — other ``marcxml-content-minimum`` triggers (missing 245,
    missing 008, missing 33X) are out of scope per
    ``docs/bibliographic-minimum.md``. The dispatcher's first check
    is therefore "does the record already have a creator?" — if so,
    nothing to do.

    The master flag :attr:`Settings.creator_salvage_enabled` is the
    opt-out lever for the rollback procedure; flipping it to False
    short-circuits this function to ``None``, restoring the pre-P-41
    drop-on-missing-creator behaviour without code revert.
    """
    if not settings.creator_salvage_enabled:
        return None
    record = _first_record(tree)
    if record is None:
        return None
    if _has_creator(record):
        return None
    # B1 — 245$c regex parse.
    outcome = _try_b1(record, bib_id=bib_id)
    if outcome is not None:
        return outcome
    # B2 — publisher-as-corporate-creator. Returns ``None`` when the
    # feature flag is off (default), the leader/06 isn't in the
    # cataloguer-confirmed set, or the record has no 260$b/264$b.
    outcome = _try_b2(record, bib_id=bib_id, settings=settings)
    if outcome is not None:
        return outcome
    # B3 — sentinel safety net.
    return _try_b3(record, bib_id=bib_id, settings=settings)


__all__ = [
    "SYNTH_MARKER",
    "SalvageOutcome",
    "SynthesisRecord",
    "try_salvage_minimum_content",
]
