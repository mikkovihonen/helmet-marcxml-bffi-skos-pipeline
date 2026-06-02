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
from bffi_pipeline.stages.m2.salvage_245c import ParsedAgent, parse_245c
from bffi_pipeline.stages.m2.salvage_245c_llm import SalvageExtractor
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

#: Defence-in-depth (2026-06-02): name particles whose presence
#: indicates that the simple "last token is surname" heuristic
#: can't be trusted. ``Robin de Smeet`` → MARC convention is
#: ``Smeet, Robin de`` (particle to the right of the forename),
#: not ``Smeet, Robin`` or ``de Smeet, Robin``. Detecting particles
#: reliably across languages is a hard cataloguing problem the
#: project doesn't try to solve in B1; instead, names containing
#: any of these particles skip the surname-first reformat and stay
#: verbatim. Conservative — the M5 blocking key will pick the
#: wrong surname token on these records, but synthesis with a
#: known-correct verbatim name is more honest than guessing.
_NAME_PARTICLES: Final[frozenset[str]] = frozenset(
    {
        # Germanic.
        "von",
        "van",
        "der",
        "den",
        "te",
        # Romance.
        "de",
        "del",
        "della",
        "di",
        "da",
        "do",
        "dos",
        "du",
        "el",
        "la",
        "le",
        # Arabic.
        "al",
        "el-",
        "abu",
        "ibn",
        # Scandinavian.
        "af",
    }
)


def _to_surname_first(name: str) -> str | None:
    """Reformat ``"First Last"`` → ``"Last, First,"`` (MARC 100$a
    convention) when the algorithm is unambiguous. Returns the
    reformatted string on hit, or ``None`` when the name should
    stay verbatim.

    Defence-in-depth (2026-06-02): B1 synthesises names verbatim
    from 245$c, which is first-last form. Cataloguer-typed MARC
    100$a is surname-first. The mismatch broke M5 blocking on the
    synthesised records' surname token (key picked the forename
    instead). Reformatting brings synthesised records into the
    same blocking-key namespace as cataloguer-typed ones.

    Conservative rules:

    - Skip if name already contains a comma (assume surname-first).
    - Skip if any token is in :data:`_NAME_PARTICLES` ("Robin de
      Smeet", "Hans von Goethe" — particles' placement is a
      cataloguing-policy judgement the algorithm can't make
      reliably).
    - Skip if name has fewer than 2 or more than 3 tokens. The
      3-token case ("Hans Christian Andersen") assumes
      Finnish/English/Swedish convention where the last token is
      the surname; languages that put the family name first
      (Hungarian, some Chinese transliterations) would be
      mis-reformatted. Per the 5 k bench: the corpus is dominantly
      first-last form and 2-3 tokens are the common shape; 4+
      tokens are usually compound names where the algorithm is
      unreliable.
    """
    name = name.strip()
    if "," in name:
        return None
    tokens = name.split()
    if not (_MIN_NAME_TOKENS <= len(tokens) <= _MAX_REFORMAT_TOKENS):
        return None
    if any(token.lower() in _NAME_PARTICLES for token in tokens):
        return None
    surname = tokens[-1]
    forename = " ".join(tokens[:-1])
    return f"{surname}, {forename},"


#: Lower bound for surname-first reformat token count — same as the
#: B1-regex minimum, surfaced here so both call sites stay aligned.
_MIN_NAME_TOKENS: Final[int] = 2

#: Upper bound for surname-first reformat. 4+ tokens are usually
#: compound titles or multi-part Asian/African names where the
#: last-token-is-surname heuristic is unreliable. The 3-token case
#: ("Hans Christian Andersen") IS reformatted because the corpus is
#: dominantly Western first-last names.
_MAX_REFORMAT_TOKENS: Final[int] = 3


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


def _build_records_for_b1_or_b1_llm(
    record: etree._Element,
    *,
    agents: list[ParsedAgent],
    bib_id: str,
    method_template: str,
    tier: str,
) -> SalvageOutcome:
    """Shared body for B1 regex + B1 LLM cascade hits — same routing
    rules (first author-role → MARC 100, rest → MARC 700), same
    SynthesisRecord shape; only the ``method`` tag and ``tier`` ID
    differ between the two paths.

    Names are reformatted from ``"First Last"`` to ``"Last, First,"``
    when :func:`_to_surname_first` returns non-None (see that
    function's docstring for the conservative rules). The method
    tag carries ``"+surname-first"`` when the reformat fires so the
    audit trail records what happened; the SynthesisRecord's
    ``synthesised_value`` matches the BIBFRAME 100$a/700$a output.
    """
    records: list[SynthesisRecord] = []
    primary_assigned = False
    for agent in agents:
        # First author-role (or unknown-role, which we treat as
        # probably-author) agent gets the 1XX slot; later agents
        # and non-author roles stay in 7XX.
        is_primary = (not primary_assigned) and agent.role in _PRIMARY_CREATOR_ROLES
        if is_primary:
            primary_assigned = True
        # Surname-first reformat where unambiguous.
        reformatted = _to_surname_first(agent.name)
        emitted_name = reformatted if reformatted is not None else agent.name
        df = _build_personal_creator(emitted_name, agent.role, primary=is_primary)
        _append_datafield(record, df)
        marc_tag = "100" if is_primary else "700"
        method = method_template.format(role=agent.role, marc=marc_tag)
        if reformatted is not None:
            method = method + ", surname-first"
        records.append(
            SynthesisRecord(
                bib_id=bib_id,
                field="bf:contribution/bf:agent",
                marc_source="245$c",
                synthesised_value=emitted_name,
                tier=tier,
                method=method,
                confidence=agent.confidence,
            )
        )
    return SalvageOutcome(tier=tier, records=tuple(records))


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
    return _build_records_for_b1_or_b1_llm(
        record,
        agents=agents,
        bib_id=bib_id,
        method_template="creator-from-245c (regex, role={role}, marc={marc})",
        tier="B1",
    )


def _try_b1_llm(
    record: etree._Element,
    *,
    bib_id: str,
    extractor: SalvageExtractor,
) -> SalvageOutcome | None:
    """B1 LLM cascade — runs the local-mlx-lm cascade against 245$c
    when the deterministic regex tier (:func:`_try_b1`) returned no
    agents. The extractor's verbatim-substring post-processor has
    already rejected any hallucinated names by the time the agents
    list reaches us; confidence is capped at
    :data:`LLM_CONFIDENCE_CAP <bffi_pipeline.stages.m2.salvage_245c_llm.LLM_CONFIDENCE_CAP>`
    (0.7) so synthesised creators always land in M6's fallback gating
    tier per P-16.

    Routing rules are identical to B1 regex (first author/unknown →
    MARC 100, rest → MARC 700); only the ``tier`` ID and the method
    tag differ.
    """
    if not _has_245(record):
        return None
    text_245c = _read_245c(record)
    if text_245c is None:
        return None
    agents = extractor.extract(c_subfield=text_245c)
    if not agents:
        return None
    return _build_records_for_b1_or_b1_llm(
        record,
        agents=agents,
        bib_id=bib_id,
        method_template="creator-from-245c (llm, role={role}, marc={marc})",
        tier="B1-LLM",
    )


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
    llm_extractor: SalvageExtractor | None = None,
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

    Tier order is **B1 regex → B1 LLM cascade → B2 publisher → B3
    sentinel**. The LLM cascade fires only when
    ``Settings.creator_salvage_b1_llm_cascade_enabled`` is true AND
    an ``llm_extractor`` is supplied. Tests inject a
    :class:`StubSalvageExtractor`; production wires
    :class:`LangChainSalvageExtractor` via the M2 runner.

    The master flag :attr:`Settings.creator_salvage_enabled` is the
    opt-out lever for the rollback procedure; flipping it to False
    short-circuits this function to ``None``, restoring the pre-P-41
    drop-on-missing-creator behaviour without code revert.
    """
    if not settings.creator_salvage_enabled:
        return None
    record = _first_record(tree)
    if record is None or _has_creator(record):
        return None
    # Tier cascade. The first hit wins; B3 always returns a value, so
    # the cascade is guaranteed to terminate.
    # B1 — 245$c regex parse (cheap, deterministic).
    # B1-LLM — LLM cascade fallback (gated on the feature flag AND a
    #          concrete extractor; production wires one, tests inject
    #          a stub, ``None`` skips the tier).
    # B2 — publisher-as-corporate-creator (default-off feature flag).
    # B3 — anonymous-by-convention sentinel (always returns a value).
    outcome = _try_b1(record, bib_id=bib_id)
    if outcome is None and (
        settings.creator_salvage_b1_llm_cascade_enabled and llm_extractor is not None
    ):
        outcome = _try_b1_llm(record, bib_id=bib_id, extractor=llm_extractor)
    if outcome is None:
        outcome = _try_b2(record, bib_id=bib_id, settings=settings)
    if outcome is None:
        outcome = _try_b3(record, bib_id=bib_id, settings=settings)
    return outcome


__all__ = [
    "SYNTH_MARKER",
    "SalvageOutcome",
    "SynthesisRecord",
    "try_salvage_minimum_content",
]
