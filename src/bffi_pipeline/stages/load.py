"""Stage M10 (phase 2): Fuseki load + Boundary-5 smoke + lookup-helmet helpers.

Uploads the M10-phase-1 ``canonical-skosified.ttl`` plus
``config/bffi-admin-vocabulary.ttl`` into the Fuseki ``bffi-works``
named graph, and the M7 ``provenance.ttl`` into the provenance named
graph, all via SPARQL Graph Store Protocol (GSP). Immediately after
the load, the four ASK queries from
``config/shapes/post-load-smoke.rq`` run against the SPARQL endpoint;
any failure rolls back by ``DELETE``-ing the bffi-works graph and
returns a failed :class:`LoadResult`. The provenance graph is *not*
rolled back — its content is independent and getting it loaded is
useful even when the bffi-works smoke fails.

The CLI (``bffi-pipeline load``) drives this end-to-end. The same
module exposes :func:`lookup_helmet_id` powering
``bffi-pipeline lookup-helmet``.

All HTTP work goes through an injectable ``httpx.Client`` so unit
tests use ``httpx.MockTransport`` to assert on request shape and
feed canned responses; nothing in this module touches the network on
import.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
from jinja2 import Template

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.observability import emit_if_active
from bffi_pipeline.stages.probes import emit_health_probes, probe_fuseki

#: Repo paths to the canonical config files.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_SMOKE_PATH: Final[Path] = _REPO_ROOT / "config" / "shapes" / "post-load-smoke.rq"
DEFAULT_HELMET_LOOKUP_PATH: Final[Path] = _REPO_ROOT / "sparql" / "queries" / "helmet_lookup.rq"
DEFAULT_ADMIN_VOCAB_PATH: Final[Path] = _REPO_ROOT / "config" / "bffi-admin-vocabulary.ttl"

#: Default named graph local names; combined with ``settings.graph_base``
#: to produce the full URIs.
GRAPH_LOCAL_BFFI_WORKS: Final[str] = "bffi-works"
GRAPH_LOCAL_PROVENANCE: Final[str] = "provenance"


# --- Schemas --------------------------------------------------------------


@dataclass(frozen=True)
class SmokeQuery:
    """One ASK query parsed from ``post-load-smoke.rq``."""

    name: str
    query: str


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of running one ``SmokeQuery`` against Fuseki."""

    name: str
    passed: bool
    error: str | None = None


@dataclass
class LoadResult:
    """End-of-run summary for :func:`run`."""

    fuseki_url: str
    bffi_works_graph: str
    provenance_graph: str
    bffi_works_uploaded: list[str] = field(default_factory=list)
    provenance_uploaded: list[str] = field(default_factory=list)
    smoke_results: list[SmokeResult] = field(default_factory=list)
    rolled_back: bool = False
    success: bool = False

    def render(self) -> str:
        """Format this result as paste-ready text for the load CLI."""
        lines = [
            "M10 load complete" if self.success else "M10 load FAILED",
            f"  fuseki:                {self.fuseki_url}",
            f"  bffi-works graph:      {self.bffi_works_graph}",
            f"    uploaded:            {len(self.bffi_works_uploaded)} file(s)",
        ]
        for path in self.bffi_works_uploaded:
            lines.append(f"      - {path}")
        lines.append(f"  provenance graph:      {self.provenance_graph}")
        lines.append(f"    uploaded:            {len(self.provenance_uploaded)} file(s)")
        for path in self.provenance_uploaded:
            lines.append(f"      - {path}")
        if self.smoke_results:
            lines.append("  Boundary-5 smoke results:")
            for r in self.smoke_results:
                mark = "PASS" if r.passed else "FAIL"
                detail = f"   ({r.error})" if r.error else ""
                lines.append(f"    {mark}  {r.name}{detail}")
        if self.rolled_back:
            lines.append(f"  ROLLED BACK the bffi-works graph: {self.bffi_works_graph}")
        return "\n".join(lines)


# --- HTTP primitives ------------------------------------------------------


def upload_graph(
    client: httpx.Client,
    *,
    fuseki_url: str,
    graph_uri: str,
    ttl_paths: list[Path],
) -> None:
    """Replace ``graph_uri`` with the union of ``ttl_paths``.

    First file is uploaded with ``PUT`` (which clears the graph); each
    subsequent file is appended via ``POST``. Both use SPARQL Graph
    Store Protocol with ``Content-Type: text/turtle``.
    """
    if not ttl_paths:
        return
    first, *rest = ttl_paths
    response = client.put(
        f"{fuseki_url.rstrip('/')}/data",
        params={"graph": graph_uri},
        content=first.read_bytes(),
        headers={"Content-Type": "text/turtle"},
    )
    response.raise_for_status()
    for path in rest:
        response = client.post(
            f"{fuseki_url.rstrip('/')}/data",
            params={"graph": graph_uri},
            content=path.read_bytes(),
            headers={"Content-Type": "text/turtle"},
        )
        response.raise_for_status()


def delete_graph(
    client: httpx.Client,
    *,
    fuseki_url: str,
    graph_uri: str,
) -> bool:
    """``DELETE`` ``graph_uri`` via GSP. Returns True on 200/204, False on error.

    Used during rollback, so a follow-up failure here is logged but
    must not crash the load run — the operator already has a failed
    smoke test to investigate.
    """
    try:
        response = client.delete(
            f"{fuseki_url.rstrip('/')}/data",
            params={"graph": graph_uri},
        )
        return response.is_success
    except httpx.HTTPError:
        return False


def run_ask(
    client: httpx.Client,
    *,
    fuseki_url: str,
    query: str,
    default_graph_uri: str | None = None,
) -> bool:
    """Run a SPARQL ASK query; return its boolean result.

    ``default_graph_uri`` is passed as the SPARQL Protocol
    ``default-graph-uri`` parameter so Fuseki uses the named graph as
    the query's default — needed because the Boundary-5 ASKs (and the
    spec § 10 examples) don't include explicit ``GRAPH`` clauses.
    """
    data = {"query": query}
    if default_graph_uri is not None:
        data["default-graph-uri"] = default_graph_uri
    response = client.post(
        f"{fuseki_url.rstrip('/')}/sparql",
        data=data,
        headers={"Accept": "application/sparql-results+json"},
    )
    response.raise_for_status()
    payload = response.json()
    return bool(payload.get("boolean", False))


def run_select(
    client: httpx.Client,
    *,
    fuseki_url: str,
    query: str,
    default_graph_uri: str | None = None,
) -> list[dict[str, Any]]:
    """Run a SPARQL SELECT and return the ``results.bindings`` array."""
    data = {"query": query}
    if default_graph_uri is not None:
        data["default-graph-uri"] = default_graph_uri
    response = client.post(
        f"{fuseki_url.rstrip('/')}/sparql",
        data=data,
        headers={"Accept": "application/sparql-results+json"},
    )
    response.raise_for_status()
    payload = response.json()
    bindings = payload.get("results", {}).get("bindings", [])
    return list(bindings)


# --- Smoke-query parser ---------------------------------------------------


_SMOKE_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^# === (.+?) ===\s*$", re.MULTILINE)


def load_smoke_queries(path: Path | None = None) -> list[SmokeQuery]:
    """Read ``config/shapes/post-load-smoke.rq``, split on ``# === <name> ===``."""
    target = path or DEFAULT_SMOKE_PATH
    if not target.is_file():
        raise FileNotFoundError(f"Smoke-query file not found at {target!s}.")
    raw = target.read_text(encoding="utf-8")
    sections = _SMOKE_HEADER_RE.split(raw)
    # sections[0] is the file preamble; following pairs are (name, body).
    queries: list[SmokeQuery] = []
    for i in range(1, len(sections), 2):
        name = sections[i].strip()
        body = sections[i + 1].strip()
        if body:
            queries.append(SmokeQuery(name=name, query=body))
    return queries


# --- Helmet lookup --------------------------------------------------------


def _quote_sparql_literal(value: str) -> str:
    """Wrap ``value`` as a properly-escaped SPARQL string literal."""
    # JSON's string escaping is conservative enough to be valid SPARQL.
    return json.dumps(value, ensure_ascii=False)


def lookup_helmet_id(
    client: httpx.Client,
    helmet_id: str,
    *,
    fuseki_url: str | None = None,
    template_path: Path | None = None,
    default_graph_uri: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``helmet_lookup.rq`` against Fuseki and return the SPARQL bindings."""
    settings = get_settings()
    fuseki_url = fuseki_url or settings.fuseki_url
    template_path = template_path or DEFAULT_HELMET_LOOKUP_PATH
    if default_graph_uri is None:
        default_graph_uri = _bffi_works_graph_uri(settings.graph_base)
    template = Template(template_path.read_text(encoding="utf-8"), autoescape=False)
    query = template.render(helmet_id=_quote_sparql_literal(helmet_id))
    return run_select(
        client, fuseki_url=fuseki_url, query=query, default_graph_uri=default_graph_uri
    )


def render_helmet_lookup(rows: list[dict[str, Any]]) -> str:
    """Format ``run_select`` rows for human eyeballing on the CLI."""
    if not rows:
        return "(no canonical Work found for this Helmet bib ID)"
    lines = []
    seen_works: set[str] = set()
    for row in rows:
        work_uri = row.get("canonicalWork", {}).get("value")
        if work_uri and work_uri not in seen_works:
            seen_works.add(work_uri)
            label = row.get("canonicalLabel", {}).get("value", "")
            label_part = f" — {label!r}" if label else ""
            lines.append(f"canonical Work: {work_uri}{label_part}")
        expr = row.get("expression", {}).get("value")
        if expr:
            elabel = row.get("expressionLabel", {}).get("value", "")
            elabel_part = f" — {elabel!r}" if elabel else ""
            lines.append(f"  expression:   {expr}{elabel_part}")
    return "\n".join(lines)


# --- Orchestrator ---------------------------------------------------------


def _bffi_works_graph_uri(graph_base: str) -> str:
    return f"{graph_base}{GRAPH_LOCAL_BFFI_WORKS}"


def _provenance_graph_uri(graph_base: str) -> str:
    return f"{graph_base}{GRAPH_LOCAL_PROVENANCE}"


def run(
    *,
    skosified_path: Path | None = None,
    admin_vocab_path: Path | None = None,
    provenance_path: Path | None = None,
    smoke_path: Path | None = None,
    fuseki_url: str | None = None,
    bffi_works_graph: str | None = None,
    provenance_graph: str | None = None,
    client: httpx.Client | None = None,
) -> LoadResult:
    """Upload to Fuseki, run smoke ASKs, roll back on failure.

    Returns a :class:`LoadResult` with the per-query smoke outcomes
    and a ``success`` flag. The caller (CLI) prints
    ``result.render()``; an exit-non-zero policy belongs there.
    """
    settings = get_settings()
    skosified_path = skosified_path or (settings.data_dir / "canonical-skosified.ttl")
    admin_vocab_path = admin_vocab_path or DEFAULT_ADMIN_VOCAB_PATH
    provenance_path = provenance_path or (settings.data_dir / "provenance.ttl")
    fuseki_url = fuseki_url or settings.fuseki_url
    bffi_works_graph = bffi_works_graph or _bffi_works_graph_uri(settings.graph_base)
    provenance_graph = provenance_graph or _provenance_graph_uri(settings.graph_base)

    own_client = client is None
    http_client: httpx.Client = client if client is not None else httpx.Client(timeout=30.0)

    result = LoadResult(
        fuseki_url=fuseki_url,
        bffi_works_graph=bffi_works_graph,
        provenance_graph=provenance_graph,
    )
    # Total for the dashboard's row-2 ``${load_total}`` tile — sourced
    # from M8's ``canonical-map.jsonl`` (one row per canonical Work);
    # the Load stage uploads them as a single SPARQL graph but the
    # tile renders the canonical-Work count, not the per-graph count,
    # so it composes with the M2 / M3 / M5 / M6 / M8 / M9 record counts.
    canonical_map = settings.data_dir / "canonical-map.jsonl"
    total = sum(1 for _ in canonical_map.open(encoding="utf-8")) if canonical_map.is_file() else 0
    emit_if_active(
        stage="load",
        event="start",
        counters={"total": total},
        extra={"fuseki_url": fuseki_url},
    )
    # P-11 Phase C: probe Fuseki at entry — the load stage's whole job
    # is talking to Fuseki, so an unreachable Fuseki should surface on
    # the dashboard the moment the stage starts.
    emit_health_probes("load", {"fuseki": probe_fuseki(fuseki_url)})

    try:
        _run_pipeline(
            http_client,
            result=result,
            skosified_path=skosified_path,
            admin_vocab_path=admin_vocab_path,
            provenance_path=provenance_path,
            smoke_path=smoke_path,
            fuseki_url=fuseki_url,
            bffi_works_graph=bffi_works_graph,
            provenance_graph=provenance_graph,
        )
    finally:
        if own_client:
            http_client.close()

    emit_if_active(
        stage="load",
        event="end",
        counters={
            "bffi_works_uploaded": len(result.bffi_works_uploaded),
            "provenance_uploaded": len(result.provenance_uploaded),
            "smoke_total": len(result.smoke_results),
        },
        extra={"success": result.success, "rolled_back": result.rolled_back},
    )
    return result


def _run_pipeline(
    http_client: httpx.Client,
    *,
    result: LoadResult,
    skosified_path: Path,
    admin_vocab_path: Path,
    provenance_path: Path,
    smoke_path: Path | None,
    fuseki_url: str,
    bffi_works_graph: str,
    provenance_graph: str,
) -> None:
    """Inner orchestrator with the typing narrowed to a real ``httpx.Client``."""
    bffi_works_files: list[Path] = []
    if skosified_path.is_file():
        bffi_works_files.append(skosified_path)
    if admin_vocab_path.is_file():
        bffi_works_files.append(admin_vocab_path)
    if not bffi_works_files:
        raise FileNotFoundError(
            f"Neither {skosified_path!s} nor {admin_vocab_path!s} exists. "
            "Run `bffi-pipeline skosify` first."
        )
    upload_graph(
        http_client,
        fuseki_url=fuseki_url,
        graph_uri=bffi_works_graph,
        ttl_paths=bffi_works_files,
    )
    result.bffi_works_uploaded = [str(p) for p in bffi_works_files]

    if provenance_path.is_file():
        upload_graph(
            http_client,
            fuseki_url=fuseki_url,
            graph_uri=provenance_graph,
            ttl_paths=[provenance_path],
        )
        result.provenance_uploaded = [str(provenance_path)]

    smoke_queries = load_smoke_queries(smoke_path)
    for sq in smoke_queries:
        try:
            ok = run_ask(
                http_client,
                fuseki_url=fuseki_url,
                query=sq.query,
                default_graph_uri=bffi_works_graph,
            )
            result.smoke_results.append(SmokeResult(name=sq.name, passed=ok))
        except httpx.HTTPError as exc:
            result.smoke_results.append(SmokeResult(name=sq.name, passed=False, error=str(exc)))

    if not all(r.passed for r in result.smoke_results):
        result.rolled_back = delete_graph(
            http_client, fuseki_url=fuseki_url, graph_uri=bffi_works_graph
        )
        result.success = False
    else:
        result.success = True


__all__ = [
    "DEFAULT_ADMIN_VOCAB_PATH",
    "DEFAULT_HELMET_LOOKUP_PATH",
    "DEFAULT_SMOKE_PATH",
    "GRAPH_LOCAL_BFFI_WORKS",
    "GRAPH_LOCAL_PROVENANCE",
    "LoadResult",
    "SmokeQuery",
    "SmokeResult",
    "delete_graph",
    "load_smoke_queries",
    "lookup_helmet_id",
    "render_helmet_lookup",
    "run",
    "run_ask",
    "run_select",
    "upload_graph",
]
