"""Vendored schemas.

* ``MARC21slim.xsd`` — the official MARC 21 XML Schema, fetched from the Library of
  Congress at ``https://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd``
  on 2026-05-08. The file is self-contained (no XSD imports). Boundary 1
  validation in :mod:`bffi_pipeline.validation.marcxml` reads it via a cached
  ``lxml.etree.XMLSchema``.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def marc21slim_xsd_path() -> Path:
    """Return the on-disk path of the vendored ``MARC21slim.xsd``."""
    resource = files(__package__).joinpath("MARC21slim.xsd")
    return Path(str(resource))
