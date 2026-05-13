"""Structured stage-event emission for P-11.

One canonical event stream that every stage writes to during its run.
Operators tail the sidecar (via ``bffi-pipeline status`` once Phase B
ships, or by hand with ``tail -F``) to answer "is the pipeline making
forward progress?" without composing ``ps`` / ``curl`` /
``docker logs`` / log-grep against three different files.

The shape mirrors :mod:`bffi_pipeline.stages.watchdog` — a stderr line
with a ``STAGE_EVENT `` prefix the existing log-tail tooling can pick
up on, plus an append to a JSONL sidecar at
``<BFFI_DATA_DIR>/stage-events.jsonl`` for post-run analysis. The
canonical payload shape:

::

    {
      "ts": "2026-05-13T05:13:36Z",
      "run_uuid": "01HXXX...",
      "stage": "m9",
      "event": "progress",
      "phase": "phase1",
      "counters": {"processed": 9876, "total": 12666},
      "extra": {"tier0_local": 7421, "no_candidate": 1893}
    }

Module-level active-emitter singleton: the CLI subcommand at entry
calls :func:`set_active_emitter` with a configured
:class:`StageEventEmitter`; stages call :func:`get_active_emitter` and,
if the result is non-None, emit. Stages don't need to thread the
emitter through their function signatures, which would have rippled
through every ``run()`` and every test fixture.

Thread safety: ``StageEventEmitter.emit`` is guarded by an internal
``threading.Lock`` so M9's ``c=4`` picker pool + ``phase1=8`` Phase 1
pool can call it concurrently without interleaving stderr lines or
JSONL appends. ``set_active_emitter`` is *not* thread-safe and is
expected to be called once at CLI entry before any worker dispatch.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

StageEvent = Literal[
    "start",
    "progress",
    "phase_boundary",
    "end",
    "health",
    "watchdog",
]

#: stderr prefix; mirror of :data:`bffi_pipeline.stages.watchdog.WATCHDOG_STDERR_PREFIX`.
#: ``scripts/run-full-pipeline.sh``'s log-tail filter can grow its regex to
#: ``^(STAGE_|PIPELINE_|WATCHDOG_EVENT|STAGE_EVENT)`` to surface these
#: alongside the existing markers.
STAGE_EVENT_STDERR_PREFIX: Final[str] = "STAGE_EVENT "

#: Default progress-emission cadence per stage. Picked to balance
#: ``stage-events.jsonl`` density against ``bffi-pipeline status`` tail
#: responsiveness — too sparse and the dashboard looks frozen; too dense
#: and the sidecar bloats. Tunable per-stage via the call site; this
#: dict is the canonical source of defaults so new stages get a sensible
#: starting value without re-deriving.
DEFAULT_PROGRESS_CADENCE: Final[dict[str, int]] = {
    "m2": 100,
    "m3": 100,
    "m5": 500,
    "m6": 25,
    "m8": 200,
    "m9": 200,
    "skosify": 0,  # 0 = no in-stage progress events, only start/end
    "load": 0,
}


@dataclass
class StageEventEmitter:
    """One emitter per pipeline invocation.

    Constructed by the CLI subcommand at entry. ``sidecar_path`` is the
    canonical ``<BFFI_DATA_DIR>/stage-events.jsonl`` location for
    production; tests pass ``None`` and assert against the stderr
    capture only.

    ``run_uuid`` anchors every event from one CLI invocation; the
    Grafana dashboard (Phase D) uses it to filter views to the current
    run, and the status CLI (Phase B) uses it to scope ``--since now``.
    """

    sidecar_path: Path | None
    run_uuid: str
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(
        self,
        *,
        stage: str,
        event: StageEvent,
        phase: str | None = None,
        counters: dict[str, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit one event to stderr and (if configured) to the JSONL sidecar.

        Concurrent calls are serialised by an internal lock — M9's
        c=4 picker pool + phase1=8 Phase 1 pool can call this from
        multiple threads without interleaving lines.

        ``ts`` is always set to ``datetime.now(UTC)`` formatted as
        ISO-8601 with second precision; callers can't override it
        (avoids the temptation to back-date events for "alignment"
        which then breaks the throughput-derivation math in Phase B's
        ETA calculation).
        """
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_uuid": self.run_uuid,
            "stage": stage,
            "event": event,
        }
        if phase is not None:
            payload["phase"] = phase
        if counters is not None:
            payload["counters"] = counters
        if extra is not None:
            payload["extra"] = extra
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        with self._lock:
            print(f"{STAGE_EVENT_STDERR_PREFIX}{line}", file=sys.stderr, flush=True)
            if self.sidecar_path is not None:
                self.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
                with self.sidecar_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")


#: Module-level singleton slot. ``None`` when no pipeline invocation is
#: active — stages that look it up via :func:`get_active_emitter` and
#: find ``None`` skip emission silently (no crash, no opt-in required).
_active_emitter: StageEventEmitter | None = None


def set_active_emitter(emitter: StageEventEmitter | None) -> None:
    """Set the process-wide active emitter.

    Called by CLI subcommands at entry once they've constructed an
    emitter from ``settings.observability_sidecar`` + ``settings.run_uuid``.
    Passing ``None`` explicitly clears the slot — useful when a test
    needs to verify the "no emitter" path between assertions.

    Not thread-safe; expected to be called once at CLI entry before
    any worker dispatch.
    """
    global _active_emitter  # noqa: PLW0603 — module-level singleton by design; the alternative (thread-local or per-call plumbing) would force every stage's signature to thread the emitter through.
    _active_emitter = emitter


def get_active_emitter() -> StageEventEmitter | None:
    """Return the active emitter (or ``None`` if not set).

    Stages call this and, if non-None, call ``emitter.emit(...)``.
    Pattern at the call site:

    .. code-block:: python

        emitter = get_active_emitter()
        if emitter is not None:
            emitter.emit(stage="m9", event="progress", ...)
    """
    return _active_emitter


def emit_if_active(
    *,
    stage: str,
    event: StageEvent,
    phase: str | None = None,
    counters: dict[str, int] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Convenience helper: emit via the active emitter if one is set.

    Reduces call-site boilerplate from "fetch emitter → null-check →
    call emit" to one line. Stages can use either pattern; this helper
    is the ergonomic default.
    """
    emitter = _active_emitter
    if emitter is not None:
        emitter.emit(
            stage=stage,
            event=event,
            phase=phase,
            counters=counters,
            extra=extra,
        )


__all__ = [
    "DEFAULT_PROGRESS_CADENCE",
    "STAGE_EVENT_STDERR_PREFIX",
    "StageEvent",
    "StageEventEmitter",
    "emit_if_active",
    "get_active_emitter",
    "set_active_emitter",
]
