"""M2-post: source-MARC-field provenance enrichment of BIBFRAME output.

Runs immediately after M2's marc2bibframe2 conversion. For each
per-record BIBFRAME RDF/XML file, walks the source MARCXML in document
order, computes a deterministic ``<bib_id>:<tag>:<ordinal>`` token per
source field instance, correlates each token to the BIBFRAME entities
it produced (via ``bflc:marcKey`` content matching in Phase A; flat
literal matching in Phase B; SubjectLink reification in Phase C), and
emits ``<entity> bffi-prov:fromMarcField "<token>"`` triples back into
the BIBFRAME RDF.

The triples ride through M3 / M8 / M9 unchanged, materialise on every
derived BFFI entity, and are read by the round-trip converter to stamp
``$9 src=<token>`` on each reconstructed datafield. The diff comparator
parses the same source MARCXML, computes the same tokens, and pairs
recon-to-source rows by token identity (P-50 redesign — supersedes the
P-48 ``<tag>-<rank>`` rank-bucketing scheme).

See ``docs/plans/backlog/p-50-source-field-provenance.md``.
"""

from __future__ import annotations

from bffi_pipeline.stages.m2_post.runner import M2PostSummary, run
from bffi_pipeline.stages.m2_post.token import (
    MarcFieldIndex,
    SourceMarcField,
    compute_token,
    parse_token,
)

__all__ = [
    "M2PostSummary",
    "MarcFieldIndex",
    "SourceMarcField",
    "compute_token",
    "parse_token",
    "run",
]
