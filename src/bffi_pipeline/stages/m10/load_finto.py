"""Stage 3b: download Finto vocab dumps and load them into Fuseki.

The Finto-hosted vocabularies (KANTO, YSO, KAUNO, MUSO, SLM) are the
authority sources M9 reconciles against. Surfacing the URIs as
labelled, clickable links in the bffi-works Skosmos UI requires the
vocab data to live in the same Fuseki Skosmos talks to. This stage
fetches the canonical Turtle dumps from ``api.finto.fi``, caches them
under ``BFFI_FINTO_DUMP_DIR/`` (default ``<repo>/finto-dumps/``), and PUTs each into its
canonical concept-scheme named graph in Fuseki via the SPARQL Graph
Store Protocol — same plumbing the M10 ``upload_graph`` helper uses.

Idempotent across runs: a local dump younger than ``--max-age-days``
is reused without re-downloading; ``--force`` overrides. Skosmos's
per-vocab entries in ``config/skosmos-config.ttl`` point at the same
graph URIs this stage writes to, so labels light up immediately on
the next page load. Subsequent ``make refresh-finto`` invocations
re-pull fresh dumps and replace the named graphs in place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote

import httpx
from rdflib import Graph

from bffi_pipeline.config import get_settings
from bffi_pipeline.stages.m10.load import upload_graph

DEFAULT_USER_AGENT: Final[str] = (
    "bffi-pipeline/0.1 (+https://github.com/mikkovihonen/helmet-marcxml-bffi-skos-pipeline)"
)


@dataclass(frozen=True)
class FintoVocab:
    """A Finto-hosted vocabulary we surface in our Skosmos UI.

    ``graph_uri`` is the URI used both as the named-graph identifier
    in our Fuseki and as the ``void:uriSpace`` in
    ``config/skosmos-config.ttl``. The two are deliberately equal — the
    URI namespace IS the concept-scheme URI for every Finto vocab we
    consume.
    """

    vocab_id: str
    dump_url: str
    graph_uri: str
    languages: tuple[str, ...]


#: Canonical vocab list. Dump URLs verified via redirect-following on
#: ``https://api.finto.fi/rest/v1/<vocab>/data?format=text/turtle``;
#: URI namespaces verified against each dump's preamble. KANTO is
#: identified as ``finaf`` in Finto's API per spec § 9 (the ``finaf``
#: vocab serves the Finnish Authority File data displayed under the
#: KANTO brand).
FINTO_VOCABS: Final[tuple[FintoVocab, ...]] = (
    FintoVocab(
        vocab_id="yso",
        dump_url="https://api.finto.fi/download/yso/yso-skos.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en", "se"),
    ),
    # YSO-Paikat (places) and YSO-Aika (time periods) share the YSO
    # concept namespace — ``http://www.yso.fi/onto/yso/p104995`` is
    # "Lontoo" in places, ``.../p6201062019`` is "2010-luku" in time,
    # ``.../p12279`` is "äidit" in YSO general topics. We load both
    # auxiliary dumps into the SAME Fuseki named graph as YSO so the
    # existing Skosmos ``:yso`` vocab entry (uriSpace + sparqlGraph
    # both equal to the YSO URI namespace) renders place + temporal
    # URIs as labelled clickable concepts without separate Skosmos
    # vocabs, and so M9 tier-0 finds their prefLabels via the same
    # SPARQL query as topic prefLabels. Cataloguer MARC ``$2 yso/fin``
    # tagging is the same across all three sub-vocabularies.
    FintoVocab(
        vocab_id="yso-paikat",
        dump_url="https://api.finto.fi/download/yso-paikat/yso-paikat-skos.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv", "en"),
    ),
    FintoVocab(
        vocab_id="yso-aika",
        dump_url="https://api.finto.fi/download/yso-aika/yso-aika-skos.ttl",
        graph_uri="http://www.yso.fi/onto/yso/",
        languages=("fi", "sv"),
    ),
    FintoVocab(
        vocab_id="finaf",
        dump_url="https://api.finto.fi/download/finaf/finaf-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:finaf:",
        languages=("fi",),
    ),
    FintoVocab(
        vocab_id="kauno",
        dump_url="https://api.finto.fi/download/kauno/kauno-skos.ttl",
        graph_uri="http://www.yso.fi/onto/kauno/",
        languages=("fi", "sv", "en"),
    ),
    # KAUNOKKI/BELLA — the legacy KAUNO thesaurus, with the Swedish
    # parallel labels under the Bella sub-vocab. Cataloguers tag
    # ``$2 kaunokki`` (Finnish form) or ``$2 bella`` (Swedish form) on
    # MARC 6XX for fiction material — the 200-record corpus smoke
    # surfaced ~10 Bella-tagged Swedish-language records that fell into
    # tier-0 ``no-candidate`` because the underlying labels live in
    # Kaunokki's graph (separate URI namespace from KAUNO), not loaded
    # by the original M11 3b pass. Separate Fuseki named graph because
    # the URI namespace (``http://urn.fi/URN:NBN:fi:au:kaunokki:``)
    # doesn't overlap KAUNO's (``http://www.yso.fi/onto/kauno/``).
    FintoVocab(
        vocab_id="kaunokki",
        dump_url="https://api.finto.fi/download/kaunokki/kaunokki-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:kaunokki:",
        languages=("fi", "sv"),
    ),
    FintoVocab(
        vocab_id="muso",
        dump_url="https://api.finto.fi/download/muso/muso-skos.ttl",
        graph_uri="http://www.yso.fi/onto/muso/",
        languages=("fi", "sv"),
    ),
    FintoVocab(
        vocab_id="slm",
        dump_url="https://api.finto.fi/download/slm/slm-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:slm:",
        languages=("fi", "sv"),
    ),
    # Allärs — the Swedish General Thesaurus, Allmän tesaurus på
    # svenska. Cataloguers tag ``$2 allars`` on Swedish-language MARC
    # 6XX subjects (parallel to YSA/YSO on the Finnish side). The
    # 200-record corpus smoke surfaced 10 Allars-tagged entries; at
    # 800k scale Swedish-language records are a significant minority
    # of Helmet. Allars lives in its own URI namespace under
    # ``http://www.yso.fi/onto/allars/`` so it loads to a separate
    # Fuseki named graph; tier-0 ``subject`` routing adds Allars
    # between YSO (Finnish-first) and LCSH (English-last).
    FintoVocab(
        vocab_id="allars",
        dump_url="https://api.finto.fi/download/allars/allars-skos.ttl",
        graph_uri="http://www.yso.fi/onto/allars/",
        languages=("sv",),
    ),
    # YSA — the legacy Finnish General Thesaurus (Yleinen suomalainen
    # asiasanasto). Stopped being updated in 2019 when YSO became the
    # successor; the 2014-2018 merge brought YSA prefLabels into YSO
    # but NOT bare-form altLabels for disambiguated concepts (``lapset``
    # in YSA was split into ``lapset (ikäryhmät)`` + ``lapset
    # (perheenjäsenet)`` in YSO with neither bare form surviving as
    # altLabel). Older Helmet records carrying ``$2 ysa`` literals
    # fall into the ~4% gap surfaced by ``ysa_disambiguation_report``.
    # Loading the full YSA SKOS dump lets the new M9 legacy-mapping
    # tier follow ``skos:exactMatch`` / ``skos:closeMatch`` triples
    # from YSA concepts into YSO without a Finto API round-trip. ~15
    # MB. Own URI namespace at ``http://www.yso.fi/onto/ysa/`` so it
    # lives in its own Fuseki named graph.
    FintoVocab(
        vocab_id="ysa",
        dump_url="https://api.finto.fi/download/ysa/ysa-skos.ttl",
        graph_uri="http://www.yso.fi/onto/ysa/",
        languages=("fi", "sv"),
    ),
    # MUSA — the legacy Music Subject Headings (Musiikin asiasanasto),
    # which also contains the merged CILLA (visual-arts terms). Frozen
    # post-2019 like YSA. MUSA concepts bridge to YSO via a two-hop
    # path: MUSA concept → ``dct:isReplacedBy`` → YSA concept →
    # ``skos:exactMatch`` → YSO concept. The M9 legacy-mapping tier
    # follows the chain via a cross-graph SPARQL UNION. ~700 KB. Own
    # URI namespace at ``http://www.yso.fi/onto/musa/``.
    FintoVocab(
        vocab_id="musa",
        dump_url="https://api.finto.fi/download/musa/musa-skos.ttl",
        graph_uri="http://www.yso.fi/onto/musa/",
        languages=("fi",),
    ),
    # SEKO (Suomalainen esityskokoonpanosanasto) — Finnish Performance
    # Ensemble Vocabulary. Loaded pre-emptively while NLF retires HKLJ
    # and the broader music-cataloguing landscape consolidates. 1,243
    # concepts covering musical instruments + performance ensembles
    # ("3-rivinen harmonikka", "5-kielinen kantele", "vocal quartet").
    # Primary MARC field per the vocab spec is 382 (Medium of
    # Performance), but cataloguers also tag $2 seko on 650/655 for
    # music-instrument subjects. Finnish-only, ~606 KB.
    #
    # URI namespace uses LOWERCASE ``urn:nbn:fi:au:seko:`` (different
    # from MTS/SLM which use UPPERCASE ``URN:NBN``).
    #
    # Bridges to LoC's Performance Mediums (LCMPT) at
    # ``id.loc.gov/authorities/performanceMediums/`` — NOT to YSO, so
    # no SEKO→YSO redirect is added. If LCMPT is loaded in the future
    # a SEKO→LCMPT redirect could be added analogous to KAUNO→YSO.
    FintoVocab(
        vocab_id="seko",
        dump_url="https://api.finto.fi/download/seko/seko-skos.ttl",
        graph_uri="http://urn.fi/urn:nbn:fi:au:seko:",
        languages=("fi",),
    ),
    # MTS (Metatietosanasto) — Metadata Thesaurus. Loaded pre-emptively;
    # not present in Helmet's 500-sample corpus but used by other Finnish
    # libraries (archives, repositories) for administrative metadata
    # like RDA content / media / carrier types, file formats, access
    # conditions. ~4,434 concepts, 2.7 MB. Spans two domains:
    #   - Topical: 1,458 ``skos:closeMatch yso:`` triples for concepts
    #     that overlap with YSO (could canonicalise to YSO in a future
    #     redirect, but not done here — MTS's RDA-side concepts
    #     shouldn't get rebound to YSO).
    #   - RDA-admin: 1,521 ``skos:exactMatch`` triples to RDA
    #     vocabularies (rdaw, rdae, rdam, rdaa, rdai, rdact, etc.) for
    #     content / media / carrier descriptors.
    # The download URL uses the full vocab name in the path
    # (``metatietosanasto`` not ``mts``), unlike most Finto vocabs;
    # Finto API's short alias is ``mts`` but the on-disk dump is
    # ``metatietosanasto-skos.ttl``.
    FintoVocab(
        vocab_id="mts",
        dump_url="https://api.finto.fi/download/metatietosanasto/metatietosanasto-skos.ttl",
        graph_uri="http://urn.fi/URN:NBN:fi:au:mts:",
        languages=("fi", "sv", "en"),
    ),
    # MARC Code List for Relators — not Finto-hosted but loaded the
    # same way so Skosmos renders the bf:role URIs the M3
    # contributor-extraction cascade emits (e.g. relators/trl) as
    # labelled, clickable links. Served as RDF/XML; the download path
    # converts to Turtle on the fly. ~130 KB; English-only.
    FintoVocab(
        vocab_id="relators",
        dump_url="https://id.loc.gov/vocabulary/relators.rdf",
        graph_uri="http://id.loc.gov/vocabulary/relators/",
        languages=("en",),
    ),
    # RDA Media Types — LoC's vocabulary at
    # ``id.loc.gov/vocabulary/mediaTypes`` covers the RDA "media type"
    # axis ("computer", "audio", "video", "unmediated", etc.). MARC 337
    # ``$b`` carries the two-letter code (``c`` = computer, ``s`` =
    # audio) that resolves to a URI in this namespace. Loaded as a
    # named-graph in Fuseki so M9 can reconcile ``bffi:media`` targets
    # against it (P-45 commit 4) and so Skosmos renders Manifestation
    # media-type URIs as labelled clickable concepts on the new
    # ``:bffiManifestations`` vocab page. Same RDF/XML wire format as
    # relators; ~10 KB. English-only.
    FintoVocab(
        vocab_id="rda-media",
        dump_url="https://id.loc.gov/vocabulary/mediaTypes.rdf",
        graph_uri="http://id.loc.gov/vocabulary/mediaTypes/",
        languages=("en",),
    ),
    # RDA Carrier Types — LoC's vocabulary at
    # ``id.loc.gov/vocabulary/carriers`` covers the RDA "carrier type"
    # axis ("volume", "online resource", "audio disc", "computer disc",
    # etc.). MARC 338 ``$b`` carries the carrier code (``nc`` = volume,
    # ``cr`` = online resource) that resolves to a URI in this
    # namespace. Same loading + Skosmos wiring rationale as RDA Media
    # above. Same RDF/XML wire format as relators; ~25 KB.
    # English-only.
    FintoVocab(
        vocab_id="rda-carrier",
        dump_url="https://id.loc.gov/vocabulary/carriers.rdf",
        graph_uri="http://id.loc.gov/vocabulary/carriers/",
        languages=("en",),
    ),
    # LC Genre/Form Terms — Helmet cataloguers cite English genre/form
    # URIs (e.g. ``http://id.loc.gov/authorities/genreForms/gf2015026020``
    # for "Novels", ``.../gf2014026542`` for "Short stories") on MARC
    # 655 fields without further translation. Loading the LCGFT dump
    # both makes ``graph_uri_for_uri`` recognise those URIs (so M9
    # walkers correctly skip them as already-resolved) and gives
    # Skosmos a graph to render English labels from. Served gzipped;
    # the download path detects ``.gz`` and decompresses on the fly.
    # ~330 KB compressed; English-only.
    FintoVocab(
        vocab_id="lcgft",
        dump_url="https://id.loc.gov/download/authorities/genreForms.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/genreForms/",
        languages=("en",),
    ),
    # LC Subject Headings — the general English subject thesaurus.
    # Cataloguers occasionally use ``$2 lcsh`` for topical subjects on
    # records copied or harmonised from English-language sources, with
    # the literal heading carried as ``rdfs:label``. Tier-0 routes
    # ``subject``-kind requests through both YSO and LCSH so those
    # English literals bind deterministically without a Finto API call.
    # Served gzipped; ~39 MB compressed (~250-500 MB uncompressed
    # depending on rdflib's serialisation density), the largest dump
    # after KANTO. English-only.
    FintoVocab(
        vocab_id="lcsh",
        dump_url="https://id.loc.gov/download/authorities/subjects.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/subjects/",
        languages=("en",),
    ),
    # LC Medium of Performance Thesaurus (LCMPT) — small (~99 KB
    # gzipped) thesaurus covering musical instruments + performance
    # mediums in English. Cataloguers rarely tag ``$2 lcmpt`` on Helmet
    # records directly, but LCMPT is the bridge target for SEKO (Finnish
    # Performance Ensemble Vocabulary): SEKO has 590 ``skos:exactMatch``
    # / ``skos:closeMatch`` triples pointing into the LCMPT URI
    # namespace. Loading LCMPT lets M9's post-tier-0 SEKO→LCMPT redirect
    # canonicalise Finnish music-ensemble literals to the international
    # LCMPT URIs where one exists. English-only labels.
    FintoVocab(
        vocab_id="lcmpt",
        dump_url="https://id.loc.gov/download/authorities/performanceMediums.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/performanceMediums/",
        languages=("en",),
    ),
    # LC Children's Subject Headings — a subset of LCSH tuned for
    # juvenile-collection cataloguing. Cataloguers tag ``$2 lcsh``
    # for children's-collection records on translated English imports
    # ("Jukka Hukka (fiktiivinen hahmo)" → matching English form).
    # Same gzipped Turtle wire format as LCSH/LCGFT, ~1.8 MB
    # compressed. Separate URI namespace
    # (``http://id.loc.gov/authorities/childrensSubjects/``) so it
    # loads to its own Fuseki named graph; tier-0 ``subject`` routing
    # adds it after LCSH.
    FintoVocab(
        vocab_id="childrensSubjects",
        dump_url="https://id.loc.gov/download/authorities/childrensSubjects.skosrdf.ttl.gz",
        graph_uri="http://id.loc.gov/authorities/childrensSubjects/",
        languages=("en",),
    ),
)


@dataclass
class VocabResult:
    """Per-vocab outcome row reported in the CLI summary."""

    vocab_id: str
    dump_path: Path
    graph_uri: str
    bytes_downloaded: int  # 0 when the cached dump was reused
    cache_hit: bool
    triples_uploaded: bool


@dataclass
class FintoLoadSummary:
    """Aggregate result reported by the ``load-finto`` CLI."""

    fuseki_url: str
    results: list[VocabResult] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Finto vocab load summary (Fuseki: {self.fuseki_url})"]
        for r in self.results:
            mb = r.bytes_downloaded // (1024 * 1024)
            cache = "cached" if r.cache_hit else f"{mb} MB downloaded"
            uploaded = "uploaded" if r.triples_uploaded else "skipped"
            lines.append(f"  {r.vocab_id:6}  {cache:>20}  {uploaded:>10}  → {r.graph_uri}")
        return "\n".join(lines)


def _is_dump_fresh(path: Path, *, max_age_days: int, now: datetime) -> bool:
    """Return True iff ``path`` exists and its mtime is younger than
    ``max_age_days`` relative to ``now``."""
    if not path.exists():
        return False
    age_seconds = now.timestamp() - path.stat().st_mtime
    return age_seconds < max_age_days * 24 * 3600


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


_RDFXML_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/rdf+xml", "text/xml", "application/xml"}
)


#: Finnish + Swedish prefLabels for LoC RDA vocab URIs. Sourced from
#: the National Library of Finland's Finnish RDA Toolkit terminology
#: (Kansalliskirjasto, RDA-FI). Only the codes that actually appear
#: in the 500-record Helmet smoke + the next-most-common surrounding
#: codes are listed — uncovered URIs fall back to the English label
#: with @fi / @sv tags (Skosmos still resolves them but renders the
#: English string). Extend this map when a new code surfaces.
#:
#: Swedish translations are best-effort; the authoritative Swedish
#: RDA Toolkit terminology is published by Kungliga biblioteket. If
#: a cataloguer corrects a Swedish term, update it here.
_RDA_LABEL_OVERRIDES: Final[dict[str, dict[str, str]]] = {
    # mediaTypes
    "http://id.loc.gov/vocabulary/mediaTypes/n": {"fi": "ilman välinettä", "sv": "utan medel"},
    "http://id.loc.gov/vocabulary/mediaTypes/s": {"fi": "audio", "sv": "audio"},
    "http://id.loc.gov/vocabulary/mediaTypes/v": {"fi": "video", "sv": "video"},
    "http://id.loc.gov/vocabulary/mediaTypes/c": {"fi": "tietokone", "sv": "dator"},
    "http://id.loc.gov/vocabulary/mediaTypes/h": {"fi": "mikromuoto", "sv": "mikroform"},
    "http://id.loc.gov/vocabulary/mediaTypes/g": {"fi": "projisoitava", "sv": "projicerad"},
    "http://id.loc.gov/vocabulary/mediaTypes/p": {"fi": "mikroskooppinen", "sv": "mikroskopisk"},
    "http://id.loc.gov/vocabulary/mediaTypes/e": {"fi": "stereografinen", "sv": "stereografisk"},
    "http://id.loc.gov/vocabulary/mediaTypes/x": {"fi": "muu", "sv": "annan"},
    "http://id.loc.gov/vocabulary/mediaTypes/z": {"fi": "määrittelemätön", "sv": "ospecificerad"},
    # carriers
    "http://id.loc.gov/vocabulary/carriers/nc": {"fi": "nidos", "sv": "volym"},
    "http://id.loc.gov/vocabulary/carriers/na": {"fi": "arkki", "sv": "blad"},
    "http://id.loc.gov/vocabulary/carriers/nr": {"fi": "rulla", "sv": "rulle"},
    "http://id.loc.gov/vocabulary/carriers/sd": {"fi": "äänilevy", "sv": "ljudskiva"},
    "http://id.loc.gov/vocabulary/carriers/ss": {"fi": "äänikasetti", "sv": "ljudkassett"},
    "http://id.loc.gov/vocabulary/carriers/vd": {"fi": "videolevy", "sv": "videoskiva"},
    "http://id.loc.gov/vocabulary/carriers/vf": {"fi": "videokasetti", "sv": "videokassett"},
    "http://id.loc.gov/vocabulary/carriers/vr": {"fi": "videokela", "sv": "videorulle"},
    "http://id.loc.gov/vocabulary/carriers/cd": {"fi": "tietokonelevy", "sv": "datorskiva"},
    "http://id.loc.gov/vocabulary/carriers/cr": {
        "fi": "verkkoaineisto",
        "sv": "online-resurs",
    },
    "http://id.loc.gov/vocabulary/carriers/ck": {"fi": "tietokonekortti", "sv": "datorkort"},
    "http://id.loc.gov/vocabulary/carriers/ce": {
        "fi": "tietokonelevykasetti",
        "sv": "datorskivkassett",
    },
    "http://id.loc.gov/vocabulary/carriers/ca": {
        "fi": "tietokonenauhakasetti",
        "sv": "datorbandkassett",
    },
    "http://id.loc.gov/vocabulary/carriers/ch": {
        "fi": "tietokonepiirikasetti",
        "sv": "datorchipkassett",
    },
    "http://id.loc.gov/vocabulary/carriers/cz": {"fi": "muu", "sv": "annan"},
}


def _lift_mads_to_skos(graph: Graph) -> None:
    """In-place: lift LoC MADS-shaped authority data to SKOS.

    LoC publishes the RDA vocabs (mediaTypes, carriers, similar
    issuance / frequency lists) using MADS rather than SKOS — concepts
    are typed ``mads:Authority`` and labelled via
    ``mads:authoritativeLabel``. Skosmos's renderer targets SKOS, so
    a MADS-only graph shows URIs as ``prefix:code`` instead of the
    label text. This helper dual-types every ``mads:Authority`` as
    ``skos:Concept`` and lifts ``mads:authoritativeLabel`` to
    ``skos:prefLabel`` tagged ``@en``.

    For URIs in :data:`_RDA_LABEL_OVERRIDES` the fi/sv prefLabels use
    the project's NLF-sourced Finnish / Swedish RDA terminology; for
    URIs without an override the English label is reused under @fi /
    @sv tags so Skosmos's cross-vocab lookup still resolves from
    Finnish / Swedish pages (rendering the English string when no
    local translation exists).

    No-op on graphs that don't contain ``mads:Authority``.
    """
    from rdflib import Literal, Namespace
    from rdflib.namespace import RDF, SKOS

    MADS = Namespace("http://www.loc.gov/mads/rdf/v1#")
    for s in list(graph.subjects(RDF.type, MADS.Authority)):
        graph.add((s, RDF.type, SKOS.Concept))
        overrides = _RDA_LABEL_OVERRIDES.get(str(s), {})
        for label in graph.objects(s, MADS.authoritativeLabel):
            text = str(label)
            graph.add((s, SKOS.prefLabel, Literal(text, lang="en")))
            graph.add((s, SKOS.prefLabel, Literal(overrides.get("fi", text), lang="fi")))
            graph.add((s, SKOS.prefLabel, Literal(overrides.get("sv", text), lang="sv")))


def _download_dump(
    client: httpx.Client,
    vocab: FintoVocab,
    target_path: Path,
) -> int:
    """GET ``vocab.dump_url`` and write atomically to ``target_path``.

    Returns the number of bytes written (after any decompression). The
    Finto dump endpoints return 302s to ``/download/<vocab>/<vocab>-skos.ttl``;
    we follow them so callers get the real Turtle. ``raise_for_status``
    surfaces HTTP errors loudly — these are deliberately not caught
    here, since a missing or rate-limited dump means the operator should
    retry rather than have the load proceed against stale data.

    Two non-Turtle wire formats are normalised to Turtle on the way in:

    - **RDF/XML** — LoC's ``id.loc.gov/vocabulary/relators.rdf`` ignores
      ``Accept: text/turtle`` and serves RDF/XML regardless. Detected
      via the response Content-Type and re-serialised through rdflib.
    - **Gzipped Turtle** — LoC's bulk authority dumps
      (``id.loc.gov/download/authorities/*.skosrdf.ttl.gz``) are only
      published gzipped; the server sends ``Content-Encoding: identity``
      because the gzip is part of the payload format. Detected via the
      ``.gz`` URL suffix and decompressed before saving.

    Decompression / conversion happens once at download time so
    :func:`upload_graph` always sees ``text/turtle`` regardless of what
    the upstream wire format was.
    """
    import gzip

    response = client.get(vocab.dump_url, headers={"Accept": "text/turtle"})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in _RDFXML_CONTENT_TYPES:
        graph = Graph()
        graph.parse(data=response.content, format="xml")
        # Lift LoC MADS authority data to SKOS so Skosmos's
        # cross-vocab label lookup can resolve URIs that link into
        # MADS-published vocabs (RDA mediaTypes / carriers / etc.).
        # No-op on non-MADS graphs.
        _lift_mads_to_skos(graph)
        payload = graph.serialize(format="turtle").encode("utf-8")
    elif vocab.dump_url.endswith(".gz"):
        payload = gzip.decompress(response.content)
    else:
        payload = response.content
    _atomic_write_bytes(target_path, payload)
    return len(payload)


def run(
    *,
    output_dir: Path | None = None,
    fuseki_url: str | None = None,
    max_age_days: int = 30,
    force: bool = False,
    vocabs: tuple[FintoVocab, ...] = FINTO_VOCABS,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> FintoLoadSummary:
    """Refresh the Finto-vocab named graphs in Fuseki.

    Per vocab: download the Turtle dump (unless a recent local copy
    exists and ``force`` is False), then PUT it into the corresponding
    named graph via Graph Store Protocol. The PUT replaces the graph
    so a re-run produces a clean graph rather than accumulating stale
    triples from old dumps.
    """
    settings = get_settings()
    fuseki = fuseki_url or settings.fuseki_url
    # ``output_dir`` (when passed) keeps the legacy ``<output_dir>/finto-dumps``
    # layout for tests + ad-hoc operator runs. Default resolves through
    # the new ``BFFI_FINTO_DUMP_DIR`` setting (the shared vocab cache,
    # decoupled from per-run ``data_dir``).
    dumps_dir = (output_dir / "finto-dumps") if output_dir is not None else settings.finto_dump_dir
    dumps_dir.mkdir(parents=True, exist_ok=True)
    summary = FintoLoadSummary(fuseki_url=fuseki)
    timestamp = now or datetime.now(UTC)

    owned_client = http_client is None
    if owned_client:
        # Connect / pool default to 60 s. Read 300 s for the long-poll
        # downloads (KANTO is ~183 MB, LCSH ~39 MB compressed). Write
        # 1800 s (30 min) because every PUT to Fuseki re-uploads the
        # full Turtle payload — at LCSH's ~465 MB decompressed size the
        # POST body alone can exceed the default 60 s write timeout.
        http_client = httpx.Client(
            timeout=httpx.Timeout(60.0, read=300.0, write=1800.0),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    assert http_client is not None  # narrowing for mypy

    try:
        # Download every dump first; group uploads by graph_uri so vocabs
        # that share a named graph (e.g. yso + yso-paikat both targeting
        # the YSO graph) get a single PUT (clears + loads the first dump)
        # followed by POSTs (appends each subsequent dump) per the
        # ``upload_graph`` multi-path protocol. Without this grouping the
        # second vocab's PUT would clobber the first.
        per_vocab_state: list[tuple[FintoVocab, Path, int, bool]] = []
        for vocab in vocabs:
            dump_path = dumps_dir / f"{vocab.vocab_id}-skos.ttl"
            cache_hit = not force and _is_dump_fresh(
                dump_path, max_age_days=max_age_days, now=timestamp
            )
            bytes_downloaded = 0 if cache_hit else _download_dump(http_client, vocab, dump_path)
            per_vocab_state.append((vocab, dump_path, bytes_downloaded, cache_hit))

        groups: dict[str, list[Path]] = {}
        for vocab, dump_path, _bytes, _cache in per_vocab_state:
            groups.setdefault(vocab.graph_uri, []).append(dump_path)
        for graph_uri, dump_paths in groups.items():
            upload_graph(
                http_client,
                fuseki_url=fuseki,
                graph_uri=graph_uri,
                ttl_paths=dump_paths,
            )

        for vocab, dump_path, bytes_downloaded, cache_hit in per_vocab_state:
            summary.results.append(
                VocabResult(
                    vocab_id=vocab.vocab_id,
                    dump_path=dump_path,
                    graph_uri=vocab.graph_uri,
                    bytes_downloaded=bytes_downloaded,
                    cache_hit=cache_hit,
                    triples_uploaded=True,
                )
            )
    finally:
        if owned_client:
            http_client.close()

    return summary


def graph_uri_for_uri(uri: str) -> str | None:
    """Return the Finto vocab graph URI a given resource URI belongs to,
    or ``None`` if it isn't from a known Finto namespace.

    Used by :mod:`bffi_pipeline.cli` and :mod:`bffi_pipeline.stages.m9.runner`
    to route URIs to the right vocab when constructing Skosmos links —
    keeps the namespace mapping in one place rather than scattered
    string-prefix checks across stages.
    """
    for vocab in FINTO_VOCABS:
        if uri.startswith(vocab.graph_uri):
            return vocab.graph_uri
    return None


# Re-export the URL-encoding helper used in tests when constructing
# expected GSP request URLs.
quote_graph_param = quote


__all__ = [
    "DEFAULT_USER_AGENT",
    "FINTO_VOCABS",
    "FintoLoadSummary",
    "FintoVocab",
    "VocabResult",
    "graph_uri_for_uri",
    "run",
]
