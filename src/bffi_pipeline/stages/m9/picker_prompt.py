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

from bffi_pipeline.stages.m9.schemas import AuthorityCandidate

#: Picker prompt source. Hashed at startup so reconciliation provenance
#: pins the exact prompt that produced each decision.
PICKER_PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[4] / "prompts" / "picker_v1.txt"
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
    """Render the candidate list in the line-by-line format the prompt expects."""
    if not candidates:
        return "(no candidates were returned by the authority client)"
    return "\n".join(
        f"  {i}. uri={c.uri} prefLabel={c.pref_label!r} "
        f"lexical_similarity={c.lexical_similarity:.3f}"
        for i, c in enumerate(candidates, start=1)
    )
