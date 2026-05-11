"""Plain dataclasses for the Sierra MARCXML export's row payloads.

The original ``helmet-sierra-data-tools`` repo used SQLAlchemy
declarative models (with relationships into Bib, Item, Volume, …) for
these. The export here doesn't load via the ORM — the streamed SQL
already does the joins and aggregates everything into JSON columns,
and the row processor uses these classes only as named-attribute
DTOs. Replacing the SQLAlchemy classes with frozen dataclasses drops
the metadata-registration imports (Bib, Item, link tables) the
original ORM needed and keeps this package's dependency surface tight
— just SQLAlchemy-async-engine (for streaming the SELECT) and
pymarc (for the MARC record building), no declarative metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subfield:
    """One MARC subfield as carried by the Sierra ``sierra_view.subfield`` view.

    The ``content`` may be ``None`` on legacy rows; the processor
    coerces to an empty string when it builds the pymarc Subfield.
    """

    tag: str
    content: str | None = None
    display_order: int | None = None


@dataclass
class Varfield:
    """One MARC variable field as carried by ``sierra_view.varfield``.

    Subfields are sorted by ``display_order`` before being handed to
    pymarc so the resulting MARCXML preserves cataloguer-intended
    ordering. ``field_content`` carries the raw control-field content
    when the varfield's ``marc_tag`` is < 010 (Sierra stores 001/003/
    005 here on older records).
    """

    id: int | None = None
    marc_tag: str | None = None
    marc_ind1: str | None = None
    marc_ind2: str | None = None
    field_content: str | None = None
    subfields: list[Subfield] = field(default_factory=list)


@dataclass(frozen=True)
class Leaderfield:
    """One MARC leader row as carried by ``sierra_view.leader_field``.

    Single-character code fields default to safe values (``'n'`` /
    ``'a'`` / ``'m'`` per MARC 21 minimal-record conventions) when the
    upstream column is NULL so the rendered Leader is always 24
    positions wide regardless of source-record completeness.
    """

    record_status_code: str | None = None
    record_type_code: str | None = None
    bib_level_code: str | None = None
    char_encoding_scheme_code: str | None = None
    encoding_level_code: str | None = None
    descriptive_cat_form_code: str | None = None
    multipart_level_code: str | None = None
    base_address: str | None = None


__all__ = ["Leaderfield", "Subfield", "Varfield"]
