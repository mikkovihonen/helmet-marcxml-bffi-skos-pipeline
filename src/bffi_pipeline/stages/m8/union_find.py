"""Path-compressed union-find used by M8 to cluster judge decisions
into canonical Work groups.

Operates on arbitrary string node IDs. Lex-smallest root wins on
``union`` so two runs that see decisions in different orders produce
the same group representatives — the canonical URI is derived from
the representative's bib_id, so determinism here is load-bearing for
byte-stable canonical.ttl output.

P-38 Phase B: extracted from m8/runner.py to keep the runner focused
on the mint orchestration. No logic change — moves only.
"""

from __future__ import annotations


class _UnionFind:
    """Tiny path-compressed union-find. Nodes are arbitrary hashable values."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        if x not in self._parent:
            self._parent[x] = x

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            # Pick the lex-smaller root for determinism
            new_root, child = (rx, ry) if rx < ry else (ry, rx)
            self._parent[child] = new_root

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in list(self._parent):
            out.setdefault(self.find(x), []).append(x)
        for v in out.values():
            v.sort()
        return out
