"""Enforce the BFFI namespace discipline rule (CLAUDE.md § Conventions).

The ``bffi:`` namespace is closed: we may only emit classes and
properties that exist in ``vocab/lkd.rdf`` (the vendored BFFI 1.0.0
ontology). This test scans every place we could conceivably emit a
``bffi:`` URI — Python vocab constants and SPARQL CONSTRUCT files —
and asserts each ``bffi:<name>`` reference appears as a declared
``rdf:about`` in ``lkd.rdf``.

Three legitimate paths for terms NOT in ``lkd.rdf`` (per CLAUDE.md):

1. Reuse a standard term (RDF / RDFS / OWL / SKOS / PROV-O / BIBFRAME / DCT).
2. Use ``bffi-prov:`` for pipeline-internal metadata. Ours; extending
   it is fine.
3. Propose addition to BFFI via NLF (precedent: P-49 Layer 3).

This test is the safety net. It runs in CI and breaks on any local
``bffi:`` mint — the regression that motivated P-50's vocab cleanup
(commit 3970699, where 5 locally-minted terms were swept off the
``bffi:`` namespace).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_LKD_RDF: Final[Path] = _REPO_ROOT / "vocab" / "lkd.rdf"
_VOCAB_PY: Final[Path] = _REPO_ROOT / "src" / "bffi_pipeline" / "provenance" / "vocab.py"
_SPARQL_DIR: Final[Path] = _REPO_ROOT / "sparql"

_BFFI_URI_PREFIX: Final[str] = "http://urn.fi/URN:NBN:fi:schema:bffi:"

#: Matches `rdf:about="http://urn.fi/URN:NBN:fi:schema:bffi:<name>"` in
#: ``lkd.rdf`` — the canonical declaration form.
_LKD_DECLARATION_RE: Final[re.Pattern[str]] = re.compile(
    r'rdf:about="' + re.escape(_BFFI_URI_PREFIX) + r'([A-Za-z][A-Za-z0-9]*)"'
)

#: Matches references in Python that pin a name into the ``BFFI``
#: namespace — both ``BFFI.<name>`` attribute access and
#: ``BFFI["<name>"]`` indexing forms. Captures the bare name.
_PYTHON_BFFI_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"\bBFFI(?:\.([A-Za-z][A-Za-z0-9]*)"
    r'|\["([A-Za-z][A-Za-z0-9]*)"\])'
)

#: Matches references in SPARQL that pin a name into the ``bffi:``
#: namespace — the ``bffi:<name>`` shorthand. Excludes the prefix
#: declaration line so ``PREFIX bffi: <…>`` doesn't trip the scan.
_SPARQL_BFFI_REF_RE: Final[re.Pattern[str]] = re.compile(r"\bbffi:([A-Za-z][A-Za-z0-9]*)\b")

#: Identifier names referenced via the ``BFFI`` Namespace object that
#: aren't BFFI terms. ``Namespace`` itself, the namespace's repr
#: helpers, etc. Empty today; add to this set if a false positive
#: appears in vocab.py without being a real namespaced term.
_PYTHON_BFFI_REF_EXCLUSIONS: Final[frozenset[str]] = frozenset()


def _declared_bffi_terms() -> set[str]:
    """Parse ``vocab/lkd.rdf`` and return the set of ``bffi:<name>``
    terms it declares (classes + properties)."""
    text = _LKD_RDF.read_text(encoding="utf-8")
    return set(_LKD_DECLARATION_RE.findall(text))


def _python_bffi_references() -> set[str]:
    """Scan ``vocab.py`` for ``BFFI.<name>`` / ``BFFI["<name>"]``
    references — every term we pin into the ``bffi:`` namespace from
    Python code lives here per the
    ``CLAUDE.md`` URI-discipline rule."""
    text = _VOCAB_PY.read_text(encoding="utf-8")
    refs: set[str] = set()
    for m in _PYTHON_BFFI_REF_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name and name not in _PYTHON_BFFI_REF_EXCLUSIONS:
            refs.add(name)
    return refs


def _strip_sparql_comments(text: str) -> str:
    """Drop SPARQL ``# …`` comments so ``bffi:Foo`` mentions inside
    explanatory text don't trip the discipline scan as false
    positives. SPARQL comments run from ``#`` to end-of-line."""
    out_lines: list[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        # Strip mid-line ``#`` comments. SPARQL strings can't contain
        # unescaped newlines so a simple split is safe — but ``#``
        # inside a string literal would mis-strip. Conservative: only
        # strip when ``#`` is preceded by whitespace (the usual
        # cataloguer convention; the regex's ``\b<name>\b`` requirement
        # provides a second guard).
        idx = raw.find(" #")
        line = raw[:idx] if idx >= 0 else raw
        out_lines.append(line)
    return "\n".join(out_lines)


def _sparql_bffi_references() -> dict[str, set[str]]:
    """Scan every ``.rq`` file under ``sparql/`` for ``bffi:<name>``
    references. Returns ``{filename: {names}}`` so a failing
    assertion can point at the offending file. Strips comments + the
    PREFIX declaration line so explanatory mentions don't false-
    positive."""
    refs: dict[str, set[str]] = {}
    for path in sorted(_SPARQL_DIR.glob("*.rq")):
        text = path.read_text(encoding="utf-8")
        text = _strip_sparql_comments(text)
        filtered_lines = [
            line for line in text.splitlines() if not line.lstrip().startswith("PREFIX")
        ]
        filtered = "\n".join(filtered_lines)
        names = set(_SPARQL_BFFI_REF_RE.findall(filtered))
        if names:
            refs[path.name] = names
    return refs


def test_python_vocab_bffi_terms_all_declared_in_lkd_rdf() -> None:
    """Every ``BFFI.<name>`` reference in ``vocab.py`` must point at a
    class or property that ``vocab/lkd.rdf`` declares.

    Failing names indicate a locally-minted ``bffi:`` term. Fix one of:

    - Swap for a standard term (``rdf:`` / ``rdfs:`` / ``owl:`` /
      ``skos:`` / ``prov:`` / ``bf:`` / ``dct:``).
    - Move to ``bffi-prov:`` if pipeline-internal.
    - Open a P-49-Layer-3-style proposal to add the term to BFFI.

    See CLAUDE.md § Conventions for the rule + decision tree.
    """
    declared = _declared_bffi_terms()
    referenced = _python_bffi_references()
    undeclared = referenced - declared
    assert not undeclared, (
        f"Locally-minted bffi: terms in vocab.py (not in vocab/lkd.rdf): "
        f"{sorted(undeclared)}. See CLAUDE.md § Conventions § BFFI "
        f"namespace discipline."
    )


def test_sparql_construct_bffi_terms_all_declared_in_lkd_rdf() -> None:
    """Every ``bffi:<name>`` reference in ``sparql/*.rq`` must point at
    a class or property declared in ``vocab/lkd.rdf``.

    Failing names indicate a locally-minted ``bffi:`` term being
    emitted by an M3 CONSTRUCT or queried by a SPARQL pass. Same fix
    options as the Python test above."""
    declared = _declared_bffi_terms()
    sparql_refs = _sparql_bffi_references()
    violations: dict[str, set[str]] = {}
    for filename, names in sparql_refs.items():
        undeclared = names - declared
        if undeclared:
            violations[filename] = undeclared
    assert not violations, (
        f"Locally-minted bffi: terms in SPARQL files (not in "
        f"vocab/lkd.rdf): {violations}. See CLAUDE.md § Conventions § "
        f"BFFI namespace discipline."
    )


def test_lkd_rdf_declaration_extractor_finds_known_terms() -> None:
    """Sanity check the extractor — at least one well-known BFFI term
    should appear in ``vocab/lkd.rdf``. Guards against an extractor
    regression that would silently render the two enforcement tests
    above as no-ops."""
    declared = _declared_bffi_terms()
    # ``bffi:Work`` / ``bffi:Manifestation`` / ``bffi:subject`` are
    # foundational; if NONE of these are found, the extractor is broken.
    assert {"Work", "Manifestation", "subject"} <= declared, (
        "lkd.rdf extractor regression — foundational terms missing"
    )


@pytest.mark.parametrize("name", ["fromMarcField", "syntheticSentinel", "MarcConversion"])
def test_pipeline_internal_metadata_lives_under_bffi_prov_not_bffi(
    name: str,
) -> None:
    """Spot-check: ``bffi-prov:`` predicates and classes we extend
    locally must NOT also exist under ``bffi:`` (would shadow them).
    Documents the convention by example."""
    declared = _declared_bffi_terms()
    assert name not in declared, (
        f"bffi-prov: term '{name}' has a collision in vocab/lkd.rdf. "
        f"Either the bffi-prov namespace claim is wrong or "
        f"lkd.rdf was updated to ratify this term — review."
    )
