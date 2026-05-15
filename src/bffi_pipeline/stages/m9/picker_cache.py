"""M9 persistent picker-decision cache (P-10 Phase B).

SQLite-backed cache for validated :class:`PickerDecision` rows keyed
on a SHA-256 of ``(fold(literal), sorted candidate URIs, prompt hash,
model name, vocab:finto_sha)``. The Finto SHA component makes the
cache version-aware: refreshing a Finto vocabulary dump (different
SHA) invalidates the per-vocab slice cleanly on next lookup.

Writes happen only after the picker has returned a structurally and
semantically valid response (mirrors M6's ``JudgeCache`` discipline).
Concurrency: a single :class:`PickerCache` instance is shared across
picker-pool worker threads; a :class:`threading.Lock` serialises every
SQLite call so concurrent ``execute()`` doesn't trip rdflib's bad-
parameter error.

P-38 Phase B: extracted from m9/runner.py to keep the runner focused
on the reconcile orchestration. No logic change — moves only.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from bffi_pipeline.blocking import fold_label
from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.m9.authority_clients import VOCAB_VIAF

if TYPE_CHECKING:
    from bffi_pipeline.stages.m9.picker import PickerDecision
    from bffi_pipeline.stages.m9.schemas import AuthorityCandidate, EntityRequest

#: Default filename. Mirrors M6's ``judge-cache.sqlite`` naming.
PICKER_CACHE_FILENAME: Final[str] = "reconcile-cache.sqlite"


def picker_cache_default_path() -> Path:
    """Return ``<BFFI_DATA_DIR>/reconcile-cache.sqlite`` from live Settings."""
    return get_settings().data_dir / PICKER_CACHE_FILENAME


def hash_finto_dump(path: Path) -> str:
    """SHA-256 of one Finto vocabulary dump file.

    Read in 1 MiB chunks so the hash is bounded by I/O, not by file
    size — KANTO is ~183 MB and LCSH ~465 MB decompressed.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_finto_shas(dumps_dir: Path) -> dict[str, str]:
    """Hash every ``<vocab>-skos.ttl`` in ``dumps_dir``.

    Returns a dict keyed by vocab slug (the part before ``-skos.ttl``).
    Missing / non-existent dumps directories return an empty dict so
    callers distinguish "vocab not cached locally" (skip caching) from
    "vocab cached but refreshed" (key mismatch → cache miss). Computed
    once per :func:`apply_reconciliation` run, not per-call.
    """
    if not dumps_dir.is_dir():
        return {}
    shas: dict[str, str] = {}
    for path in sorted(dumps_dir.glob("*-skos.ttl")):
        vocab = path.stem.removesuffix("-skos")
        shas[vocab] = hash_finto_dump(path)
    return shas


def compute_picker_cache_key(
    *,
    request: EntityRequest,
    candidates: Iterable[AuthorityCandidate],
    prompt_hash_value: str,
    model_name: str,
    finto_shas: dict[str, str],
) -> tuple[str, str, str] | None:
    """Return ``(key, vocab, finto_sha)`` or ``None`` if the call must skip cache.

    Skip conditions (return ``None``):

    - Empty candidate list — picker would short-circuit to ``uncertain``
      anyway, no value in caching.
    - VIAF-source candidates — no local dump to anchor the version,
      so caching would risk binding to upstream-drifted data.
    - Vocab has no on-disk Finto dump (no SHA to invalidate against).

    Otherwise key = SHA-256 of:

    .. code-block:: text

        fold(literal) | sorted(candidate.uri) | prompt_hash |
        model_name | vocab:finto_sha

    ``fold(literal)`` uses :func:`bffi_pipeline.blocking.fold_label`
    so diacritic-equivalent literals hit the same cached decision.
    """
    candidate_list = list(candidates)
    if not candidate_list:
        return None
    vocab = candidate_list[0].source_vocabulary
    if vocab == VOCAB_VIAF:
        return None
    finto_sha = finto_shas.get(vocab)
    if not finto_sha:
        return None
    payload = "|".join(
        (
            fold_label(request.literal),
            ",".join(sorted(c.uri for c in candidate_list)),
            prompt_hash_value,
            model_name,
            f"{vocab}:{finto_sha}",
        )
    )
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return key, vocab, finto_sha


@dataclass(frozen=True)
class CacheHit:
    """One row returned by :meth:`PickerCache.get`.

    Carries the cached :class:`PickerDecision` plus the original
    Activity URI so the orchestrator wires the new provenance Activity
    through ``prov:wasInfluencedBy``.
    """

    decision: PickerDecision
    activity_uuid: str


class PickerCache:
    """SQLite-backed cache for validated :class:`PickerDecision` rows.

    Writes happen only after the picker has returned a structurally
    and semantically valid response (the same gate
    :class:`~bffi_pipeline.stages.m6.JudgeCache` applies): a
    ``ValidationError`` short-circuits before the write so a re-run
    can recover once the model is updated or the prompt is fixed.

    Concurrency contract: one instance is shared across picker-pool
    worker threads. A :class:`threading.Lock` serialises every SQLite
    call (mirroring M6's cross-thread fix in commit ``1452a4f``: the
    ``sqlite3`` module does *not* serialise concurrent statement
    execution on a shared connection). The lock is held only for the
    SQLite call itself, so contention is negligible next to the
    seconds-long LLM call. ``BEGIN IMMEDIATE`` on writes prevents two
    threads from racing on the same key.
    """

    _SCHEMA: Final[str] = """
    CREATE TABLE IF NOT EXISTS picker_cache (
      cache_key       TEXT PRIMARY KEY,
      decision_json   TEXT NOT NULL,
      finto_vocab     TEXT NOT NULL,
      finto_sha       TEXT NOT NULL,
      prompt_hash     TEXT NOT NULL,
      model_name      TEXT NOT NULL,
      activity_uuid   TEXT NOT NULL,
      decided_at      TEXT NOT NULL
    )
    """

    _INDEX: Final[str] = (
        "CREATE INDEX IF NOT EXISTS picker_cache_vocab_sha ON picker_cache(finto_vocab, finto_sha)"
    )

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._INDEX)
            self._conn.commit()

    def __enter__(self) -> PickerCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        with suppress(sqlite3.ProgrammingError), self._lock:
            self._conn.close()

    def get(self, key: str) -> CacheHit | None:
        """Return the cached :class:`CacheHit` for ``key`` or ``None``."""
        from bffi_pipeline.stages.m9.picker import PickerDecision as _PickerDecision

        with self._lock:
            row = self._conn.execute(
                "SELECT decision_json, activity_uuid FROM picker_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return CacheHit(
            decision=_PickerDecision.model_validate_json(row[0]),
            activity_uuid=row[1],
        )

    def set(
        self,
        key: str,
        *,
        decision: PickerDecision,
        finto_vocab: str,
        finto_sha: str,
        prompt_hash_value: str,
        model_name: str,
        activity_uuid: str,
    ) -> None:
        """Insert-or-replace the cached decision for ``key``.

        Uses ``BEGIN IMMEDIATE`` so two worker threads that compute
        the same key concurrently never produce a half-written row —
        the second writer blocks at the BEGIN and then overwrites
        cleanly (idempotent in content; decision JSON / Activity URI
        are deterministic per input).
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO picker_cache "
                    "(cache_key, decision_json, finto_vocab, finto_sha, "
                    "prompt_hash, model_name, activity_uuid, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        decision.model_dump_json(),
                        finto_vocab,
                        finto_sha,
                        prompt_hash_value,
                        model_name,
                        activity_uuid,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
