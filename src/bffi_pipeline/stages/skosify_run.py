"""Stage M10 (phase 1): Skosify overlay run.

Loads the M8 ``canonical.ttl`` together with
``config/overlay/bffi-skos-overlay.ttl`` through ``skosify.skosify``
with RDFS inference on. The output keeps the BFFI types
(``bffi:Work`` / ``bffi:Expression``) intact and adds the matching
``skos:Concept`` typing plus ``skos:narrower`` / ``skos:broader``
inverses on the Work ↔ Expression hierarchy. The result is what M10
phase 2 will load into Fuseki's ``bffi-works`` named graph.

Spec § 5 commits to the *overlay-plus-inference* approach: the
destructive Skosify ``[types]`` section is left empty, and the
overlay declares
``bffi:Work / bffi:Expression rdfs:subClassOf skos:Concept`` so
future BFFI subclasses absorb without a config change.

Output goes to ``<BFFI_DATA_DIR>/canonical-skosified.ttl`` via
tmp-then-rename. Re-runs are idempotent: when the output exists *and*
is newer than every input (canonical, overlay, config), the run
skips re-serialising. Pass ``force=True`` to override.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from rdflib import Graph

from bffi_pipeline.config import get_settings
from bffi_pipeline.provenance import vocab as V
from bffi_pipeline.stages.observability import emit_if_active

#: Default filenames under ``BFFI_DATA_DIR``.
SKOSIFIED_FILENAME: Final[str] = "canonical-skosified.ttl"

#: Project-relative paths to the canonical config files.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_OVERLAY_PATH: Final[Path] = _REPO_ROOT / "config" / "overlay" / "bffi-skos-overlay.ttl"
DEFAULT_CONFIG_PATH: Final[Path] = _REPO_ROOT / "config" / "bffi.cfg"


@dataclass
class SkosifyResult:
    """Summary of one ``run`` invocation."""

    input_triples: int
    output_triples: int
    dual_typed_works: int
    dual_typed_expressions: int
    inferred_narrower: int
    inferred_broader: int
    skipped_idempotent: bool
    output_path: str

    def render(self) -> str:
        """Format this result as paste-ready text for the skosify CLI."""
        if self.skipped_idempotent:
            return (
                "Skosify skipped — output is newer than the inputs (canonical, "
                "overlay, config). Re-run with --force to override.\n"
                f"  output: {self.output_path}"
            )
        return "\n".join(
            (
                "Skosify run complete",
                f"  input triples:                  {self.input_triples:,}",
                f"  output triples:                 {self.output_triples:,}",
                f"  dual-typed Works:               {self.dual_typed_works:,}",
                f"  dual-typed Expressions:         {self.dual_typed_expressions:,}",
                f"  inferred skos:narrower triples: {self.inferred_narrower:,}",
                f"  inferred skos:broader  triples: {self.inferred_broader:,}",
                f"  output: {self.output_path}",
            )
        )


def _load_skosify_config(config_path: Path) -> dict[str, Any]:
    """Read ``bffi.cfg`` into the kwargs dict skosify expects.

    Deferred import — ``skosify`` is fast enough but reading the config
    fails at module import time on machines without skosify, which
    would prevent the rest of the CLI from loading.
    """
    from skosify import config as skosify_config

    cfg = skosify_config(str(config_path))
    return dict(cfg)


def _is_output_fresh(output_path: Path, input_paths: list[Path]) -> bool:
    if not output_path.is_file():
        return False
    out_mtime = output_path.stat().st_mtime
    return all(p.stat().st_mtime <= out_mtime for p in input_paths if p.is_file())


def _count_dual_typed(g: Graph, bffi_class: Any) -> int:
    """How many subjects are typed as both ``bffi_class`` and ``skos:Concept``?"""
    count = 0
    for s in set(g.subjects(V.RDF.type, bffi_class)):
        types = set(g.objects(s, V.RDF.type))
        if V.SKOS.Concept in types:
            count += 1
    return count


def _summarise(
    skosified: Graph, *, input_triples: int, output_path: Path, skipped: bool
) -> SkosifyResult:
    return SkosifyResult(
        input_triples=input_triples,
        output_triples=len(skosified),
        dual_typed_works=_count_dual_typed(skosified, V.BFFI.Work),
        dual_typed_expressions=_count_dual_typed(skosified, V.BFFI.Expression),
        inferred_narrower=len(list(skosified.triples((None, V.SKOS.narrower, None)))),
        inferred_broader=len(list(skosified.triples((None, V.SKOS.broader, None)))),
        skipped_idempotent=skipped,
        output_path=str(output_path),
    )


def run(
    canonical_path: Path | None = None,
    *,
    output_path: Path | None = None,
    overlay_path: Path | None = None,
    config_path: Path | None = None,
    force: bool = False,
) -> SkosifyResult:
    """Run Skosify on the canonical Turtle + overlay; emit the dual-typed result."""
    from skosify import skosify

    settings = get_settings()
    canonical_path = canonical_path or (settings.data_dir / "canonical.ttl")
    overlay_path = overlay_path or DEFAULT_OVERLAY_PATH
    config_path = config_path or DEFAULT_CONFIG_PATH
    output_path = output_path or (settings.data_dir / SKOSIFIED_FILENAME)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Total for the dashboard's row-2 ``${skosify_total}`` tile —
    # sourced from M8's ``canonical-map.jsonl`` (one row per canonical
    # Work; M9 / Skosify / Load don't add or remove Works so the count
    # is stable across all post-M8 stages).
    canonical_map = settings.data_dir / "canonical-map.jsonl"
    total = sum(1 for _ in canonical_map.open(encoding="utf-8")) if canonical_map.is_file() else 0
    emit_if_active(
        stage="skosify",
        event="start",
        counters={"total": total},
        extra={"input": str(canonical_path)},
    )

    if not canonical_path.is_file():
        raise FileNotFoundError(
            f"Canonical Turtle not found at {canonical_path!s}. Run `bffi-pipeline merge` first."
        )

    inputs = [canonical_path, overlay_path, config_path]

    if not force and _is_output_fresh(output_path, inputs):
        existing = Graph()
        existing.parse(str(output_path), format="turtle")
        result = _summarise(existing, input_triples=0, output_path=output_path, skipped=True)
        emit_if_active(
            stage="skosify",
            event="end",
            counters={"output_triples": len(existing)},
            extra={"skipped": True},
        )
        return result

    pre_graph = Graph()
    pre_graph.parse(str(canonical_path), format="turtle")
    input_triples = len(pre_graph)

    cfg = _load_skosify_config(config_path)
    skosified = skosify(str(canonical_path), str(overlay_path), **cfg)

    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    skosified.serialize(destination=str(tmp), format="turtle")
    tmp.replace(output_path)

    emit_if_active(
        stage="skosify",
        event="end",
        counters={"input_triples": input_triples, "output_triples": len(skosified)},
        extra={"skipped": False},
    )
    return _summarise(
        skosified, input_triples=input_triples, output_path=output_path, skipped=False
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_OVERLAY_PATH",
    "SKOSIFIED_FILENAME",
    "SkosifyResult",
    "run",
]
