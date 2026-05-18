"""Per-run manifest (`bffi-run.json`) — P-32 Phase A.

Each pipeline invocation writes / updates a `bffi-run.json` next to
its other outputs under `BFFI_DATA_DIR`. The manifest is the data
model the `bffi-pipeline runs` CLI command tree reads to enumerate,
filter, tag, and prune runs; it also carries the run's
identification (run_uuid, started_at, ended_at, description) plus
the per-stage lifecycle markers (stages_observed, stages_completed)
that the dashboard ingests via the metrics exporter.

Schema is forward-compatible: the `RunManifest` Pydantic model
accepts unknown top-level fields (extra="allow"), and the
`update_manifest_field` helper round-trips through a dict rather
than the model so unknown fields survive partial updates. Future
phases can add fields without coordinating with earlier code paths.

Atomic write: every write goes via `.tmp` + `os.replace`. A crashed
writer leaves the previous file intact (never half-written).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Manifest filename, written into each run's data_dir.
MANIFEST_FILENAME = "bffi-run.json"

#: Status values the manifest's `status` field accepts.
#: - ``running`` — pipeline init wrote the manifest; no end yet.
#: - ``completed`` — atexit hook (or explicit ``runs mark-complete``) wrote ``ended_at``.
#: - ``aborted`` — pipeline crashed; operator ran ``runs mark-complete --status=aborted``.
#: - ``adopted-legacy`` — synthesised by Phase F's ``runs migrate`` for pre-P-32 dirs.
#: - ``unknown`` — manifest exists but its provenance can't be determined (rare).
RunStatus = Literal["running", "completed", "aborted", "adopted-legacy", "unknown"]

#: Max length of the operator-supplied description. Long enough for
#: "Q2 production trial after P-22 lands"; short enough to stay on one
#: line in `runs list` output.
DESCRIPTION_MAX_CHARS = 256

#: Module-level lock serialises in-process updates so M9's threaded
#: c=4 picker + phase1=8 Phase 1 pools don't race when appending stage
#: events to the manifest. Cross-process concurrent writers aren't
#: protected (single-process pipeline assumption — see P-32 R1).
_MANIFEST_LOCK = threading.Lock()


class RunManifest(BaseModel):
    """Schema for ``bffi-run.json``.

    Forward-compatible: ``extra="allow"`` keeps any unknown top-level
    fields a future phase has added. Use :func:`update_manifest_field`
    (dict-level) for one-off writes that should preserve extras
    without requiring a schema bump.
    """

    model_config = ConfigDict(extra="allow")

    run_uuid: str
    started_at: datetime
    ended_at: datetime | None = None
    bffi_data_dir: str
    description: str = Field(default="", max_length=DESCRIPTION_MAX_CHARS)
    pipeline_git_sha: str | None = None
    pipeline_version: str | None = None
    stages_observed: list[str] = Field(default_factory=list)
    stages_completed: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: RunStatus = "running"
    #: Populated by P-32 Phase H (pre-run Fuseki clear) when it ships.
    #: Carries ``{"dropped_graphs": [...], "skipped_oversized": [...],
    #: "total_triples_before": N, "ts": <iso8601>}``. None on runs
    #: that didn't trigger the clear (legacy, --no-clear-fuseki).
    pre_run_fuseki_clear: dict[str, Any] | None = None

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: str) -> str:
        return v.strip()


# --- I/O helpers ----------------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` via ``.tmp`` + ``os.replace``.

    Crashed writers leave the previous file intact. JSON serialised
    with ``indent=2`` + ``ensure_ascii=False`` for operator-readable
    diffs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _json_default(value: Any) -> str:
    """Serialise datetimes as ISO-8601 with explicit UTC offset."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return str(dt.isoformat())
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def read_manifest(path: Path) -> RunManifest:
    """Parse ``path`` into a :class:`RunManifest`.

    Unknown top-level fields land in ``model_extra`` and survive any
    subsequent :func:`write_manifest` call.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return RunManifest.model_validate(raw)


def write_manifest(path: Path, manifest: RunManifest) -> None:
    """Serialise ``manifest`` (including any ``model_extra`` fields) to ``path``.

    Atomic; safe to call from multiple threads in one process via the
    module-level lock.
    """
    with _MANIFEST_LOCK:
        payload = manifest.model_dump(mode="json", exclude_none=False)
        _atomic_write_json(path, payload)


def update_manifest_field(path: Path, **kwargs: Any) -> None:
    """Read-modify-write helper that preserves unknown top-level fields.

    Bypasses the :class:`RunManifest` model — dict-level update — so a
    future phase that adds a field this code doesn't know about
    doesn't lose it when an earlier code path bumps another field.
    Caller's ``kwargs`` are applied as-is to the top-level dict.

    No-op (with warning logged via the value side-channel) if ``path``
    doesn't exist. Atomic write; module-locked.
    """
    with _MANIFEST_LOCK:
        if not path.is_file():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, value in kwargs.items():
            payload[key] = _normalise_for_json(value)
        _atomic_write_json(path, payload)


def _normalise_for_json(value: Any) -> Any:
    """Convert datetimes to ISO strings; pass other values through."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return value


# --- Lifecycle helpers ----------------------------------------------------


def write_initial_manifest(
    data_dir: Path,
    *,
    run_uuid: str,
    description: str = "",
    pipeline_git_sha: str | None = None,
    pipeline_version: str | None = None,
    started_at: datetime | None = None,
) -> Path:
    """Write the initial ``bffi-run.json`` for a fresh pipeline invocation.

    Returns the manifest path so the caller can store it (typically
    on the :class:`StageEventEmitter` so per-stage emits can update it).
    """
    manifest = RunManifest(
        run_uuid=run_uuid,
        started_at=started_at or datetime.now(UTC),
        bffi_data_dir=str(data_dir.resolve()),
        description=description,
        pipeline_git_sha=pipeline_git_sha,
        pipeline_version=pipeline_version,
        status="running",
    )
    path = data_dir / MANIFEST_FILENAME
    write_manifest(path, manifest)
    return path


def append_stage_observed(path: Path, stage: str) -> None:
    """Append ``stage`` to ``stages_observed`` if not already present.

    Idempotent — repeat calls don't duplicate entries. No-op if the
    manifest doesn't exist (e.g. emit fires before init wrote one;
    shouldn't happen in practice but the helper stays defensive).
    """
    _append_idempotent(path, field="stages_observed", value=stage)


def append_stage_completed(path: Path, stage: str) -> None:
    """Append ``stage`` to ``stages_completed`` if not already present.

    Idempotent — see :func:`append_stage_observed`.
    """
    _append_idempotent(path, field="stages_completed", value=stage)


def _append_idempotent(path: Path, *, field: str, value: str) -> None:
    """Shared implementation for the idempotent stage-list appenders."""
    with _MANIFEST_LOCK:
        if not path.is_file():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = payload.get(field) or []
        if value not in existing:
            existing.append(value)
            payload[field] = existing
            _atomic_write_json(path, payload)


def mark_run_complete(data_dir: Path, status: RunStatus = "completed") -> None:
    """Stamp ``ended_at`` + ``status`` on the run's manifest.

    Called from a CLI ``atexit`` hook for the happy path; callable
    manually via ``bffi-pipeline runs mark-complete`` for the
    crash-recovery path.
    """
    path = data_dir / MANIFEST_FILENAME
    update_manifest_field(
        path,
        ended_at=datetime.now(UTC),
        status=status,
    )


# --- Discovery + filters (P-32 Phase B / C / D shared) ------------------


@dataclass(frozen=True)
class DiscoveredRun:
    """One manifested run found under ``BFFI_RUNS_ROOT``.

    The ``manifest`` field is the parsed :class:`RunManifest`; ``path``
    is the on-disk run dir. ``size_bytes`` is computed eagerly by the
    caller when needed — directory walks are expensive enough that we
    don't want them implicit in attribute access.
    """

    manifest: RunManifest
    path: Path


def discover_runs(runs_root: Path) -> list[DiscoveredRun]:
    """Walk ``runs_root`` and return every dir with a parseable ``bffi-run.json``.

    Returns runs in ascending ``started_at`` order. Dirs without a
    manifest are silently skipped — they're legacy / non-canonical
    and outside this plan's managed scope (post Phase F drop, see
    plan's "What this plan does NOT do"). Operator can adopt them
    case-by-case via the deferred ``runs adopt`` command if a need
    surfaces.
    """
    if not runs_root.is_dir():
        return []
    out: list[DiscoveredRun] = []
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_manifest(manifest_path)
        except ValueError, OSError:
            # Corrupt or unreadable manifest — skip with no fanfare;
            # the operator can ``runs info <uuid>`` for the per-run
            # diagnostic.
            continue
        out.append(DiscoveredRun(manifest=manifest, path=child))
    out.sort(key=lambda r: r.manifest.started_at)
    return out


def discover_legacy_dirs(runs_root: Path) -> list[DiscoveredRun]:
    """Walk ``runs_root`` and return synthesised :class:`DiscoveredRun` records
    for directories that lack a parseable ``bffi-run.json``.

    Used by ``bffi-pipeline runs list --include-legacy`` so the operator
    can see what's in their ``BFFI_RUNS_ROOT`` even before adopting
    historical dirs into the canonical manifest schema. The synth
    manifest carries ``run_uuid=legacy-<sha1[:8]>``, ``status="unknown"``,
    ``description=<dirname>``, and ``started_at`` from the dir's mtime.
    Synth values are unstable across filesystems / mounts; do NOT pass
    them to ``runs prune`` / ``runs tag`` / etc — they exist purely so
    legacy dirs render in ``runs list`` output.
    """
    if not runs_root.is_dir():
        return []
    out: list[DiscoveredRun] = []
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILENAME
        if manifest_path.is_file():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        digest = hashlib.sha1(child.name.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        synth = RunManifest(
            run_uuid=f"legacy-{digest}",
            started_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            bffi_data_dir=str(child),
            description=child.name,
            status="unknown",
        )
        out.append(DiscoveredRun(manifest=synth, path=child))
    out.sort(key=lambda r: r.manifest.started_at)
    return out


def compute_dir_size(path: Path) -> int:
    """Recursive file-size sum for one run dir.

    Best-effort: unreadable files are silently skipped (counted as 0
    bytes). The pre-flight output uses this to show the operator how
    much they're about to free; an undercount due to permission
    issues is acceptable — they'll see the right size for the files
    the prune actually deletes.
    """
    total = 0
    if not path.is_dir():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


_DURATION_RE = re.compile(r"^(\d+)(d|w|mo|y)$")


def parse_duration(s: str) -> timedelta:
    """Parse an operator-style duration string into a :class:`timedelta`.

    Recognised suffixes:

    - ``d`` — days (``30d`` = 30 days)
    - ``w`` — weeks (``2w`` = 14 days)
    - ``mo`` — months, approximated as 30 days (``6mo`` = 180 days)
    - ``y`` — years, approximated as 365 days (``1y`` = 365 days)

    Approximate units are sufficient for "older-than X" filters —
    operators don't expect calendar-precise semantics. Raises
    :class:`ValueError` on malformed input.
    """
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise ValueError(f"Invalid duration {s!r}; expected NN<unit> with unit in {{d, w, mo, y}}.")
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    if unit == "mo":
        return timedelta(days=30 * n)
    # "y"
    return timedelta(days=365 * n)


def select_for_pruning(
    runs: list[DiscoveredRun],
    *,
    older_than: timedelta | None = None,
    statuses: list[str] | None = None,
    tags: list[str] | None = None,
    keep_last: int | None = None,
    keep_tagged: bool = False,
    now: datetime | None = None,
) -> tuple[list[DiscoveredRun], list[DiscoveredRun]]:
    """Apply prune filters; return ``(to_delete, preserved)``.

    Selection logic:

    1. Start with all candidates matching the inclusion filters
       (``older_than``, ``statuses``, ``tags``). A run is a candidate
       iff ALL provided filters match.
    2. Apply ``keep_last`` — the N most-recent runs (by
       ``started_at`` descending) are preserved across the entire
       discovered set, not just the candidate set. So
       ``--keep-last 5`` always preserves the five newest runs,
       even if they match ``--older-than`` (unusual case).
    3. Apply ``keep_tagged`` — any run with at least one tag is
       preserved.
    4. ``to_delete`` is the candidate set minus the preserved set;
       ``preserved`` is the subset of the discovered set that was
       in the candidate set but rescued by ``keep_*``.

    ``now`` is injectable so tests can pin "what counts as old".
    """
    if not runs:
        return [], []

    current = now if now is not None else datetime.now(UTC)

    def _matches(run: DiscoveredRun) -> bool:
        if older_than is not None:
            started = run.manifest.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if current - started < older_than:
                return False
        if statuses and run.manifest.status not in statuses:
            return False
        if tags:
            run_tags = set(run.manifest.tags)
            if not all(t in run_tags for t in tags):
                return False
        return True

    candidates = [r for r in runs if _matches(r)]

    # Compute preserved set across all runs (not just candidates).
    by_started_desc = sorted(runs, key=lambda r: r.manifest.started_at, reverse=True)
    keep_last_set: set[Path] = set()
    if keep_last is not None and keep_last > 0:
        keep_last_set = {r.path for r in by_started_desc[:keep_last]}
    keep_tagged_set: set[Path] = set()
    if keep_tagged:
        keep_tagged_set = {r.path for r in runs if r.manifest.tags}

    preserved_paths = keep_last_set | keep_tagged_set
    to_delete = [r for r in candidates if r.path not in preserved_paths]
    preserved = [r for r in candidates if r.path in preserved_paths]
    return to_delete, preserved


__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "MANIFEST_FILENAME",
    "DiscoveredRun",
    "RunManifest",
    "RunStatus",
    "append_stage_completed",
    "append_stage_observed",
    "compute_dir_size",
    "discover_legacy_dirs",
    "discover_runs",
    "mark_run_complete",
    "parse_duration",
    "read_manifest",
    "select_for_pruning",
    "update_manifest_field",
    "write_initial_manifest",
    "write_manifest",
]
