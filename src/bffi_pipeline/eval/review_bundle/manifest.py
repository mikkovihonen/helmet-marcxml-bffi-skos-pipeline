"""Manifest schema for review bundles.

The manifest is the bundle's table of contents — it tells the HTML
reviewer (and the Python importer) which stages are present, where
their candidate JSONLs live in the zip, where the per-row MARCXML
sidecars are, and which schema version each stage carries.

The ``schema`` field on the manifest itself (``bffi-review-bundle/1``)
versions the manifest format. The per-stage ``schema`` field versions
each stage's row shape independently — so judges can iterate without
breaking contrib bundles in the wild.

Forward compatibility: unknown stage ``schema`` values are not a
parse error. They flow through the manifest into the HTML's stage
registry, which renders an "unsupported in this reviewer version"
placeholder tab. New stages can ship without forcing every cataloguer
to download a newer HTML on the same day.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Manifest format version. Bump this if the manifest's own structure
#: changes incompatibly (e.g. ``stages`` is renamed). Adding optional
#: fields does NOT require a bump — they're forward-compatible.
MANIFEST_SCHEMA: Final[str] = "bffi-review-bundle/1"


class StageEntry(BaseModel):
    """One stage's worth of review material inside a bundle.

    ``schema_`` is the versioned dispatch key — the Python
    :data:`STAGE_REGISTRY` and the HTML's ``STAGE_SURFACES`` both
    index on it. ``marc_dir`` is optional: stages whose review
    doesn't benefit from MARC context (e.g. M9 picker, which is
    more about KANTO candidate JSON) can omit it.

    ``marc_missing`` lists the candidate bib ids whose MARCXML file
    wasn't found in the source directory at bundle-build time — the
    HTML uses this to render a "no MARCXML available" stub for those
    rows rather than letting the cataloguer think the MARC viewer
    is broken.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    candidates: str = Field(min_length=1)
    marc_dir: str | None = None
    schema_: str = Field(alias="schema", min_length=1)
    marc_missing: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    """The bundle's ``manifest.json`` contents.

    Built by :func:`build_bundle`, consumed by :func:`read_bundle` and
    by the HTML reviewer (which parses it client-side). Round-trips
    JSON exactly: written via ``model_dump(by_alias=True)`` and read
    via ``model_validate(json_dict)``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_: str = Field(alias="schema", default=MANIFEST_SCHEMA)
    created: str
    operator: str
    stages: list[StageEntry]
    #: Cataloguer side adds these when exporting the results zip. Absent
    #: on the operator-built input bundle; present on the results
    #: bundle that comes back. Both sides round-trip through the same
    #: ``Manifest`` model so the import side can read either shape.
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    #: P-47: true when the bundle includes the marc-roundtrip review
    #: directory. The HTML reviewer reads this flag to decide whether
    #: to add the "MARC round-trip" tab. Absent (false) on older
    #: bundles built before P-47 — graceful degrade, no tab.
    marc_roundtrip_present: bool = False

    @classmethod
    def new(cls, *, operator: str, stages: list[StageEntry]) -> Manifest:
        """Construct a manifest with ``created`` set to the current
        UTC timestamp. Used by the operator-side build path; the
        cataloguer's results manifest mirrors the input plus the
        reviewer-side ``reviewed_at`` field via direct construction.
        """
        return cls(
            schema=MANIFEST_SCHEMA,
            created=datetime.now(UTC).isoformat(),
            operator=operator,
            stages=stages,
        )
