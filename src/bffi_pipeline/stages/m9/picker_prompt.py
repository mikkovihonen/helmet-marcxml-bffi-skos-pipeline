"""M9 picker prompt loader + section split + hashing + candidate formatting.

The prompt text lives in ``prompts/picker_v1.txt`` and is parsed into
``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks via ``### SECTION`` markers.
Hashes are recorded on each provenance Activity so a re-run that
changes the prompt is forensically distinguishable from a cold run
against the same model.

P-38 Phase D: extracted from m9/runner.py. No logic change.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Final

from bffi_pipeline.stages.m9.schemas import AuthorityCandidate, WorkContext

#: Picker prompt source. Hashed at startup so reconciliation provenance
#: pins the exact prompt that produced each decision.
#:
#: v2 (2026-06-04) added an Originating-Work context block — Work
#: title, language, contributors, sibling subjects — so the picker
#: can disambiguate same-named authorities by topical match.
#:
#: v3 (2026-06-04) adds per-candidate context lines (definition,
#: scope note, broader topics, occupations, life dates, alt labels)
#: fetched from local Fuseki by
#: :class:`bffi_pipeline.stages.m9.candidate_context.FusekiCandidateContextFetcher`.
#: This is the central lever for shared-name disambiguation: the
#: candidate side now tells the picker that "Koivisto, Ilkka" #1 is a
#: psychologist while #2 is a zoologist.
#:
#: v4 (2026-06-04) was an attempt to add explicit confidence-
#: calibration rules to fix v3's over-hedging. The smoke (run
#: ``392823a160304676be614ab52952c21c``) showed needs_review jumped
#: to 133 (vs v3's 40) — explicitly mentioning the "0.80 downstream
#: gate" caused the LLM to mass-emit "uncertain". The 92-min M9 wall
#: time on that run was later traced to a separate
#: :class:`bffi_pipeline.stages.m9.candidate_context.FusekiCandidateContextFetcher`
#: SPARQL bug (unbound-variable cartesian explosion against ~290k
#: cross-graph prefLabels — fixed by the BOUND/isIRI guard +
#: ``skos:inScheme`` anchor); the prompt change alone wouldn't have
#: blown wall time up that dramatically. v4 stays in tree for replay
#: only; the over-hedging fix lives in
#: :data:`bffi_pipeline.stages.m9.schemas.LLM_CONFIDENCE_THRESHOLD`
#: (lowered from 0.80 to 0.75 to match v3's more-realistically-
#: calibrated confidence distribution).
#:
#: v5 (2026-06-05) adds a single "Confidence coherence" rule to the
#: rule list: when decision="uncertain", confidence must be ≤ 0.5.
#: Surfaced during the gemma-4-26B-A4B eval shootout on the 10-case
#: eval-picker set — the model emitted ``{"decision":"uncertain",
#: "confidence":1.0}`` on one case, which
#: :class:`bffi_pipeline.stages.m9.picker.PickerDecision`'s pydantic
#: validator rejects (the pair is structurally incoherent — high
#: confidence with no chosen URI). The clarification is one line in
#: the rule block, mirrors what the validator already enforces, and
#: invalidates the picker cache by content-hash. No example changes;
#: the v3 examples already follow this convention.
#:
#: Older prompts stay in tree for replay of historical decisions;
#: the cache key includes the prompt SHA so cross-version decisions
#: never collide.
PICKER_PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[4] / "prompts" / "picker_v5.txt"
_PICKER_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)


@lru_cache(maxsize=1)
def picker_prompt_text() -> str:
    """Return the raw ``prompts/picker_v1.txt`` contents."""
    if not PICKER_PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Picker prompt not found at {PICKER_PROMPT_PATH!s}.")
    return PICKER_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def picker_prompt_hash() -> str:
    """SHA-256 of :func:`picker_prompt_text`. Logged with each reconciliation."""
    return "sha256:" + hashlib.sha256(picker_prompt_text().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _parse_picker_prompt_sections() -> dict[str, str]:
    """Split ``picker_v1.txt`` into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks."""
    raw = picker_prompt_text()
    sections: dict[str, str] = {}
    matches = list(_PICKER_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {PICKER_PROMPT_PATH!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(
                f"{PICKER_PROMPT_PATH!s} is missing required '### {required}' section."
            )
    return sections


def _format_candidates_for_prompt(candidates: list[AuthorityCandidate]) -> str:
    """Render the candidate list in the line-by-line format the prompt expects.

    Each candidate's ``context`` (Phase B candidate-side enrichment)
    renders as indented sub-lines beneath the prefLabel: scope notes,
    broader topics, biographical fragments, occupation labels. Empty
    sub-fields are omitted. Candidates with ``context=None`` render
    in the pre-Phase-B prefLabel-only form — graceful degrade when
    Fuseki has no entry for that URI.
    """
    if not candidates:
        return "(no candidates were returned by the authority client)"
    parts: list[str] = []
    for i, c in enumerate(candidates, start=1):
        parts.append(
            f"  {i}. uri={c.uri} prefLabel={c.pref_label!r} "
            f"lexical_similarity={c.lexical_similarity:.3f}"
        )
        parts.extend(_format_candidate_context_lines(c.context))
    return "\n".join(parts)


def _format_candidate_context_lines(context: object) -> list[str]:
    """Render one :class:`CandidateContext` as indented sub-lines beneath
    its parent candidate row. Returns ``[]`` when ``context`` is ``None``
    so callers can blindly extend without an empty-list guard.

    Imports the dataclass lazily to keep this module's import graph
    light — the picker prompt is in M9's hot path and the context
    module pulls in httpx for the Fuseki client.
    """
    if context is None:
        return []
    from bffi_pipeline.stages.m9.candidate_context import CandidateContext  # noqa: PLC0415

    if not isinstance(context, CandidateContext):
        return []
    out: list[str] = []
    if context.definition:
        out.append(f"     definition: {context.definition}")
    if context.scope_note:
        out.append(f"     scope note: {context.scope_note}")
    if context.broader_labels:
        out.append(f"     broader topics: {list(context.broader_labels)!r}")
    if context.field_of_activity_labels:
        out.append(f"     field of activity: {list(context.field_of_activity_labels)!r}")
    if context.occupation_labels:
        out.append(f"     occupations: {list(context.occupation_labels)!r}")
    if context.birth_date or context.death_date:
        out.append(f"     life dates: {context.birth_date or '?'}-{context.death_date or ''}")
    if context.alt_labels:
        out.append(f"     alt labels: {list(context.alt_labels)!r}")
    return out


def _format_work_context_for_prompt(work_context: WorkContext | None) -> str:
    """Render the originating Work's context block for the picker prompt.

    Returns one indented line per populated field. Empty / missing
    fields are omitted so a Work with only a title doesn't render a
    pile of placeholder rows. When ``work_context`` is ``None`` (test
    requests, or pre-Phase-A callers building EntityRequest directly),
    returns a short marker so the prompt still parses cleanly.
    """
    if work_context is None:
        return "  (no Work context supplied)"
    lines: list[str] = []
    if work_context.title:
        lines.append(f"  title: {work_context.title!r}")
    if work_context.language:
        lines.append(f"  language: {work_context.language}")
    if work_context.contributors:
        lines.append(f"  contributors: {list(work_context.contributors)!r}")
    if work_context.sibling_subjects:
        lines.append(f"  other subjects on this Work: {list(work_context.sibling_subjects)!r}")
    if work_context.classification:
        lines.append(f"  classification: {list(work_context.classification)!r}")
    if work_context.year:
        lines.append(f"  year: {work_context.year}")
    if not lines:
        return "  (Work context was empty — no title / subjects / contributors)"
    return "\n".join(lines)
