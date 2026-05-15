"""M6 SQLite-backed judge-decision cache.

Stores validated :class:`WorkMatchDecision` rows keyed on
``(model_name, prompt_hash, record_a_canonical, record_b_canonical)``.
Writes happen only *after* the LLM response has passed both the
LangChain structural parse and the Boundary-4 semantic validators —
see :func:`bffi_pipeline.stages.m6.runner.judge_pair`. Validation-
failed responses are intentionally not cached so a re-run can recover
once the prompt or model is updated.

P-38 Phase B: extracted from m6/runner.py to keep the runner focused
on the cascade orchestration. No logic change — moves only.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.m6.validation import WorkMatchDecision, WorkRecord

#: Default cache filename under ``BFFI_DATA_DIR``.
CACHE_FILENAME: Final[str] = "judge-cache.sqlite"


def _canonicalise_record(record: WorkRecord) -> str:
    """Stable JSON dump of a record — sorted keys, no nones, ASCII-safe."""
    return record.model_dump_json(exclude_none=True, by_alias=False)


def _cache_key(
    *,
    model_name: str,
    prompt_hash_value: str,
    record_a: WorkRecord,
    record_b: WorkRecord,
) -> str:
    payload = "|".join(
        (
            model_name,
            prompt_hash_value,
            _canonicalise_record(record_a),
            _canonicalise_record(record_b),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JudgeCache:
    """Tiny SQLite-backed cache for validated :class:`WorkMatchDecision`\\ s.

    Writes happen *only* after a response has passed structural and
    semantic validation — see :func:`judge_pair`. Validation-failed
    responses are deliberately not cached so a re-run can recover
    once the model is updated or the prompt is fixed.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS judge_cache (
      cache_key   TEXT PRIMARY KEY,
      model_name  TEXT NOT NULL,
      prompt_hash TEXT NOT NULL,
      decision    TEXT NOT NULL,
      created_at  TEXT NOT NULL
    )
    """

    def __init__(self, path: Path | str):
        self._path = path
        # check_same_thread=False permits cross-thread use of the connection,
        # but the sqlite3 module does NOT serialise concurrent statement
        # execution on a shared connection (an unlocked concurrent
        # ``execute()`` raises ``sqlite3.InterfaceError: bad parameter or
        # other API misuse``, surfaced during the M6_CONCURRENCY=4
        # production-scale run). ``_lock`` serialises every ``execute()`` /
        # ``commit()``. Held only for the SQLite call itself, so contention
        # is negligible compared to the seconds-long LLM call each thread
        # spends elsewhere.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.commit()

    def __enter__(self) -> JudgeCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        with suppress(sqlite3.ProgrammingError), self._lock:
            self._conn.close()

    def get(self, key: str) -> WorkMatchDecision | None:
        """Return the cached decision for ``key`` or ``None`` if not present."""
        with self._lock:
            row = self._conn.execute(
                "SELECT decision FROM judge_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return WorkMatchDecision.model_validate_json(row[0])

    def set(
        self,
        key: str,
        decision: WorkMatchDecision,
        *,
        model_name: str,
        prompt_hash_value: str,
    ) -> None:
        """Insert-or-replace the cached decision for ``key``.

        ``model_name`` and ``prompt_hash_value`` are committed alongside
        the decision for audit; never silently re-cache a decision under
        a different model or prompt.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO judge_cache "
                "(cache_key, model_name, prompt_hash, decision, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    model_name,
                    prompt_hash_value,
                    decision.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._conn.commit()


def default_cache_path() -> Path:
    """Return ``<BFFI_DATA_DIR>/judge-cache.sqlite`` from the live Settings."""
    return get_settings().data_dir / CACHE_FILENAME
