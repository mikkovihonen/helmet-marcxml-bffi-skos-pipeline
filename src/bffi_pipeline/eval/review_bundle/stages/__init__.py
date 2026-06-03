"""Stage handlers for review bundles.

Each LLM-using pipeline stage that wants cataloguer review registers a
:class:`StageHandler` here, keyed by its versioned schema id
(``contrib-candidate/1``, future: ``judge-pair/1``, ``picker-choice/1``).
The handler tells the bundle builder how to sample candidates + which
bib ids to attach MARC sidecars for, and tells the bundle importer
how to validate the cataloguer's KEEP rows + append them to the
stage's gold file.

V2 ships **one handler** — contrib. Adding the next stage is a single
registry entry + a per-stage module sibling to :mod:`contrib`, plus a
matching surface in the HTML's ``STAGE_SURFACES``. The infrastructure
in :mod:`bffi_pipeline.eval.review_bundle.bundle` is stage-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from bffi_pipeline.eval.review_bundle.stages import contrib, judge


class _SampleFn(Protocol):
    def __call__(
        self,
        *,
        input_path: Path,
        output_path: Path,
        per_category: int = ...,
        seed: str | None = ...,
    ) -> dict[str, int]: ...


class _ImportFn(Protocol):
    def __call__(self, *, input_path: Path, output_path: Path) -> Any: ...


class _BibIdsFn(Protocol):
    def __call__(self, candidates_jsonl: bytes) -> list[str]: ...


@dataclass(frozen=True)
class StageHandler:
    """One stage's bundle plug-in surface.

    - ``stage_id``: short directory-safe identifier (``"contrib"``,
      ``"judge"``, ...). Used as the per-stage subdirectory name
      inside the bundle zip.
    - ``label``: human-readable tab label the HTML shows for this stage.
    - ``audit_filename``: the per-run JSONL the stage writes to under
      ``runs/<uuid>/``. The cataloguer-bundle dispatcher uses this to
      auto-discover which stages actually have an audit log present
      for this run, so heuristic-only / skipped stages no-op silently
      without operator-side flags.
    - ``sample``: produces the per-stage ``candidates.jsonl`` from a
      pool file.
    - ``import_results``: consumes the per-stage ``results.jsonl`` from
      a cataloguer's results zip and appends KEEP rows to the gold
      file.
    - ``bib_ids_for_marc``: which bib ids the bundle builder should
      look up MARCXML sidecars for. Takes the raw candidates JSONL
      bytes (not a path) so the bundle builder can pass its in-memory
      buffer. Stages whose review doesn't need MARC return an empty
      list.
    - ``default_gold_path``: where the importer appends KEEP rows when
      the CLI's ``--output`` flag is omitted.
    - ``default_pool_path``: the candidate pool file the sampler reads
      when no override is supplied. ``None`` for stages whose pool is
      strictly per-run (e.g. judge — there's no corpus-wide aggregator).
    """

    stage_id: str
    label: str
    audit_filename: str
    sample: _SampleFn
    import_results: _ImportFn
    bib_ids_for_marc: _BibIdsFn
    default_gold_path: Path
    default_pool_path: Path | None


_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]


STAGE_REGISTRY: Final[dict[str, StageHandler]] = {
    "contrib-candidate/1": StageHandler(
        stage_id="contrib",
        label="M3 contrib extraction",
        audit_filename="contrib-candidates.jsonl",
        sample=contrib.sample_stratified,
        import_results=contrib.import_results,
        bib_ids_for_marc=contrib.bib_ids_for_marc,
        default_gold_path=_REPO_ROOT / "gold" / "contrib.jsonl",
        default_pool_path=_REPO_ROOT / "gold" / "grow-candidates-contrib.jsonl",
    ),
    "judge-pair/1": StageHandler(
        stage_id="judge",
        label="M6 judge",
        audit_filename=judge.AUDIT_FILENAME,
        sample=judge.sample_stratified,
        import_results=judge.import_results,
        bib_ids_for_marc=judge.bib_ids_for_marc,
        default_gold_path=_REPO_ROOT / "gold" / "gold.jsonl",
        # Per-run only — no corpus-wide aggregator. The cataloguer-bundle
        # dispatcher resolves this stage's pool to
        # runs/<uuid>/judge-decisions.jsonl at run time.
        default_pool_path=None,
    ),
}

__all__ = ["STAGE_REGISTRY", "StageHandler"]
