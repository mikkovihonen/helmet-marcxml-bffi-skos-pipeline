"""Stream bibs out of the Sierra Postgres view as one MARCXML file per row.

Migrated from the standalone ``helmet-sierra-data-tools`` repo
(archived). The export combines three concerns:

1. Stream rows asynchronously from a Sierra ``sierra_view`` Postgres
   replica using SQLAlchemy + asyncpg, one bib per row.
2. Per row, build a pymarc :class:`Record` from leader + varfields +
   controlfields, synthesising the MARC 21 fields Sierra writes at
   export time but does NOT persist (``001`` from
   ``record_metadata.record_num``, ``003`` from the agency code,
   ``005`` from ``record_last_updated_gmt``, ``907 $a`` from the
   Sierra system number with its check digit). One ``852`` is emitted
   per linked non-suppressed item (location / call-number / barcode /
   copy-num).
3. Write ``<record_num>.xml`` atomically into the output directory.

These records feed M2 (``bffi-pipeline marc-to-bf``) verbatim. The
pipeline downstream of M2 assumes the MARCXML is well-formed — the
synthesis here is what makes that assumption hold (in particular
``001`` MUST be populated, otherwise marc2bibframe2 falls back to its
``id1`` placeholder and every empty-001 record collides into the same
canonical Work at M8 time — the "SupaRed" bug surfaced on the
200-record corpus smoke).

Configuration via env vars (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD,
DB_NAME); a ``.env`` next to the working directory is picked up via
``python-dotenv``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from importlib.resources import files
from threading import BoundedSemaphore
from typing import Any, Final

from pymarc import Field, Indicators, Record, marcxml
from pymarc import Subfield as PymarcSubfield

from marcxml_export_pipeline.sierra.dtos import Leaderfield, Subfield, Varfield

# --- Constants ----------------------------------------------------------

#: MARC Organization Code written into ``003`` when Sierra would have
#: synthesised it at export time. HelMet's registered code is
#: ``FI-HELME``; adjust for other Sierra deployments by overriding
#: :data:`AGENCY_CODE` or setting ``MARCXML_EXPORT_AGENCY_CODE``.
AGENCY_CODE: Final[str] = os.environ.get("MARCXML_EXPORT_AGENCY_CODE", "FI-HELME")

#: Sierra-local tag carrying the full Sierra system number
#: (``.b<num><check>``) in ``$a``. 907 is the Sierra default; some
#: HelMet exports use 909 instead.
SIERRA_LOCAL_TAG: Final[str] = "907"
SIERRA_BIB_PREFIX: Final[str] = "b"

#: Sierra ``varfield.field_content`` occasionally carries a leading
#: subfield delimiter even on control fields (legacy loads). Strip one
#: such prefix so ``005`` doesn't come out as ``"|a20240101..."``.
_SUBFIELD_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^[\x1f|][a-z0-9]")

#: Same delimiters anywhere in the string. Used to flatten inline-
#: subfield encoding in item varfields (``"|aQA76|bM4"`` →
#: ``"QA76 M4"``) before they go into 852 ``$h``.
_SUBFIELD_DELIM_RE: Final[re.Pattern[str]] = re.compile(r"[\x1f|][a-z0-9]")

#: ``sierra_view.control_field.control_num`` → MARC tag. Sierra stores
#: the MARC tag number directly as an integer; ``varfield_type_code``
#: on this table is uniformly ``'y'`` and is not the discriminator.
CONTROLFIELD_TAGS: Final[dict[int, str]] = {6: "006", 7: "007", 8: "008"}

#: Standard MARC fixed lengths. ``007`` length depends on material
#: category (positions 0-22), so it is trimmed to the non-blank prefix
#: instead of forced to a fixed width.
CONTROLFIELD_FIXED_LENGTHS: Final[dict[str, int]] = {"008": 40, "006": 18}

#: Expected column order from ``sql/all_bibs_marcxml.sql``. The
#: processor validates against this so a schema drift fails the run
#: instead of silently producing garbled records.
EXPECTED_KEYS: Final[tuple[str, ...]] = (
    "record_num",
    "leader",
    "varfields",
    "controlfields",
    "record_last_updated_gmt",
    "items",
)

#: MARC tag boundary: tags ``< "010"`` are control fields (no
#: indicators / subfields); tags ``>= "010"`` are data fields.
_CONTROL_TAG_BOUNDARY: Final[str] = "010"

#: Sierra check-digit algorithm: weighted-sum mod 11; remainder 10
#: maps to literal ``'x'``.
_SIERRA_CHECK_MOD: Final[int] = 11
_SIERRA_CHECK_X: Final[int] = 10

#: Minimum copy_num that triggers a ``$t`` subfield on the 852 datafield.
_COPY_NUM_NEEDS_SUFFIX: Final[int] = 2


# --- Pure helpers (no DB; unit-testable) --------------------------------


def _decode(value: Any) -> Any:
    """Coerce a JSON column to a Python object regardless of driver behavior.

    asyncpg returns ``jsonb`` as already-decoded ``dict``/``list``;
    some drivers return raw text. Accept both.
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _strip_subfield_prefix(value: str | None) -> str:
    if not value:
        return value or ""
    return _SUBFIELD_PREFIX_RE.sub("", value, count=1)


def _flatten_inline_subfields(value: str | None) -> str:
    if not value:
        return value or ""
    return _SUBFIELD_DELIM_RE.sub(" ", value).strip()


def _format_005(ts: datetime | None) -> str | None:
    """MARC 21 005 spec: ``YYYYMMDDHHMMSS.F``. Returns ``None`` for
    missing / non-datetime input so the caller can skip the field."""
    if ts is None or not hasattr(ts, "strftime"):
        return None
    return ts.strftime("%Y%m%d%H%M%S.0")


def sierra_check_digit(record_num: int | str) -> str:
    """Innovative's bib-number check digit: weighted sum mod 11.

    The weighted sum walks the digits right-to-left, multiplying each
    by an index starting at 2. Remainder 10 maps to ``'x'``; other
    remainders are the digit themselves.
    """
    digits = str(record_num)
    total = sum((i + 2) * int(d) for i, d in enumerate(reversed(digits)))
    remainder = total % _SIERRA_CHECK_MOD
    return "x" if remainder == _SIERRA_CHECK_X else str(remainder)


def build_leader(lf: Leaderfield | None) -> str:
    """Render the 24-character MARC 21 leader from the Sierra leader row.

    Single-character code fields default to MARC 21 minimal-record
    conventions (``'n'`` / ``'a'`` / ``'m'``) when NULL. ``base_address``
    is forced to 5 digits.
    """
    if lf is None:
        return "00000nam a2200000   4500"
    base_address_raw = lf.base_address or "0"
    try:
        base_address = f"{int(base_address_raw):05d}"
    except (TypeError, ValueError):
        base_address = "00000"
    return (
        "00000"
        f"{lf.record_status_code or 'n'}"
        f"{lf.record_type_code or 'a'}"
        f"{lf.bib_level_code or 'm'}"
        " "
        f"{lf.char_encoding_scheme_code or 'a'}"
        "22"
        f"{base_address}"
        f"{lf.encoding_level_code or ' '}"
        f"{lf.descriptive_cat_form_code or ' '}"
        f"{lf.multipart_level_code or ' '}"
        "4500"
    )


def build_holdings_fields(item_dicts: list[dict[str, Any]]) -> list[Field]:
    """One 852 datafield per linked item that has at least one populated
    subfield (``$b`` location, ``$h`` call number, ``$p`` barcode,
    ``$t`` copy_num when > 1). Items without any of those are
    skipped so we don't emit empty 852s."""
    fields: list[Field] = []
    for item in item_dicts:
        subfields: list[PymarcSubfield] = []
        location = (item.get("location_code") or "").strip()
        if location:
            subfields.append(PymarcSubfield(code="b", value=location))
        cn = _flatten_inline_subfields(item.get("call_number") or "")
        if cn:
            subfields.append(PymarcSubfield(code="h", value=cn))
        bc = _flatten_inline_subfields(item.get("barcode") or "")
        if bc:
            subfields.append(PymarcSubfield(code="p", value=bc))
        copy_num = item.get("copy_num") or 0
        if copy_num >= _COPY_NUM_NEEDS_SUFFIX:
            subfields.append(PymarcSubfield(code="t", value=str(copy_num)))
        if subfields:
            fields.append(Field(tag="852", indicators=Indicators(" ", " "), subfields=subfields))
    return fields


def build_record(
    leader: Leaderfield | None,
    varfields: list[Varfield],
    controlfields: list[tuple[str, str]],
    extra_fields: list[Field] | None = None,
) -> Record:
    """Build a pymarc :class:`Record` from the structured row payload.

    Tag-ordered stream: varfields, controlfields, and any pre-built
    pymarc Fields (852 holdings, 907 local system number) merge into
    one list sorted by ``(tag, source-id)`` so the resulting MARC
    stays in standard tag order.

    Varfields with ``marc_tag < "010"`` are emitted as
    ``Field(tag, data=field_content)`` (control field shape). Higher
    tags become data fields with indicators + subfields preserved.
    """
    record = Record(leader=build_leader(leader))
    entries: list[tuple[str, int, str, Any]] = []
    for vf in varfields:
        if not vf.marc_tag:
            continue
        entries.append((vf.marc_tag, vf.id or 0, "v", vf))
    for tag, content in controlfields:
        entries.append((tag, 0, "c", content))
    for f in extra_fields or []:
        entries.append((f.tag, 0, "f", f))
    entries.sort(key=lambda e: (e[0], e[1]))

    for tag, _id, kind, payload in entries:
        if kind == "c":
            record.add_field(Field(tag=tag, data=payload))
            continue
        if kind == "f":
            record.add_field(payload)
            continue
        vf = payload
        if tag < _CONTROL_TAG_BOUNDARY:
            content = _strip_subfield_prefix(vf.field_content or "")
            record.add_field(Field(tag=tag, data=content))
            continue
        # ``sf.tag and sf.tag.strip()`` rejects Sierra subfields whose
        # tag is empty, ``None``, or whitespace-only. Sierra
        # occasionally serialises placeholder subfields with
        # ``tag=" "`` (a literal space); the MARC21slim XSD pattern
        # ``[\dA-Za-z!"#$%&'()*+,-./:;<=>?{}_^`~\[\]\\]{1}`` rejects
        # them and the downstream M2 stage kills the whole record.
        # Surfaced on a 5k production-style sample (bib 2553807,
        # 338-field with a trailing ``<subfield code=" " />``).
        subfields = [
            PymarcSubfield(code=sf.tag, value=sf.content or "")
            for sf in sorted(vf.subfields, key=lambda s: s.display_order or 0)
            if sf.tag and sf.tag.strip()
        ]
        record.add_field(
            Field(
                tag=tag,
                indicators=Indicators(vf.marc_ind1 or " ", vf.marc_ind2 or " "),
                subfields=subfields,
            )
        )
    return record


def build_marcxml_for_row(row: tuple[Any, ...]) -> tuple[str, bytes]:
    """Build one MARCXML file (filename + bytes) from a streamed SQL row.

    Pure: no DB, no I/O. Returned ``filename`` is
    ``<record_num>.xml``; ``xml_bytes`` is UTF-8 MARCXML ready to write.
    """
    record_num = row[0]
    leader_dict = _decode(row[1])
    varfield_dicts = _decode(row[2]) or []
    controlfield_dicts = _decode(row[3]) or []
    last_updated = row[4]
    item_dicts = _decode(row[5]) or []

    leader = Leaderfield(**leader_dict) if leader_dict is not None else None

    varfields: list[Varfield] = []
    for vf in varfield_dicts:
        varfield = Varfield(
            id=vf.get("id"),
            marc_tag=vf.get("marc_tag"),
            marc_ind1=vf.get("marc_ind1"),
            marc_ind2=vf.get("marc_ind2"),
            field_content=vf.get("field_content"),
        )
        varfield.subfields = [
            Subfield(
                tag=sf.get("tag"),
                content=sf.get("content"),
                display_order=sf.get("display_order"),
            )
            for sf in (vf.get("subfields") or [])
            if sf and sf.get("tag")
        ]
        varfields.append(varfield)

    controlfields: list[tuple[str, str]] = []
    for cf in controlfield_dicts:
        tag = CONTROLFIELD_TAGS.get(cf.get("control_num"))
        if not tag:
            continue
        raw = cf.get("content") or ""
        fixed_len = CONTROLFIELD_FIXED_LENGTHS.get(tag)
        content = raw[:fixed_len].ljust(fixed_len) if fixed_len is not None else raw.rstrip()
        if not content:
            continue
        controlfields.append((tag, content))

    # Sierra synthesises 001/003/005 + 907 $a at export time rather
    # than persisting them. Fill in whatever neither varfield nor
    # control_field already supplied.
    present_tags = {vf.marc_tag for vf in varfields if vf.marc_tag}
    present_tags.update(t for t, _ in controlfields)

    if "001" not in present_tags and record_num is not None:
        controlfields.append(("001", str(record_num)))
    if "003" not in present_tags:
        controlfields.append(("003", AGENCY_CODE))
    if "005" not in present_tags:
        ts = _format_005(last_updated)
        if ts:
            controlfields.append(("005", ts))

    extra_fields = build_holdings_fields(item_dicts)

    if SIERRA_LOCAL_TAG not in present_tags and record_num is not None:
        sierra_id = f".{SIERRA_BIB_PREFIX}{record_num}{sierra_check_digit(record_num)}"
        extra_fields.append(
            Field(
                tag=SIERRA_LOCAL_TAG,
                indicators=Indicators(" ", " "),
                subfields=[PymarcSubfield(code="a", value=sierra_id)],
            )
        )

    record = build_record(leader, varfields, controlfields, extra_fields)
    xml_bytes: bytes = marcxml.record_to_xml(record, quiet=True, namespace=True)
    return f"{record_num}.xml", xml_bytes


# --- Async streaming entry point ----------------------------------------


SQL_PATH_RESOURCE: Final[str] = "all_bibs_marcxml.sql"


def _load_sql() -> str:
    """Read the bundled ``all_bibs_marcxml.sql`` via ``importlib.resources``."""
    return (
        files("marcxml_export_pipeline.sierra")
        .joinpath("sql")
        .joinpath(SQL_PATH_RESOURCE)
        .read_text(encoding="utf-8")
    )


def _validate_keys(actual: tuple[str, ...]) -> None:
    """Raise if the streamed result-set columns drift from the SQL contract."""
    if tuple(actual) != EXPECTED_KEYS:
        raise ValueError(f"Incompatible SQL result keys: expected {EXPECTED_KEYS}, got {actual}")


def _apply_limit(sql: str, limit: int) -> str:
    """Append a top-level ``LIMIT n`` to the bundled query for smoke runs.

    The bundled SQL ends with ``ORDER BY b.record_id`` and has no
    top-level ``LIMIT``, so plain appending applies to the outermost
    SELECT (not to any of the JSON-aggregating subqueries).
    """
    if limit <= 0:
        raise ValueError(f"--limit must be a positive integer, got {limit}")
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {limit}\n"


class _Pool:
    """ProcessPoolExecutor wrapper that streams ``(filename, bytes)``
    pairs from the row-processing workers into the output directory.

    Bounded by a semaphore so a slow disk doesn't let the per-row
    futures queue grow unbounded.
    """

    def __init__(self, outdir: pathlib.Path, max_workers: int, queue_depth: int) -> None:
        self.outdir = outdir
        self.outdir.mkdir(parents=True, exist_ok=True)
        self._executor = ProcessPoolExecutor(max_workers=max_workers)
        self._semaphore = BoundedSemaphore(queue_depth)

    def submit(self, row: tuple[Any, ...]) -> None:
        self._semaphore.acquire()
        future = self._executor.submit(build_marcxml_for_row, row)
        future.add_done_callback(self._on_done)

    def _on_done(self, future: Any) -> None:
        self._semaphore.release()
        try:
            filename, xml_bytes = future.result()
        except Exception as exc:
            print(f"row-processing error: {exc!s}")
            return
        (self.outdir / filename).write_bytes(xml_bytes)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


async def export_async(
    output_dir: pathlib.Path,
    *,
    batch_size: int = 30_000,
    max_workers: int = 10,
    queue_depth: int = 100,
    sql_override: str | None = None,
) -> None:
    """Stream the SELECT, dispatch each row to a worker, write outputs.

    ``sql_override`` exists so tests / one-off slices can replace the
    bundled query (e.g. an ``ORDER BY ... LIMIT n`` for a sample).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.sql import text

    engine = create_async_engine(
        (
            f"postgresql+asyncpg://"
            f"{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}"
            f"/{os.environ['DB_NAME']}"
        ),
        echo=False,
        pool_size=1,
        max_overflow=0,
    )
    sql = sql_override or _load_sql()
    started = time.perf_counter()
    print("Start MARCXML export.")
    pool = _Pool(output_dir, max_workers=max_workers, queue_depth=queue_depth)
    try:
        async_session = async_sessionmaker(engine, expire_on_commit=False)
        async with async_session() as session:
            stmt = text(sql).execution_options(stream_results=True, yield_per=batch_size)
            result = await session.stream(stmt)
            print(f"Streaming starts at {time.perf_counter() - started:0.2f} s")
            _validate_keys(tuple(result.keys()))
            async for row in result:
                pool.submit(tuple(row))
    finally:
        pool.shutdown()
        await engine.dispose()
    print(f"Finished in {time.perf_counter() - started:0.2f} s")


def main() -> None:
    """CLI entry point — ``marcxml-export-sierra``.

    Reads DB credentials from env (DB_HOST / DB_PORT / DB_USER /
    DB_PASSWORD / DB_NAME); ``.env`` next to CWD is loaded
    automatically via ``python-dotenv``.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # python-dotenv is optional; explicit env vars still work.

    parser = argparse.ArgumentParser(
        prog="marcxml-export-sierra",
        description=(
            "Stream the Sierra Postgres view into MARCXML files, one per "
            "non-suppressed bib record. Output directory receives "
            "<record_num>.xml files ready for `bffi-pipeline marc-to-bf`."
        ),
    )
    parser.add_argument(
        "-p",
        "--path",
        type=pathlib.Path,
        required=True,
        help="Output directory (created if missing).",
    )
    parser.add_argument(
        "-b",
        "--batchsize",
        type=int,
        default=30_000,
        help="Yield interval for async result streaming (default 30000).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Worker pool size for parallel MARCXML rendering (default 10).",
    )
    parser.add_argument(
        "--queue-depth",
        type=int,
        default=100,
        help=(
            "Bounded-semaphore depth limiting in-flight row futures "
            "(default 100) so a slow disk doesn't let memory grow unbounded."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Cap the export at the first N rows after ORDER BY record_id. "
            "Smoke-test only — production runs leave this unset."
        ),
    )
    args = parser.parse_args()

    sql_override = _apply_limit(_load_sql(), args.limit) if args.limit else None

    asyncio.run(
        export_async(
            args.path,
            batch_size=args.batchsize,
            max_workers=args.max_workers,
            queue_depth=args.queue_depth,
            sql_override=sql_override,
        )
    )


if __name__ == "__main__":
    main()


__all__ = [
    "AGENCY_CODE",
    "CONTROLFIELD_FIXED_LENGTHS",
    "CONTROLFIELD_TAGS",
    "EXPECTED_KEYS",
    "SIERRA_BIB_PREFIX",
    "SIERRA_LOCAL_TAG",
    "build_holdings_fields",
    "build_leader",
    "build_marcxml_for_row",
    "build_record",
    "export_async",
    "main",
    "sierra_check_digit",
]
