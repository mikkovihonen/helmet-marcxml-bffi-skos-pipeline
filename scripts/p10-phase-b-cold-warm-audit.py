#!/usr/bin/env python3
"""P-10 Phase B cold/warm-cache divergence audit.

Reads two reconciled canonical Turtle files (cold + warm) and reports
where they differ at the triple level. Used to debug the unexpected
lexical→local outcome shift observed in the 2026-05-13 Phase B + E
bench snapshot.

Usage:
    scripts/p10-phase-b-cold-warm-audit.py \\
        /tmp/canonical-phase-b-cold.ttl /tmp/canonical-phase-b-warm.ttl

Prints:
    1. Triple-set summary: only-in-cold, only-in-warm, common.
    2. Per-predicate breakdown of additions / removals.
    3. Sample triples per predicate for manual inspection.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

# Drop these from the per-triple comparison — they're timestamp
# fields that always differ run-to-run.
NOISE_PREDICATES: set[URIRef] = {
    URIRef("http://urn.fi/URN:NBN:fi:schema:bffi:descriptionChangeDate"),
}


def _format_node(n: Node) -> str:
    if isinstance(n, URIRef):
        s = str(n)
        if len(s) > 70:
            return f"<…{s[-65:]}>"
        return f"<{s}>"
    if isinstance(n, Literal):
        v = str(n)
        if len(v) > 70:
            v = v[:65] + "…"
        return f'"{v}"'
    if isinstance(n, BNode):
        return f"_:b{str(n)[:8]}"
    return str(n)


def _format_predicate(p: URIRef) -> str:
    s = str(p)
    if "schema:bffi" in s:
        return "bffi:" + s.rsplit(":", 1)[-1]
    if "schema:bffi-prov" in s:
        return "bffi-prov:" + s.rsplit("#", 1)[-1]
    return s


def _normalise_triple(t: tuple[Node, Node, Node]) -> tuple[Node, Node, Node] | None:
    s, p, o = t
    if isinstance(p, URIRef) and p in NOISE_PREDICATES:
        return None
    # Skip triples involving blank nodes — they have non-deterministic
    # identifiers per parse and would otherwise show up as 100 % diff
    # noise across the two files. Per-blank-node anonymous content
    # (Contributions, AdminMetadata blocks) is byte-stable in terms of
    # *value* even if the BNode label changes; the audit's signal is
    # the named-URI triples.
    if isinstance(s, BNode) or isinstance(o, BNode):
        return None
    return (s, p, o)


def _triples(graph: Graph) -> set[tuple[Node, Node, Node]]:
    out: set[tuple[Node, Node, Node]] = set()
    for s, p, o in graph:
        norm = _normalise_triple((s, p, o))
        if norm is not None:
            out.add(norm)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cold", type=Path)
    parser.add_argument("warm", type=Path)
    parser.add_argument(
        "--sample",
        type=int,
        default=8,
        help="Triples to print per per-predicate diff bucket (default: 8).",
    )
    args = parser.parse_args(argv)

    for p in (args.cold, args.warm):
        if not p.is_file():
            sys.stderr.write(f"ERROR: file not found: {p}\n")
            return 1

    print(f"Parsing cold: {args.cold}", file=sys.stderr)
    cold = Graph()
    cold.parse(str(args.cold), format="turtle")
    print(f"Parsing warm: {args.warm}", file=sys.stderr)
    warm = Graph()
    warm.parse(str(args.warm), format="turtle")

    cold_t = _triples(cold)
    warm_t = _triples(warm)

    only_cold = cold_t - warm_t
    only_warm = warm_t - cold_t
    common = cold_t & warm_t

    print("=" * 70)
    print("Triple-set diff (excluding BNode-anchored triples + noise predicates)")
    print("=" * 70)
    print(f"  Triples in cold:           {len(cold_t):>8,}")
    print(f"  Triples in warm:           {len(warm_t):>8,}")
    print(f"  Common (both):             {len(common):>8,}")
    print(f"  Only in cold:              {len(only_cold):>8,}")
    print(f"  Only in warm:              {len(only_warm):>8,}")

    by_pred_cold: Counter[URIRef] = Counter()
    by_pred_warm: Counter[URIRef] = Counter()
    for _s, p, _o in only_cold:
        if isinstance(p, URIRef):
            by_pred_cold[p] += 1
    for _s, p, _o in only_warm:
        if isinstance(p, URIRef):
            by_pred_warm[p] += 1

    all_preds = sorted(set(by_pred_cold) | set(by_pred_warm), key=lambda p: (-(by_pred_cold[p] + by_pred_warm[p]), str(p)))
    print()
    print("Per-predicate breakdown:")
    print("  " + "─" * 68)
    print(f"  {'predicate':<40} {'only cold':>10} {'only warm':>10}")
    print("  " + "─" * 68)
    for p in all_preds:
        print(f"  {_format_predicate(p):<40} {by_pred_cold[p]:>10,} {by_pred_warm[p]:>10,}")

    # Show a handful of sample triples per predicate so the operator can
    # eyeball whether the diff is binding-change, timestamp-noise, or
    # something else.
    print()
    print("Sample diverging triples (per predicate):")
    print()
    samples_per: dict[URIRef, dict[str, list[tuple[Node, Node, Node]]]] = {}
    for triple in only_cold:
        _s, p, _o = triple
        if isinstance(p, URIRef):
            samples_per.setdefault(p, {"cold": [], "warm": []})["cold"].append(triple)
    for triple in only_warm:
        _s, p, _o = triple
        if isinstance(p, URIRef):
            samples_per.setdefault(p, {"cold": [], "warm": []})["warm"].append(triple)

    for p in all_preds[:5]:  # Top-5 most-diverging predicates only.
        buckets = samples_per[p]
        print(f"  {_format_predicate(p)}:")
        for label in ("cold", "warm"):
            for s, _p, o in buckets[label][: args.sample]:
                marker = "-" if label == "cold" else "+"
                print(f"    {marker} {_format_node(s)} → {_format_node(o)}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
