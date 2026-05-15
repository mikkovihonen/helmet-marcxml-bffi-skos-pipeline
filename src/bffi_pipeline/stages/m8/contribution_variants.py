"""M8 F2 binding pass — attach ``skos:altLabel`` for transliteration
variants of canonical agents.

F2 of P-35 (M3 cascade follow-ups) persists transliteration variants
to a sidecar JSONL (``<BFFI_DATA_DIR>/contrib-variants.jsonl``). After
M8 mints canonical Works, this pass joins each sidecar row against
the canonical map, walks every Contribution on the canonical Work
(primary + Expressions), and adds ``skos:altLabel "<variant>"`` to
every agent whose ``rdfs:label`` matches the claim's canonical label.

The two label forms then resolve to the same agent identity for M9
reconciliation — eliminating a class of false-negative misses where
the transliteration form (``Tsaikovskij``) and the canonical form
(``Чайковский``) would otherwise reconcile to different KANTO URIs.

P-38 Phase B: extracted from m8/runner.py to keep the runner focused
on the mint orchestration. No logic change — moves only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, URIRef
from rdflib.term import Node

from bffi_pipeline.contrib_variants import load_variant_claims
from bffi_pipeline.provenance import vocab as V

if TYPE_CHECKING:
    from bffi_pipeline.stages.m8.runner import CanonicalEntry


def _apply_contrib_variants(
    g: Graph,
    *,
    variants_sidecar_path: Path,
    canonical_entries: list[CanonicalEntry],
) -> int:
    """Read F2 ``contrib-variants.jsonl`` and attach ``skos:altLabel``
    on the canonical agent each claim points at. Returns the number
    of altLabels added (zero when sidecar is absent or no claim
    matches).

    Matching: each claim's ``raw_work_uri`` rolls up to the canonical
    Work via :class:`CanonicalEntry`. On every Expression of that
    canonical Work, every agent whose ``rdfs:label`` equals the
    claim's ``canonical_label`` gets ``skos:altLabel <variant_label>``.
    Multiple matches per claim are fine (an agent may be shared
    across Expressions); duplicates are deduplicated by rdflib's
    set-semantics graph add.

    Idempotent: re-running on the same sidecar adds the same
    triples, which is a no-op; canonical.ttl stays byte-stable.
    """
    claims = load_variant_claims(variants_sidecar_path)
    if not claims:
        return 0

    raw_to_canonical: dict[str, str] = {}
    for entry in canonical_entries:
        for raw in entry.raw_work_uris:
            raw_to_canonical[raw] = entry.canonical_work_uri

    added = 0
    for claim in claims:
        canonical_uri = raw_to_canonical.get(claim.raw_work_uri)
        if canonical_uri is None:
            continue
        canonical_node = URIRef(canonical_uri)
        canonical_lit = Literal(claim.canonical_label)
        variant_lit = Literal(claim.variant_label)
        # Walk every Contribution attached to the canonical Work
        # itself (primary contributions — MARC 100$a) AND every
        # Contribution attached to its Expressions (non-primary —
        # MARC 700$a). Both shapes can carry the canonical agent the
        # cascade matched against; missing either path drops a
        # legitimate variant binding.
        contrib_subjects: list[Node] = list(g.objects(canonical_node, V.BFFI.contribution))
        for expr in g.objects(canonical_node, V.BFFI.hasExpression):
            contrib_subjects.extend(g.objects(expr, V.BFFI.contribution))
        for contrib in contrib_subjects:
            for agent in g.objects(contrib, V.BFFI.agent):
                if (agent, V.RDFS.label, canonical_lit) not in g:
                    continue
                if (agent, V.SKOS.altLabel, variant_lit) in g:
                    continue
                g.add((agent, V.SKOS.altLabel, variant_lit))
                added += 1
    return added
