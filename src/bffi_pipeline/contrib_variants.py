"""Contributor-variant sidecar (F2): persistence of the M3 cascade's
``transliteration_of`` pointers.

When the M3 contributor-extraction cascade detects that a 245$c name
is a script / spelling variant of an agent already in 100/700 (the
Tšarka / Karttunen / Bridžet Kollinz patterns), it returns a
:class:`bffi_pipeline.contrib_extract_llm.ContribCandidate` with
``transliteration_of`` set. The post-process emitter intentionally
*doesn't* propagate these as new ``bffi:Contribution`` blocks — that
would duplicate the cataloguer's existing structured agent.

Instead, the cascade writes a row to this sidecar at
``<BFFI_DATA_DIR>/contrib-variants.jsonl``. Downstream, M8 reads the
sidecar after canonical Works are minted and attaches
``skos:altLabel "<variant_label>"`` on the canonical agent whose
``rdfs:label`` matches ``canonical_label``. Both forms then share
the same agent identity (and any future M9 KANTO binding) without
needing two distinct nodes in the graph.

Same JSONL convention as ``helmet-map.jsonl`` (M2) and
``embed-candidates.jsonl`` (M5). Pure data-handling here: schema +
append + load + dedup. No graph mutation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Default sidecar filename relative to ``BFFI_DATA_DIR``.
DEFAULT_SIDECAR_NAME: Final[str] = "contrib-variants.jsonl"


class ContribVariantClaim(BaseModel):
    """One row in ``contrib-variants.jsonl``.

    Schema is closed (``extra="forbid"``) so a typo in a future writer
    fails fast on load rather than silently introducing dead fields.
    All identifiers are strings; the JSONL stays human-readable for
    cataloguer review.
    """

    model_config = ConfigDict(extra="forbid")

    helmet_bib_id: str = Field(
        description="Helmet bib ID of the source MARC record (e.g. '1714651')."
    )
    raw_work_uri: str = Field(
        description=(
            "Raw bffi:Work URI minted by the M3 SPARQL CONSTRUCT "
            "(``http://urn.fi/URN:NBN:fi:bib:work:<sha1>``). M8's "
            "binding pass uses canonical-map.jsonl to roll this up "
            "to the canonical Work URI."
        ),
    )
    variant_label: str = Field(
        min_length=1,
        description=(
            "The variant form as it appears in 245$c (e.g. 'Anssi "
            "Karttunen'). Will become a ``skos:altLabel`` on the "
            "canonical agent."
        ),
    )
    canonical_label: str = Field(
        min_length=1,
        description=(
            "The 100/700 agent's ``rdfs:label`` that the cascade "
            "matched the variant against (e.g. 'Karttunen, Assi'). "
            "M8 finds the canonical agent by this label."
        ),
    )
    relator_code_hint: str | None = Field(
        default=None,
        description=(
            "Optional MARC relator code the LLM emitted alongside the "
            "transliteration pointer. Preserved for human audit; M8's "
            "binding pass does not use it (the canonical agent's role "
            "is already in the graph from the 100/700 pass-through)."
        ),
    )
    role_text_hint: str | None = Field(
        default=None,
        description="Optional cataloguer-language role marker the LLM saw in 245$c.",
    )
    rationale: str = Field(
        min_length=1,
        description="LLM rationale for the variant decision (audit trail).",
    )
    prompt_hash: str = Field(
        description="SHA-256 of the prompt that produced this decision (versioning)."
    )
    model_id: str = Field(description="Ollama model id, e.g. 'qwen3:8b-q4_K_M'.")
    decided_at: str = Field(description="ISO 8601 UTC timestamp of the cascade call.")


def append_variant_claims(path: Path, claims: Iterable[ContribVariantClaim]) -> int:
    """Append claims to ``path`` (creating it if needed). Returns count
    written. Each row is a single JSON object on its own line; no
    separator beyond the newline so ``cat`` / ``jq -c`` work as expected."""
    rows = list(claims)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for claim in rows:
            fh.write(claim.model_dump_json(exclude_none=False) + "\n")
    return len(rows)


def load_variant_claims(path: Path) -> list[ContribVariantClaim]:
    """Read every row from ``path``; dedup on
    ``(raw_work_uri, variant_label, canonical_label)`` so re-runs that
    appended overlapping rows produce one effective claim per
    (record, variant) pair. Returns ``[]`` when the file is missing
    — sidecar absence is a normal state for runs without the
    ``--llm-contrib-cascade`` flag."""
    if not path.is_file():
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[ContribVariantClaim] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Bad JSON at {path!s}:{line_no}: {exc}") from exc
        claim = ContribVariantClaim.model_validate(data)
        key = (claim.raw_work_uri, claim.variant_label, claim.canonical_label)
        if key in seen:
            continue
        seen.add(key)
        out.append(claim)
    return out


def truncate_sidecar(path: Path) -> None:
    """Empty the sidecar at ``path``. Called by M3 ``--force`` re-runs
    so a fresh cascade pass starts with a clean file rather than
    accumulating stale rows from prior runs."""
    if path.exists():
        path.write_text("", encoding="utf-8")


__all__ = [
    "DEFAULT_SIDECAR_NAME",
    "ContribVariantClaim",
    "append_variant_claims",
    "load_variant_claims",
    "truncate_sidecar",
]
