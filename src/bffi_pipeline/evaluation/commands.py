"""Evaluation / benchmarking CLI commands.

Four commands previously inline in ``cli.py``:

- ``bffi-pipeline embed-benchmark`` — compare embedding models on the
  gold set's same_work / different_work gap (M5 / M12).
- ``bffi-pipeline embed-stats`` — report band counts + similarity
  distribution from the persisted M5 index.
- ``bffi-pipeline eval`` — score the gold set against the M6 judge
  and write a JSON summary (M12 Phase 2).
- ``bffi-pipeline grow-gold`` — grow the gold set from
  human-overridden judge decisions (M12 Phase 3).

All four are M12 / M5-adjacent evaluation work, not per-pipeline-run
stage invocations. Registered as flat top-level commands on the root
typer app from ``cli.py`` (``app.command("eval")(eval_command)`` etc.)
so the operator-facing CLI surface is bit-identical to the
pre-Phase-C state.

P-38 Phase C-3: extracted from ``cli.py`` so the eval CLI surface
lives next to its backing :mod:`bffi_pipeline.eval` package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bffi_pipeline.config import get_settings
from bffi_pipeline.eval import embed_benchmark
from bffi_pipeline.eval import grow as eval_grow
from bffi_pipeline.eval import harness as eval_harness
from bffi_pipeline.stages import m5


def embed_benchmark_command(
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--model",
            help=(
                "HuggingFace model name; pass multiple times to compare. "
                "Defaults to BGE-M3 / multilingual-e5-large / jina-v3."
            ),
        ),
    ] = None,
    gold_path: Annotated[
        Path | None,
        typer.Option(
            "--gold-path",
            help="Path to gold/gold.jsonl; defaults to repo gold/ directory.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    device: Annotated[
        str,
        typer.Option(
            "--device",
            help="PyTorch device: mps (Apple Silicon), cuda, or cpu.",
        ),
    ] = m5.DEFAULT_DEVICE,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Embedding batch size.",
        ),
    ] = m5.DEFAULT_BATCH_SIZE,
) -> None:
    """Compare embedding models on the gold set's same_work / different_work gap (M5 / M12)."""
    candidate_models = tuple(models) if models else embed_benchmark.DEFAULT_MODELS
    results = embed_benchmark.benchmark_models(
        models=candidate_models,
        gold_path=gold_path,
        device=device,
        batch_size=batch_size,
    )
    typer.echo(embed_benchmark.render_comparison(results))


def eval_command(
    run_label: Annotated[
        str,
        typer.Option(
            "--run-label",
            help=(
                "Identifier for this run (e.g. 'qwen3-32b-prompt-v3'). Becomes the "
                "filename stem under --output-dir and is recorded in the JSON summary."
            ),
        ),
    ],
    gold_path: Annotated[
        Path | None,
        typer.Option(
            "--gold-path",
            help="Path to gold/gold.jsonl; defaults to repo gold/ directory.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help=("Directory for the JSON summary; defaults to <repo>/eval-runs (gitignored)."),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Score the gold set against the M6 judge and write a JSON summary (M12).

    Eval is **not in CI** per spec § 9; this subcommand is invoked
    locally on the M5 Max via ``make eval`` before any PR that touches
    prompts / gold / judge code. The text rendering is paste-ready for
    the PR description; the JSON file is the durable record.
    """
    summary, out_path = eval_harness.run_eval(
        run_label=run_label,
        gold_path=gold_path,
        output_dir=output_dir,
    )
    typer.echo(eval_harness.render_text(summary))
    typer.echo("")
    typer.echo(f"Summary written to {out_path}")


def grow_gold_command(
    fuseki_url: Annotated[
        str | None,
        typer.Option(
            "--fuseki-url",
            help="Fuseki dataset base URL; defaults to FUSEKI_URL.",
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            help=(
                "Where to write the candidate JSONL; defaults to "
                "gold/grow-candidates.jsonl in the repo."
            ),
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Grow the gold set from human-overridden judge decisions (M12 phase 3).

    Run monthly per spec § 9. Writes a JSONL of candidate cases the
    cataloguer reviews — each row needs ``category`` filled in by hand
    before being merged into ``gold/gold.jsonl``. New cases default to
    ``holdout: false``; cataloguers flip the flag explicitly when a
    case should join the eval set.
    """
    result = eval_grow.grow(
        fuseki_url=fuseki_url,
        output_path=output_path,
    )
    typer.echo(result.render())


def embed_stats_command(
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory holding m5.faiss + idmap; defaults to BFFI_DATA_DIR.",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    top_k: Annotated[
        int,
        typer.Option("--top-k", help="Top-k neighbours per Work."),
    ] = m5.DEFAULT_TOP_K,
    cross_block: Annotated[
        bool,
        typer.Option(
            "--cross-block",
            help="Keep candidate pairs whose Stage-1 blocking keys differ.",
        ),
    ] = False,
) -> None:
    """Report band counts and similarity distribution from the persisted index (M5)."""
    target = output_dir or get_settings().data_dir
    stats = m5.query_candidates(target, top_k=top_k, cross_block=cross_block)
    typer.echo(stats.render())
