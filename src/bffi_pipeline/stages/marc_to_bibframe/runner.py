"""Pillar 2 orchestrator: corpus-wide MARCXML -> BIBFRAME RDF/XML conversion.

Walks an input directory of MARCXML records, runs each through the
marc2bibframe2 XSLT pipeline (optional preprocess split + main convert),
writes a ``<bib-id>.bibframe.xml`` per input under the output directory,
and emits observability events (start / progress / failed / end) along
the way.

Stage label for observability sidecar events: ``marc2bibframe``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from bffi_pipeline.observability.events import emit_if_active
from bffi_pipeline.stages.marc_to_bibframe.xslt import (
    XsltPaths,
    XsltprocError,
    run_xsltproc,
)

STAGE: Final[str] = "marc2bibframe"

#: How often to emit a ``progress`` event during corpus conversion.
#: Tuned to balance sidecar density against dashboard tail-responsiveness.
PROGRESS_CADENCE: Final[int] = 100


@dataclass(frozen=True)
class ConversionOptions:
    """Configuration for one corpus-conversion run."""

    input_dir: Path
    output_dir: Path
    xslt_paths: XsltPaths
    #: Stem of the BIBFRAME URI namespace; matches our committed identifiers.
    baseuri: str = "http://urn.fi/URN:NBN:fi:bib:"
    #: Which MARC field carries the record ID. Helmet bib IDs live in 001.
    idfield: str = "001"
    #: Optional source URI for the Local identifier minted from ``idfield``.
    idsource: str | None = None
    #: Run the LoC preprocessing splitter before the main conversion.
    #: Default True per the LoC README's "strongly encouraged" guidance.
    preprocess: bool = True
    #: Per-record timeout in seconds. xsltproc has been seen to hang on
    #: malformed input; the timeout is the fallback when that happens.
    timeout_per_record: float = 60.0


@dataclass
class ConversionSummary:
    """Aggregate counts after corpus conversion ends."""

    total: int = 0
    converted: int = 0
    failed: int = 0
    failures: list[tuple[Path, str]] = field(default_factory=list)


def _xslt_params(options: ConversionOptions) -> dict[str, str]:
    params: dict[str, str] = {
        "baseuri": options.baseuri,
        "idfield": options.idfield,
    }
    if options.idsource:
        params["idsource"] = options.idsource
    return params


def convert_one(marcxml_path: Path, *, options: ConversionOptions) -> Path:
    """Convert one MARCXML file to BIBFRAME RDF/XML.

    Writes ``<output_dir>/<input_stem>.bibframe.xml`` and returns the path.
    Raises :exc:`XsltprocError` on any conversion failure (preprocess or
    convert pass).
    """
    output_path = options.output_dir / f"{marcxml_path.stem}.bibframe.xml"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    params = _xslt_params(options)

    if options.preprocess:
        # Two-pass via a temp file: preprocess output is the convert input.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".preprocessed.xml",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            pre = run_xsltproc(
                stylesheet=options.xslt_paths.preprocess,
                input_path=marcxml_path,
                timeout=options.timeout_per_record,
            )
            if not pre.ok:
                raise XsltprocError(
                    f"preprocess failed for {marcxml_path}: {pre.stderr.strip()[:240]}"
                )
            tmp_path.write_text(pre.stdout, encoding="utf-8")
            result = run_xsltproc(
                stylesheet=options.xslt_paths.convert,
                input_path=tmp_path,
                params=params,
                timeout=options.timeout_per_record,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        result = run_xsltproc(
            stylesheet=options.xslt_paths.convert,
            input_path=marcxml_path,
            params=params,
            timeout=options.timeout_per_record,
        )

    if not result.ok:
        raise XsltprocError(f"convert failed for {marcxml_path}: {result.stderr.strip()[:240]}")

    output_path.write_text(result.stdout, encoding="utf-8")
    return output_path


def convert_corpus(*, options: ConversionOptions) -> ConversionSummary:
    """Walk ``options.input_dir`` and convert every ``*.xml`` to BIBFRAME RDF/XML.

    Emits observability events through the active emitter (if any):

      - ``start``    once at entry, with ``entities_total``
      - ``progress`` every ``PROGRESS_CADENCE`` records
      - ``failed``   per record that raised :exc:`XsltprocError`
      - ``end``      once at exit, with success / failed bucket counts

    Returns the aggregate :class:`ConversionSummary`.
    """
    marcxml_files = sorted(options.input_dir.glob("*.xml"))
    total = len(marcxml_files)

    emit_if_active(
        stage=STAGE,
        event="start",
        counters={"entities_total": total},
    )

    summary = ConversionSummary(total=total)

    for idx, path in enumerate(marcxml_files, start=1):
        try:
            convert_one(path, options=options)
            summary.converted += 1
        except XsltprocError as exc:
            summary.failed += 1
            message = str(exc)
            summary.failures.append((path, message))
            emit_if_active(
                stage=STAGE,
                event="failed",
                extra={"path": str(path), "error": message[:240]},
            )

        if idx % PROGRESS_CADENCE == 0 or idx == total:
            emit_if_active(
                stage=STAGE,
                event="progress",
                counters={"entities_processed": idx},
            )

    emit_if_active(
        stage=STAGE,
        event="end",
        counters={
            "success": summary.converted,
            "failed": summary.failed,
        },
    )

    return summary
