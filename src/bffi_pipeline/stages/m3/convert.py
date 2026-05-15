"""M3 per-record conversion pipeline + corpus concat.

Glues the M3 sub-modules together for one BIBFRAME RDF/XML input:

1. Parse to an rdflib :class:`~rdflib.Graph`.
2. Apply pre-CONSTRUCT byte-level repairs
   (:mod:`bffi_pipeline.stages.m3.sanitize`).
3. Run both SPARQL CONSTRUCTs (:mod:`construct`).
4. ``post_process`` tags prefLabels + runs 245$c cascade + binds
   namespaces (:mod:`post_process`).
5. Serialise to ``<output_dir>/bffi/<helmet_id>.ttl`` atomically.

``_write_bffi_corpus`` is the P-19 Phase A concat that feeds M8's
single-stream load. Idempotent; skips when the existing concat is at
least as new as every per-record ``.ttl``.

P-38 Phase D: extracted from m3/runner.py to keep the runner focused
on the multi-record driver loop. No logic change — moves only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from rdflib import Graph

from bffi_pipeline.stages.m3.construct import construct_bffi
from bffi_pipeline.stages.m3.post_process import post_process
from bffi_pipeline.stages.m3.sanitize import _sanitize_date_literals, _sanitize_uri_whitespace


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _is_output_fresh(input_path: Path, output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_mtime >= input_path.stat().st_mtime


def _iter_bibframe_files(bibframe_dir: Path) -> Iterator[Path]:
    yield from sorted(p for p in bibframe_dir.glob("*.rdf") if not p.name.startswith("_"))


def _write_bffi_corpus(bffi_dir: Path, corpus_path: Path) -> int:
    """Concatenate every per-record BFFI Turtle into a single corpus file.

    M8's load (~8 min on 20 k bench, projected ~5.5 h on the 800 k
    corpus) is dominated by per-file ``open`` + parser-init overhead,
    not by graph size. Layering one ``bffi-corpus.ttl`` stream over
    the per-record store collapses ``len(bffi/*.ttl)`` opens into one
    on M8's side. P-19 Phase A.

    Idempotent: skip when the existing concat is at least as new as
    every per-record ``.ttl``. The per-record layout stays canonical
    — the concat is a derived view.

    ``@prefix`` declarations are deduplicated (single block at the
    top, per-record headers stripped) to avoid a multi-millionfold
    redeclaration that rdflib's parser would walk on a full-corpus
    parse. Returns the number of per-record files concatenated, or
    ``0`` when the concat was skipped or no input files existed.
    """
    if not bffi_dir.is_dir():
        return 0
    per_record = sorted(bffi_dir.glob("*.ttl"))
    if not per_record:
        return 0
    if corpus_path.is_file():
        corpus_mtime = corpus_path.stat().st_mtime
        if all(p.stat().st_mtime <= corpus_mtime for p in per_record):
            return 0

    seen_prefixes: set[str] = set()
    prefix_lines: list[str] = []
    body_chunks: list[str] = []
    for path in per_record:
        with path.open("r", encoding="utf-8") as fh:
            body_lines: list[str] = []
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("@prefix") or stripped.startswith("@base"):
                    if stripped not in seen_prefixes:
                        seen_prefixes.add(stripped)
                        prefix_lines.append(line.rstrip("\n"))
                    continue
                body_lines.append(line)
            body_chunks.append("".join(body_lines).rstrip("\n"))

    tmp = corpus_path.with_suffix(corpus_path.suffix + ".tmp")
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as fh:
        for line in prefix_lines:
            fh.write(line + "\n")
        fh.write("\n")
        for chunk in body_chunks:
            if not chunk.strip():
                continue
            fh.write(chunk)
            fh.write("\n\n")
    os.replace(tmp, corpus_path)
    return len(per_record)


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _convert_one(
    input_path: Path,
    output_path: Path,
    *,
    llm_detector: object | None = None,
    contrib_extractor: object | None = None,
    variants_sidecar_path: Path | None = None,
    now: datetime | None = None,
) -> Graph:
    source = Graph()
    source.parse(str(input_path), format="xml")
    # Cataloguer $0 values occasionally carry stray whitespace that
    # marc2bibframe2 passes through unchanged; rdflib refuses to
    # serialize those as Turtle and the whole record's M3 conversion
    # would fail hard. Sanitize the parsed source so the CONSTRUCT
    # pass sees clean URIs.
    _sanitize_uri_whitespace(source)
    # Cataloguer-supplied date placeholders (e.g. ``"19  -  -  T00:00:00"``
    # for "year not yet entered") parse as xsd:dateTime in
    # marc2bibframe2's output but raise ValueError when rdflib tries
    # to coerce them at downstream load. Drop the datatype tag so the
    # literal survives as plain text rather than crashing the merge.
    _sanitize_date_literals(source)
    bffi_graph = construct_bffi(source)
    post_process(
        bffi_graph,
        source,
        llm_detector=llm_detector,
        contrib_extractor=contrib_extractor,
        variants_sidecar_path=variants_sidecar_path,
        now=now,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(output_path, bffi_graph.serialize(format="turtle").encode("utf-8"))
    return bffi_graph
