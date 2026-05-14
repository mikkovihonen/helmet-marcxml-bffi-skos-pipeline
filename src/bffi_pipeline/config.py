"""Pydantic Settings: runtime config from environment variables and ``.env``.

Defaults for the four URI namespaces match the committed identifiers listed
in ``CLAUDE.md``. Overriding them is meant for local development only and
must be coordinated with the National Library of Finland before any
production publish.
"""

from __future__ import annotations

import uuid as _uuid
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # URI namespaces (committed; do not change without surfacing the decision).
    work_namespace: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:work:",
        alias="BFFI_WORK_NAMESPACE",
    )
    expression_namespace: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:expression:",
        alias="BFFI_EXPRESSION_NAMESPACE",
    )
    helmet_source_uri: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:source:helmet",
        alias="BFFI_HELMET_SOURCE_URI",
    )
    graph_base: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:graph:",
        alias="BFFI_GRAPH_BASE",
    )

    # Local LLM endpoint.
    llm_base_url: str = Field(
        default="http://localhost:11434/v1",
        alias="LLM_BASE_URL",
    )
    # Per-tier base URLs for the M6 cascade. Empty string means
    # "use ``llm_base_url`` for this tier" (backward compatibility
    # with single-server setups such as Ollama). When set, the
    # cascade's primary judge calls route to ``llm_base_url_primary``
    # and the fallback judge calls route to ``llm_base_url_fallback``
    # — needed for mlx-lm where one process serves one model and the
    # cascade has to hop between two ports. Plan P-02 § D1.
    llm_base_url_primary: str = Field(
        default="",
        alias="LLM_BASE_URL_PRIMARY",
    )
    llm_base_url_fallback: str = Field(
        default="",
        alias="LLM_BASE_URL_FALLBACK",
    )
    llm_api_key: str = Field(default="ollama", alias="LLM_API_KEY")
    llm_model_primary: str = Field(
        default="qwen3:32b-q4_K_M",
        alias="LLM_MODEL_PRIMARY",
    )
    llm_model_fallback: str = Field(
        default="qwen2.5:72b-instruct-q4_K_M",
        alias="LLM_MODEL_FALLBACK",
    )
    # Per-call wall-time ceiling. Calibrated against the observed
    # steady-state ~28 s/decision on qwen3:8b — 90 s is ~3x the
    # median, so legitimate long-rationale generations stay inside
    # the budget while a wedged Ollama session is bounded. See
    # ``docs/plans/completed/p-03-m6-stall-watchdog.md`` for the
    # design rationale and the dry-run procedure that calibrates this.
    llm_call_timeout_seconds: int = Field(
        default=90,
        alias="LLM_CALL_TIMEOUT_SECONDS",
    )
    # Per-pair wall-time ceiling for the whole M6 cascade
    # (primary + fallback + retries). 300 s is ~3-5x a typical
    # all-tier pass; orthogonal to the per-call ceiling above —
    # catches the pile-up case where many legitimate calls add up
    # to a long pair even when no single call exceeds the per-call
    # budget. See the same plan, Phase B.
    llm_pair_timeout_seconds: int = Field(
        default=300,
        alias="LLM_PAIR_TIMEOUT_SECONDS",
    )
    # M9 reconcile concurrency. ThreadPoolExecutor max_workers for
    # the picker (tier-2) dispatch in
    # ``bffi_pipeline.stages.reconcile.apply_reconciliation``.
    # tier-0 / tier-1 / tier-3 stay single-threaded — they're cheap
    # and write back to the canonical graph. Default 4 matches
    # M6's c=4 throughput knee on M2 Max (P-02 § A6). Setting to 1
    # restores the pre-Phase-A sequential behaviour without code
    # revert. See ``docs/plans/backlog/p-10-m9-reconcile-throughput.md``.
    m9_concurrency: int = Field(
        default=4,
        alias="M9_CONCURRENCY",
    )
    # M9 Phase 1 concurrency. ThreadPoolExecutor max_workers for the
    # serial pre-pass (tier-0 local SPARQL + Finto/VIAF candidate
    # query) inside ``apply_reconciliation``. Separate from
    # ``m9_concurrency`` (which bounds tier-2 picker dispatch by GPU)
    # because Phase 1's binding constraint is HTTP / SPARQL throughput
    # on Fuseki + Finto + VIAF, which tolerate higher concurrency than
    # mlx-lm. Default 8 — Phase A's 2026-05-12 bench surfaced that
    # this pre-pass is ~70 % of M9 wall, so this lever is what closes
    # the gap to the plan's original >=3x speedup target. See
    # ``docs/plans/in-progress/p-10-m9-reconcile-throughput.md``
    # Phase A2 + the snapshot at
    # ``docs/performance/2026-05-12-5k-m2-max-phase-a.md``.
    m9_phase1_concurrency: int = Field(
        default=8,
        alias="M9_PHASE1_CONCURRENCY",
    )
    # Per-field wall-time ceiling for the M9 picker. M9 analogue of
    # ``llm_pair_timeout_seconds``: the outer budget that catches a
    # hung tier-2 call from sterilising a worker thread under
    # ``m9_concurrency``. The picker's connection-error retry stack
    # is 5 / 30 / 120 s backoff x 3 retries + per-call timeout, so
    # 180 s absorbs ~1 retry comfortably; a field that exceeds the
    # budget is treated as stuck, marked
    # ``bffi-prov:stage = "watchdog-aborted"``, and falls through to
    # tier-3 fallback (highest-lexical + needs-review). 0 disables
    # the budget — used by tests and by the Phase A rollback knob.
    # Re-tune against ``docs/performance/`` snapshots if the 5k bench
    # surfaces non-zero ``field_budget_exceeded`` events.
    llm_m9_field_timeout_seconds: int = Field(
        default=180,
        alias="LLM_M9_FIELD_TIMEOUT_SECONDS",
    )
    # P-10 Phase C: tier-0 expansion (folded-lookup + altLabel inclusion
    # via the ``bffi:foldedLabel`` predicate that ``load-finto`` materialises).
    # Default off: the feature is gated by the 200-sample cataloguer audit
    # in plan § C.5. Operators flip this to True only after the audit
    # confirms zero false-positive bindings on their corpus. When off, the
    # resolver falls back to the pre-Phase-C exact-string match path —
    # byte-stable with the post-Phase-A2 behaviour.
    m9_tier0_expansion: bool = Field(
        default=False,
        alias="BFFI_M9_TIER0_EXPANSION",
    )
    # P-10 Phase E: ordering of the deferred picker queue before
    # ThreadPoolExecutor dispatch. ``submission`` (default) preserves the
    # walk order ``_collect_requests`` yielded. ``prefix-cache`` sorts by
    # (request.kind, candidates[0].source_vocabulary, sorted-candidate-URI
    # fingerprint, request.literal) so consecutive picker calls share the
    # longest possible prompt prefix — intended to lift mlx-lm's
    # prompt-prefix cache hit rate.
    #
    # 2026-05-13 A/B bench (docs/performance/2026-05-13-5k-m2-max-phase-e.md)
    # showed ``prefix-cache`` produced a +5.0 % picker-phase wall regression
    # vs ``submission`` on the heterogeneous 5 k cataloguer-curated sample,
    # failing the Phase E ≥ 5 % reduction gate. Default reverted to
    # ``submission``; ``prefix-cache`` stays available behind the env var
    # for future re-benches against more-homogeneous corpora (e.g. a
    # YSO-heavy slice). Byte-stability is guaranteed under both values
    # because the orchestrator sorts results by submission ``idx`` before
    # graph-write.
    m9_picker_ordering: str = Field(
        default="submission",
        alias="BFFI_M9_PICKER_ORDERING",
    )
    # P-16 Knob A: separate lexical floor for the tier-3 fallback path.
    # When the LLM picker returns ``uncertain`` or low-confidence, the
    # current code falls back to the highest-lexical candidate (flagged
    # ``needs-review``) provided its similarity clears
    # :data:`bffi_pipeline.stages.reconcile.LEXICAL_FLOOR` (0.70). Some
    # namesake-rich authorities (KANTO / VIAF) routinely produce
    # lexical-similar wrong-person matches between 0.70 and 0.85; raising
    # this floor forces those into ``no-candidate`` instead of a
    # provisional bind. Default ``0.70`` preserves current behaviour.
    m9_lexical_fallback_floor: float = Field(
        default=0.70,
        alias="BFFI_M9_LEXICAL_FALLBACK_FLOOR",
    )
    # P-16 Knob B: per-source-vocabulary override of the fallback floor.
    # JSON-encoded dict via the env var:
    # ``BFFI_M9_LEXICAL_FALLBACK_FLOOR_PER_VOCAB='{"finaf":0.85,"viaf":0.85}'``.
    # An entry here overrides ``m9_lexical_fallback_floor`` for that
    # vocabulary's candidates. Vocabularies not listed fall through to
    # the global floor.
    m9_lexical_fallback_floor_per_vocab: dict[str, float] = Field(
        default_factory=dict,
        alias="BFFI_M9_LEXICAL_FALLBACK_FLOOR_PER_VOCAB",
    )
    # P-16 Knob C: hard-disable the tier-3 fallback path. When ``True``,
    # picker-uncertain outcomes return ``no-candidate`` instead of binding
    # the highest-lexical candidate with ``needs-review`` set. Trades the
    # recovery path for misspellings / alternate forms (which the LLM
    # picker correctly routes through tier-2 today) against eliminating
    # all provisional binds for cataloguers to review. Default ``False``
    # preserves current behaviour.
    m9_disable_fallback: bool = Field(
        default=False,
        alias="BFFI_M9_DISABLE_FALLBACK",
    )
    # P-10 Phase B: emergency-disable knob for the persistent picker
    # decision cache (``<data_dir>/reconcile-cache.sqlite``). When
    # ``True`` the CLI does not construct a :class:`PickerCache`, so
    # ``apply_reconciliation`` runs the picker for every deferred entry
    # — the post-Phase-A2 + Phase-E behaviour. Mirrors M6's pattern of
    # having the cache always on by default with an env-var rollback.
    # Useful for benching (one run with cache, one without) and for
    # recovering when a corrupted SQLite file would otherwise block a
    # production run.
    m9_cache_disabled: bool = Field(
        default=False,
        alias="BFFI_M9_CACHE_DISABLED",
    )
    # P-12 Phase D: cadence for M9 progress events (Phase 1's tier-0 /
    # candidate-query walk AND Phase 2's picker-pool dispatch). Default
    # 200 matches the value the orchestrator constants used pre-P-12;
    # operators bench-tuning can crank it down (e.g. 50) on short
    # corpora where the default leaves Phase 2 too quiet for the
    # dashboard's m9 progress panel. 0 disables emission entirely.
    m9_progress_cadence: int = Field(
        default=200,
        alias="BFFI_M9_PROGRESS_CADENCE",
    )
    # P-11 Phase A: per-invocation structured event stream.
    # ``observability_sidecar`` is the canonical JSONL file every stage
    # appends to (start / progress / phase_boundary / end / health /
    # watchdog). Set to "" to disable (stderr emission only — useful for
    # tests, the operator-side rollback knob). Empty string is treated
    # as ``None`` by the CLI's emitter-construction code.
    observability_sidecar: str = Field(
        default="",
        alias="BFFI_OBSERVABILITY_SIDECAR",
    )
    # ``run_uuid`` anchors every event from one CLI invocation. Empty
    # default = the CLI auto-generates a fresh UUID at command entry; an
    # explicit value lets the operator pin a run identity across nested
    # invocations or replay scenarios.
    run_uuid: str = Field(
        default="",
        alias="BFFI_RUN_UUID",
    )
    # P-32 Phase A: human-readable description recorded in
    # ``<data_dir>/bffi-run.json``. Shows in ``bffi-pipeline runs list``;
    # operator can edit post-hoc via ``runs tag``. Empty default.
    run_description: str = Field(
        default="",
        alias="BFFI_RUN_DESCRIPTION",
    )
    # P-32 Phase E: canonical root for run dirs. Phase A persists the
    # setting so later phases can read it; Phase E switches
    # ``data_dir`` resolution to ``runs_root / run_uuid``. Default is
    # ``<repo>/runs/`` — the parent directory of ``src/bffi_pipeline``
    # is the repo root in the local-clone case.
    runs_root: Path = Field(
        default=Path(__file__).resolve().parents[2] / "runs",
        alias="BFFI_RUNS_ROOT",
    )

    # Shared (NOT per-run) authority-vocabulary cache. ``load-finto``
    # writes the SKOS Turtle dumps here; M9 reconcile reads them for
    # tier-0 prefLabel lookup. Decoupled from ``data_dir`` so the
    # ~850 MB vocab cache survives ``data_dir`` cleanup and isn't
    # duplicated across per-run dirs under ``runs_root``.
    finto_dump_dir: Path = Field(
        default=Path(__file__).resolve().parents[2] / "finto-dumps",
        alias="BFFI_FINTO_DUMP_DIR",
    )

    # Triple store.
    fuseki_url: str = Field(
        default="http://localhost:3030/bffi",
        alias="FUSEKI_URL",
    )
    # Prometheus admin API endpoint (P-32 Phase G). The local
    # docker-compose maps container 9090 → host 9091 to avoid the
    # Skosmos port collision; the Prometheus container is started
    # with ``--web.enable-admin-api`` so ``reset_prometheus`` can
    # delete a pruned run_uuid's series via
    # ``/api/v1/admin/tsdb/delete_series``.
    prometheus_url: str = Field(
        default="http://localhost:9091",
        alias="BFFI_PROMETHEUS_URL",
    )
    # P-32 Phase H: pre-run Fuseki clear.
    # - "true" (default): best-effort clear at the start of every
    #   pipeline-driving run; warn + continue if Fuseki is unreachable.
    # - "false": skip the clear entirely. Operator-side opt-out for
    #   debugging / side-by-side comparison runs.
    # - "strict": fatal error if Fuseki is unreachable. For operators
    #   who want a reproducibility guarantee — the run only proceeds
    #   if Fuseki is in the known-clean state.
    fuseki_clear_on_run_start: str = Field(
        default="true",
        alias="BFFI_FUSEKI_CLEAR_ON_RUN_START",
    )
    # P-32 Phase H: safety threshold per named graph for the pre-run
    # clear. A graph larger than this gets skipped + warned about
    # unless the operator passes --force-clear; catches the
    # misconfigured-``graph_base`` failure mode where a vocabulary URI
    # accidentally prefix-matches the pipeline output namespace.
    # 100 M is well above any single pipeline run's graph size and
    # well below the smallest Finto vocab (YSO is ~5 M triples).
    fuseki_clear_max_triples: int = Field(
        default=100_000_000,
        alias="BFFI_FUSEKI_CLEAR_MAX_TRIPLES",
    )
    # Fuseki credentials are optional. The Apache Jena Fuseki 5 image
    # protects the Graph Store Protocol (`/<dataset>/data`) endpoints
    # behind admin auth by default; M10 phase 2 needs them set when
    # the dataset is locked down. Empty defaults preserve the existing
    # anonymous-access behavior for instances configured that way.
    fuseki_user: str = Field(default="", alias="FUSEKI_USER")
    fuseki_password: str = Field(default="", alias="FUSEKI_PASSWORD")

    # Filesystem layout.
    # P-32 Phase E: ``data_dir`` resolution rule:
    #   1. If ``BFFI_DATA_DIR`` is explicitly set (env var or ``.env`` row),
    #      use it (operator-side escape hatch — e.g. reproducing a legacy
    #      run's exact paths).
    #   2. Otherwise, derive ``data_dir = runs_root / run_uuid`` so every
    #      new run lands at a canonical, uuid-keyed location.
    # The model-validator below implements (2) by detecting whether
    # ``data_dir`` is in ``model_fields_set`` (pydantic's signal for "the
    # operator explicitly provided this"). The literal default below is
    # the fallback when neither path resolves — kept for shape, never
    # observed in practice.
    data_dir: Path = Field(default=Path("./data"), alias="BFFI_DATA_DIR")
    logs_dir: Path = Field(default=Path("./logs"), alias="BFFI_LOGS_DIR")
    eval_dir: Path = Field(default=Path("./eval-runs"), alias="BFFI_EVAL_DIR")
    config_dir: Path = Field(default=Path("./config"), alias="BFFI_CONFIG_DIR")

    @model_validator(mode="after")
    def _resolve_run_identity(self) -> Settings:
        """P-32 Phase E: derive ``data_dir`` from ``runs_root / run_uuid`` by default.

        Order of operations:
        1. Snapshot ``model_fields_set`` membership BEFORE any writes
           below — Pydantic v2's ``self.data_dir = ...`` direct
           attribute assignment in an ``after`` validator DOES add the
           field name to ``model_fields_set``, which would flip the
           "operator-supplied?" signal on subsequent inspection. The
           snapshot is what the CLI's startup-log echo reads to mark
           the canonical-vs-override case.
        2. If ``run_uuid`` is empty (not set via env), generate fresh
           ``uuid4().hex``. The CLI's ``_init_observability`` previously
           did this; moving it here means the value is available before
           the ``data_dir`` resolution runs.
        3. If ``data_dir`` wasn't explicitly provided, derive it as
           ``runs_root / run_uuid``.

        Writes go through ``self.__dict__`` to bypass Pydantic's
        field-set tracking — we don't want internally-derived values
        to look operator-supplied to downstream inspection.
        """
        data_dir_was_explicit = "data_dir" in self.model_fields_set
        run_uuid_was_explicit = bool(self.run_uuid.strip())

        if not run_uuid_was_explicit:
            self.__dict__["run_uuid"] = _uuid.uuid4().hex
            self.model_fields_set.discard("run_uuid")
        if not data_dir_was_explicit:
            self.__dict__["data_dir"] = self.runs_root / self.run_uuid
            self.model_fields_set.discard("data_dir")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide :class:`Settings` singleton (lazy, cached)."""
    return Settings()
