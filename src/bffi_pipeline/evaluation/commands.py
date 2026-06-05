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
from bffi_pipeline.eval import embed_benchmark, review_bundle
from bffi_pipeline.eval import grow as eval_grow
from bffi_pipeline.eval import grow_contrib as eval_grow_contrib
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


def grow_gold_contrib_command(
    bibframe_dir: Annotated[
        Path | None,
        typer.Option(
            "--bibframe-dir",
            help=(
                "Directory of M2's per-record BIBFRAME RDF/XML files; "
                "defaults to <BFFI_DATA_DIR>/bibframe."
            ),
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output-path",
            help=(
                "Where to write the candidate JSONL; defaults to "
                "gold/grow-candidates-contrib.jsonl in the repo."
            ),
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help=(
                "Max number of BIBFRAME files to scan. Useful for "
                "cost-bounded smoke runs — each cascade hit is one "
                "Qwen3 8B call (~10 s warm)."
            ),
        ),
    ] = None,
    llm_cascade: Annotated[
        bool,
        typer.Option(
            "--llm-cascade/--no-llm-cascade",
            help=(
                "Run the LLM contrib-extract cascade. With --no-llm-cascade "
                "the tool walks the corpus + measures heuristic-fire rate "
                "without spending LLM time (cost projection)."
            ),
        ),
    ] = True,
    dedup_against_gold: Annotated[
        bool,
        typer.Option(
            "--dedup-against-gold/--no-dedup-against-gold",
            help=(
                "Skip records whose helmet_bib_id is already in "
                "gold/contrib.jsonl. Re-runs don't re-ask the cataloguer "
                "to re-vet cases they already merged."
            ),
        ),
    ] = True,
) -> None:
    """Grow ``gold/contrib.jsonl`` from M3 contrib-extract cascade decisions.

    Walks ``<bibframe-dir>/*.rdf`` and runs the same heuristic + LLM
    cascade M3 runs internally. For every decision with ≥ 1
    contribution, writes a candidate row the cataloguer reviews,
    fills in ``category``, optionally flips ``holdout``, and merges
    into ``gold/contrib.jsonl`` by hand.

    P-39 (M9 reconciles non-primary contributions against KANTO) is
    gated on this gold file reaching ≥ 30 cataloguer-vetted rows.
    """
    settings = get_settings()
    bibframe_path = bibframe_dir or (settings.data_dir / "bibframe")
    target = output_path or eval_grow_contrib.DEFAULT_CONTRIB_CANDIDATES_PATH

    extractor: object | None = None
    if llm_cascade:
        # Lazy-import the LangChain stack so --no-llm-cascade runs don't
        # pull in the LLM dependencies.
        from bffi_pipeline.contrib_extract_llm import LangChainContribExtractor  # noqa: PLC0415

        extractor = LangChainContribExtractor()

    gold_path: Path | None = None
    if dedup_against_gold:
        gold_path = Path(__file__).resolve().parents[2] / "gold" / "contrib.jsonl"

    from bffi_pipeline.contrib_extract_llm import ContribExtractor  # noqa: PLC0415

    summary = eval_grow_contrib.generate_candidates(
        bibframe_dir=bibframe_path,
        output_path=target,
        extractor=extractor,  # type: ignore[arg-type]
        limit=limit,
        existing_gold_path=gold_path,
    )
    typer.echo(summary.render())
    # Quiet unused-import — kept for the type alias used in the module signature.
    _ = ContribExtractor


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


def eval_picker_command(
    cases_path: Annotated[
        Path,
        typer.Option(
            "--cases",
            help=(
                "JSONL file of picker eval cases. See gold/picker-eval/cases.jsonl for the schema."
            ),
            file_okay=True,
            dir_okay=False,
            exists=True,
            readable=True,
            resolve_path=True,
        ),
    ] = Path("gold/picker-eval/cases.jsonl"),
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Override LLM_MODEL_PRIMARY for this eval run. Lets the operator "
                "compare Qwen3-8B vs Qwen3-32B vs Qwen3.6-27B side-by-side without "
                "touching .env. Default: the configured llm_model_primary."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Score only the first N cases. 0 (default) = score all.",
            min=0,
        ),
    ] = 0,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of the human-readable table.",
        ),
    ] = False,
) -> None:
    """Score the configured picker LLM against a fixed eval set.

    Reuses :class:`bffi_pipeline.stages.m9.picker.LangChainLLMPicker`
    verbatim so the eval tests the real prompt + JSON-mode setup.
    Use it to compare models without running the 30-min full-chain
    smoke, and to pin known-tricky cases (Hakala-the-baritone,
    Koivisto-the-zoologist, Suomi-the-country) as regression tests.

    Set ``LLM_BASE_URL`` in the environment to point at the model
    server being tested. The ``--model`` flag overrides the model
    name; the base URL stays env-driven so two different mlx-lm
    instances on different ports can be compared by just changing
    ``LLM_BASE_URL=http://localhost:8001/v1 ... eval-picker`` ↔
    ``LLM_BASE_URL=http://localhost:8002/v1 ...``.
    """
    import json as _json  # noqa: PLC0415

    from bffi_pipeline.eval.picker_eval import (  # noqa: PLC0415
        load_cases,
        render_table,
        score_set,
    )
    from bffi_pipeline.stages.m9.picker_chain import LangChainLLMPicker  # noqa: PLC0415

    cases = load_cases(cases_path)
    if limit > 0:
        cases = cases[:limit]

    picker = LangChainLLMPicker(model_name=model)

    settings = get_settings()
    effective_model = model or settings.llm_model_primary
    typer.echo(
        f"Picker eval — {len(cases)} cases, model={effective_model}, "
        f"base_url={settings.llm_base_url}"
    )
    typer.echo("")

    results, summary = score_set(cases, picker)

    if json_output:
        payload = {
            "model": effective_model,
            "base_url": settings.llm_base_url,
            "summary": {
                "total": summary.total,
                "decision_matches": summary.decision_matches,
                "uri_matches": summary.uri_matches,
                "confidently_wrong": summary.confidently_wrong,
                "parse_failures": summary.parse_failures,
                "avg_latency_seconds": summary.avg_latency,
                "max_latency_seconds": summary.max_latency,
            },
            "results": [
                {
                    "id": r.case.id,
                    "expected_decision": r.case.expected_decision,
                    "expected_uri": r.case.expected_uri,
                    "actual_decision": r.pick.decision,
                    "actual_uri": r.pick.chosen_uri,
                    "confidence": r.pick.confidence,
                    "decision_match": r.decision_match,
                    "uri_match": r.uri_match,
                    "confidently_wrong": r.confidently_wrong,
                    "latency_seconds": r.latency_seconds,
                    "rationale": r.pick.rationale,
                }
                for r in results
            ],
        }
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    typer.echo(render_table(results))
    typer.echo("")
    typer.echo("Summary:")
    typer.echo(summary.render())
    if summary.confidently_wrong:
        typer.echo("")
        typer.echo(
            f"  ⚠  {summary.confidently_wrong} confidently-wrong pick(s) — review the rationale; "
            "this is the Hakala-class failure mode worth investigating before shipping."
        )


def _default_bundle_output() -> Path:
    """Today's bundle path under ``<repo>/scratchpad/`` (gitignored).

    The bundle is per-day ephemera the operator emails to the
    cataloguer, not a checked-in artefact.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    today = datetime.now(UTC).date().isoformat()
    return Path(__file__).resolve().parents[2] / "scratchpad" / f"review-batch-{today}.zip"


def review_bundle_build_command(
    operator: Annotated[
        str,
        typer.Option(
            "--operator",
            help="Operator name recorded in the bundle manifest.",
        ),
    ],
    stages: Annotated[
        list[str] | None,
        typer.Option(
            "--stage",
            help=(
                "Stage schema id to include (e.g. 'contrib-candidate/1'). "
                "Pass multiple times. Defaults to every registered stage."
            ),
        ),
    ] = None,
    per_category: Annotated[
        int,
        typer.Option(
            "--per-category",
            help=(
                "Per-stratum candidate count for the stratified sample "
                "(default 25). Pass ``0`` to disable the cap and emit "
                "every row of every stage's audit log — used when "
                "investigating a quality regression and the operator "
                "wants the FULL set of LLM decisions in the bundle, "
                "not a stratified sample."
            ),
        ),
    ] = 25,
    seed: Annotated[
        str | None,
        typer.Option(
            "--seed",
            help=(
                "Sampling seed (any string). Default: today's UTC date so "
                "two operators on the same day generate the same batch."
            ),
        ),
    ] = None,
    marc_dir: Annotated[
        Path,
        typer.Option(
            "--marc-dir",
            help=(
                "Source directory of Helmet MARCXML files (one <bib>.xml "
                "per record). Defaults to the operator-machine path."
            ),
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = review_bundle.bundle.DEFAULT_HELMET_MARC_DIR,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Output zip path. Defaults to scratchpad/review-batch-<date>.zip.",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    from_run: Annotated[
        str | None,
        typer.Option(
            "--from-run",
            help=(
                "Build the bundle from a specific pipeline run's contrib-cascade "
                "audit log (runs/<uuid>/contrib-candidates.jsonl) instead of the "
                "corpus-wide pool. Output defaults to "
                "runs/<uuid>/cataloguer-review/bundle.zip and the HTML reviewer "
                "is copied alongside it so the run dir is self-contained for "
                "cataloguer hand-off."
            ),
        ),
    ] = None,
) -> None:
    """Build a cataloguer-review zip bundle.

    Bundle layout: manifest.json + per-stage candidates.jsonl + per-row
    MARCXML sidecars. The cataloguer opens it in
    gold/cataloguer-review.html, reviews, and emails back a results zip
    in the same shape.

    With ``--from-run <uuid>``, the bundle samples from that run's own
    contrib-cascade audit log (cheaper + run-specific) and the produced
    bundle + a copy of the HTML reviewer land under
    ``runs/<uuid>/cataloguer-review/``. Without it, the bundle samples
    from the corpus-wide candidate pool.
    """
    import shutil  # noqa: PLC0415

    pool_overrides: dict[str, Path] = {}
    target: Path
    html_copy_dir: Path | None = None
    if from_run is not None:
        from bffi_pipeline.eval.review_bundle.stages import STAGE_REGISTRY  # noqa: PLC0415

        runs_root = get_settings().runs_root
        run_dir = runs_root / from_run
        if not run_dir.is_dir():
            preview_n = 5
            existing = sorted(p.name for p in runs_root.iterdir() if p.is_dir())
            preview = ", ".join(existing[:preview_n])
            more = " …" if len(existing) > preview_n else ""
            raise typer.BadParameter(
                f"--from-run: run dir {run_dir} does not exist. Available runs: {preview}{more}"
            )
        # Auto-discover every stage's per-run audit log — mirrors
        # the in-chain ``_dispatch_cataloguer_bundle`` behaviour.
        # Stages whose audit log is empty / missing are silently
        # skipped (heuristic-only / skipped-stage runs).
        for schema, handler in STAGE_REGISTRY.items():
            audit = run_dir / handler.audit_filename
            if audit.is_file() and audit.stat().st_size > 0:
                pool_overrides[schema] = audit
        if "contrib-candidate/1" not in pool_overrides:
            # Contrib uses a corpus-wide default pool when no per-run
            # log exists, so its absence isn't an error — but the
            # operator's --from-run intent presumes a per-run pool, so
            # flag it loudly enough that they notice the empty contrib
            # log + can re-enable --llm-contrib-cascade if needed.
            audit = run_dir / "contrib-candidates.jsonl"
            if not audit.is_file():
                typer.echo(
                    f"  note: {audit} absent — contrib stage will sample "
                    "from the corpus-wide pool. (Re-enable --llm-contrib-cascade "
                    "on a future run to get a per-run contrib audit log.)"
                )
        target = output if output is not None else (run_dir / "cataloguer-review" / "bundle.zip")
        html_copy_dir = target.parent
    else:
        target = output if output is not None else _default_bundle_output()

    result = review_bundle.build_bundle(
        output_path=target,
        operator=operator,
        stage_schemas=stages,
        per_category=per_category,
        seed=seed,
        marc_dir=marc_dir,
        pool_overrides=pool_overrides or None,
    )

    if html_copy_dir is not None:
        html_dst = html_copy_dir / "cataloguer-review.html"
        shutil.copy2(review_bundle.bundle.HTML_REVIEWER_PATH, html_dst)
        typer.echo(f"HTML reviewer copied to {html_dst}")

    typer.echo(result.render())


def review_bundle_import_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            help=(
                "Results zip the cataloguer exported from "
                "gold/cataloguer-review.html. Mirrors the build-side "
                "bundle layout with <stage>/results.jsonl files."
            ),
            file_okay=True,
            dir_okay=False,
            exists=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    contrib_gold: Annotated[
        Path | None,
        typer.Option(
            "--contrib-gold",
            help=(
                "Override the contrib stage's gold file path. Defaults to "
                "gold/contrib.jsonl in the repo. Useful for dry-runs and "
                "tests; production imports leave this unset."
            ),
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Import a cataloguer's results zip.

    Dispatches each stage's results.jsonl to its registered handler.
    For contrib, KEEP rows are validated and appended to
    gold/contrib.jsonl with sequential cg-NNNN ids; DISCARD rows are
    dropped; SKIP-FOR-NOW rows surface in the summary.

    P-39's M9 reconciliation work is gated on gold/contrib.jsonl
    reaching ≥ 30 vetted rows; the contrib summary reports the gap.
    """
    overrides: dict[str, Path] = {}
    if contrib_gold is not None:
        overrides["contrib-candidate/1"] = contrib_gold
    result = review_bundle.import_results(
        input_path=input_path,
        gold_overrides=overrides or None,
    )
    typer.echo(result.render())
