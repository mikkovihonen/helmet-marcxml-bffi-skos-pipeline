"""M6 prompt loading + hashing.

The M6 judge ships two prompt files: ``judge_v1.txt`` (full-rationale)
and ``judge_v1_fast.txt`` (rationale-on-uncertainty). Each is split
into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` sections by ``### SECTION``
markers. Both files are hashed; the hash is logged with every M6
provenance record so a future audit can reproduce or regress a
decision against the exact prompt version.

P-38 Phase B: extracted from m6/runner.py to keep the runner focused
on the cascade orchestration. No logic change — moves only.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Final

#: Two-shot prompt source. Hashed at startup; the hash is logged with every
#: provenance record so a future audit can reproduce or regress a decision.
PROMPT_PATH: Final[Path] = Path(__file__).resolve().parents[4] / "prompts" / "judge_v1.txt"
#: Fast-mode prompt — rationale required only for ``uncertain`` or
#: ``confidence < FALLBACK_CONFIDENCE_THRESHOLD`` decisions. Used when
#: ``judge_pair`` is called with ``full_rationale=False`` to save the
#: rationale-generation tokens on confident clear-cut calls.
PROMPT_PATH_FAST: Final[Path] = (
    Path(__file__).resolve().parents[4] / "prompts" / "judge_v1_fast.txt"
)

#: Section markers in ``judge_v1.txt`` (the file is plain text — no YAML).
_PROMPT_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^### (\w+)\s*$", re.MULTILINE)


@lru_cache(maxsize=1)
def prompt_text() -> str:
    """Return the raw ``prompts/judge_v1.txt`` contents."""
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Judge prompt not found at {PROMPT_PATH!s}.")
    return PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def prompt_text_fast() -> str:
    """Return the raw ``prompts/judge_v1_fast.txt`` contents."""
    if not PROMPT_PATH_FAST.is_file():
        raise FileNotFoundError(f"Fast judge prompt not found at {PROMPT_PATH_FAST!s}.")
    return PROMPT_PATH_FAST.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def prompt_hash() -> str:
    """SHA-256 of :func:`prompt_text`. Logged with every provenance record."""
    return "sha256:" + hashlib.sha256(prompt_text().encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def prompt_hash_fast() -> str:
    """SHA-256 of :func:`prompt_text_fast`. Logged separately so a re-run
    that switches modes invalidates the cache automatically (different
    prompt → different ``cache_key`` per :func:`_cache_key`)."""
    return "sha256:" + hashlib.sha256(prompt_text_fast().encode("utf-8")).hexdigest()[:16]


def _parse_sections(raw: str, source_path: Path) -> dict[str, str]:
    """Shared section-splitter for SYSTEM / EXAMPLES / USER blocks."""
    sections: dict[str, str] = {}
    matches = list(_PROMPT_SECTION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"No '### SECTION' markers found in {source_path!s}.")
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    for required in ("SYSTEM", "EXAMPLES", "USER"):
        if required not in sections:
            raise ValueError(f"{source_path!s} is missing required '### {required}' section.")
    return sections


@lru_cache(maxsize=1)
def _parse_prompt_sections() -> dict[str, str]:
    """Split ``judge_v1.txt`` into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks."""
    return _parse_sections(prompt_text(), PROMPT_PATH)


@lru_cache(maxsize=1)
def _parse_prompt_sections_fast() -> dict[str, str]:
    """Split ``judge_v1_fast.txt`` into ``SYSTEM`` / ``EXAMPLES`` / ``USER`` blocks."""
    return _parse_sections(prompt_text_fast(), PROMPT_PATH_FAST)
