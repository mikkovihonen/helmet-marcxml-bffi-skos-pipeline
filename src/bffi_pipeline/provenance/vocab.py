"""Namespaces and term URIs for the PROV-O + BFFI provenance graph.

The provenance graph is layered with the BFFI-native AdminMetadata view (see
spec § 8). M2 emits the first :data:`MarcConversion` Activities; later
stages extend the vocabulary with :data:`WorkMergeDecision` (M6) and
:data:`HumanReview` (M7+).

This module is intentionally pure constants — no I/O, no graph mutation —
so it stays cheap to import from any stage.
"""

from __future__ import annotations

from rdflib import Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

# --- Namespaces -----------------------------------------------------------

PROV = Namespace("http://www.w3.org/ns/prov#")
BFFI = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")
BFFI_PROV = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi-prov#")
BIB = Namespace("http://urn.fi/URN:NBN:fi:bib:")
BF = Namespace("http://id.loc.gov/ontologies/bibframe/")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

# --- Activity classes -----------------------------------------------------

MarcConversion: URIRef = BFFI_PROV.MarcConversion
WorkMergeDecision: URIRef = BFFI_PROV.WorkMergeDecision
HumanReview: URIRef = BFFI_PROV.HumanReview
Reconciliation: URIRef = BFFI_PROV.Reconciliation
#: P-41 Phase A — sibling of :data:`MarcConversion`. Emitted when M2's
#: salvage layer synthesises a field to make a record meet
#: :func:`bffi_pipeline.validation.marcxml.validate_minimum_content`.
#: Carries one ``prov:used`` link to the source MarcConversion Activity
#: for the same record, so the audit chain is traversable from a
#: synthesised triple back to its MARC input. Predicate set documented
#: in :mod:`bffi_pipeline.provenance.vocab` below
#: (``synthetic*`` predicates).
Synthesis: URIRef = BFFI_PROV.Synthesis

# --- bffi-prov predicates emitted by M2 -----------------------------------

helmetBibId: URIRef = BFFI_PROV.helmetBibId
converterVersion: URIRef = BFFI_PROV.converterVersion

# --- bffi-prov predicates emitted by M6 (WorkMergeDecision) ---------------

stage: URIRef = BFFI_PROV.stage
decision: URIRef = BFFI_PROV.decision
confidence: URIRef = BFFI_PROV.confidence
embeddingSimilarity: URIRef = BFFI_PROV.embeddingSimilarity
rationale: URIRef = BFFI_PROV.rationale
matchingField: URIRef = BFFI_PROV.matchingField
divergingField: URIRef = BFFI_PROV.divergingField
promptHash: URIRef = BFFI_PROV.promptHash
promptSource: URIRef = BFFI_PROV.promptSource
rawResponse: URIRef = BFFI_PROV.rawResponse
modelId: URIRef = BFFI_PROV.modelId
provider: URIRef = BFFI_PROV.provider
temperature: URIRef = BFFI_PROV.temperature
seed: URIRef = BFFI_PROV.seed
cacheHit: URIRef = BFFI_PROV.cacheHit

# --- bffi-prov predicates emitted by M7 (HumanReview) --------------------

reviewNote: URIRef = BFFI_PROV.reviewNote

# --- bffi-prov predicates emitted by M9 (Reconciliation) -----------------

chosenAuthorityUri: URIRef = BFFI_PROV.chosenAuthorityUri
candidateAuthorityUri: URIRef = BFFI_PROV.candidateAuthorityUri
sourceVocabulary: URIRef = BFFI_PROV.sourceVocabulary
lexicalSimilarity: URIRef = BFFI_PROV.lexicalSimilarity
inputLiteral: URIRef = BFFI_PROV.inputLiteral

# --- bffi-prov predicates emitted by M8 (canonical Work mint) ------------

#: Records which input slot of the canonical-Work mint key was used.
#: Two values today (P-34): primary-author-anchored (the standard
#: bffi:PrimaryContribution → bffi:agent path) and first-contributor-
#: anchored (P-34 sub-option 1 fallback, for anonymous-main-entry
#: records that lack a MARC 1XX but carry MARC 700 contributors).
mintAnchor: URIRef = BFFI_PROV.mintAnchor

MINT_ANCHOR_PRIMARY_AUTHOR: URIRef = BIB["auth/primary-author-anchored"]
MINT_ANCHOR_FIRST_CONTRIBUTOR: URIRef = BIB["auth/first-contributor-anchored"]
#: P-34 Phase B: truly-anonymous records (no primary creator, no
#: usable non-primary contribution either) mint a canonical Work
#: anchored on (title, content-type, language). See P-34 plan's
#: "Phase B" section for the MARC-input contract.
MINT_ANCHOR_ANONYMOUS_WORK: URIRef = BIB["auth/anonymous-work-anchored"]

# --- bffi-prov predicates emitted by M2 salvage (Synthesis) --------------
# P-41 Phase A. Every Synthesis Activity carries the four below.

#: The BFFI field synthesised, e.g. ``"bf:contribution/bf:agent"`` for
#: a creator salvaged by P-41 Phase B. Free-text; the value is the
#: graph path the consumer should look at, not a URI.
syntheticField: URIRef = BFFI_PROV.syntheticField
#: Human-readable method tag, e.g. ``"creator-from-245c (regex)"`` or
#: ``"anonymous-by-convention"``. Used in the per-run TSV's ``method``
#: column verbatim.
syntheticMethod: URIRef = BFFI_PROV.syntheticMethod
#: Phase B tier ID — ``"B1"`` (245$c parse), ``"B2"`` (publisher-as-
#: corporate-creator), ``"B3"`` (anonymous sentinel). Future plans
#: extending the salvage taxonomy reuse the same predicate with a
#: distinct tier string.
syntheticTier: URIRef = BFFI_PROV.syntheticTier
#: Confidence in the synthesised value, 0.0 to 1.0. Tier-specific
#: bands documented in ``docs/bibliographic-minimum.md``. B1 regex
#: emits 0.5-0.8; B1 LLM cascade caps at 0.7; B2 emits 0.3; B3 emits 0.1.
syntheticConfidence: URIRef = BFFI_PROV.syntheticConfidence
#: The literal or URI now in the synthesised resource — for B1/B2 the
#: agent name, for B3 the sentinel URI. Persisted on the Synthesis
#: Activity so the Phase C.3 retrospective CLI can rebuild the TSV's
#: ``synthesised_value`` column byte-identically without traversing
#: the BIBFRAME graph.
syntheticValue: URIRef = BFFI_PROV.syntheticValue
#: The MARC source field(s) the salvage tier read — ``"245$c"`` for
#: B1, ``"260$b/264$b"`` for B2, ``"(none)"`` for B3. Persisted on
#: the Synthesis Activity so the retrospective CLI doesn't have to
#: infer this from the method tag.
syntheticMarcSource: URIRef = BFFI_PROV.syntheticMarcSource

# --- BFFI-side predicates added by M2 salvage (P-41) ---------------------

#: P-41 Phase B — boolean flag marking synthetic-sentinel resources
#: (Agents, Works) that downstream stages must NOT key on. The B3
#: sentinel agent at :data:`SENTINEL_AGENT_UNKNOWN` carries this
#: triple. M5/M6/M8/M9 honour it via the exclude rules wired in
#: P-41 Phase B.6.
syntheticSentinel: URIRef = BFFI.syntheticSentinel

# --- Stable sentinel URIs (P-41 Phase A.4 — committed identifiers) -------

#: P-41 Phase B — single shared sentinel agent URI for B3
#: (anonymous-by-convention) salvages. Multiple records sharing this
#: URI is correct — they share the property "no known author", not
#: the claim of being by the same person. Carries the
#: :data:`syntheticSentinel` flag. Committed identifier per
#: ``CLAUDE.md`` § "Committed identifiers"; do not change without
#: surfacing.
SENTINEL_AGENT_UNKNOWN: URIRef = URIRef("http://urn.fi/URN:NBN:fi:bib:agent:unknown")

# --- BFFI-side AdminMetadata predicates added by M9 ----------------------

sourceConsulted: URIRef = BFFI.sourceConsulted

# --- Stable AdminMetadata authentication-state URIs (M9) -----------------

AUTH_NEEDS_REVIEW: URIRef = BIB["auth/needs-review"]
AUTH_VERIFIED: URIRef = BIB["auth/verified"]

# --- Compaction sentinel (provenance-meta graph) -------------------------

lastCompactedAt: URIRef = BFFI_PROV.lastCompactedAt

# --- Stable agent / process URIs (defined in config/bffi-admin-vocabulary.ttl)

AGENT_MARC2BIBFRAME2: URIRef = BIB["agent/marc2bibframe2"]
GEN_PROCESS_PIPELINE_V0_1_0: URIRef = BIB["gen-process/bffi-pipeline/v0.1.0"]
DESC_CONV_BFFI_1_0_0: URIRef = BIB["desc-conv/bffi-1.0.0"]
DESC_LEVEL_MINIMUM: URIRef = BIB["desc-level/minimum"]
ENC_LEVEL_AUTO: URIRef = BIB["enc-level/auto"]
AUTH_AUTO_MERGED: URIRef = BIB["auth/auto-merged"]
RECORDING_SOURCE_HELMET: URIRef = BIB["recording-source/helmet"]
METADATA_LICENSOR_CC0: URIRef = BIB["metadata-licensor/cc0"]
HELMET_SOURCE_URI: URIRef = URIRef("http://urn.fi/URN:NBN:fi:bib:source:helmet")

# --- AdminMetadata predicates --------------------------------------------

adminMetadata: URIRef = BFFI.adminMetadata
adminMetadataFor: URIRef = BFFI.adminMetadataFor
descriptionCreationDate: URIRef = BFFI.descriptionCreationDate
descriptionChangeDate: URIRef = BFFI.descriptionChangeDate
dateGenerated: URIRef = BFFI.dateGenerated
descriptionModifier: URIRef = BFFI.descriptionModifier
descriptionConventions: URIRef = BFFI.descriptionConventions
descriptionLevel: URIRef = BFFI.descriptionLevel
encodingLevel: URIRef = BFFI.encodingLevel
descriptionAuthentication: URIRef = BFFI.descriptionAuthentication
generationProcess: URIRef = BFFI.generationProcess
metadataLicensor: URIRef = BFFI.metadataLicensor
recordingSource: URIRef = BFFI.recordingSource
sourceMetadata: URIRef = BFFI.sourceMetadata

AdminMetadata: URIRef = BFFI.AdminMetadata

__all__ = [
    "AGENT_MARC2BIBFRAME2",
    "AUTH_AUTO_MERGED",
    "AUTH_NEEDS_REVIEW",
    "AUTH_VERIFIED",
    "BF",
    "BFFI",
    "BFFI_PROV",
    "BIB",
    "DESC_CONV_BFFI_1_0_0",
    "DESC_LEVEL_MINIMUM",
    "ENC_LEVEL_AUTO",
    "GEN_PROCESS_PIPELINE_V0_1_0",
    "HELMET_SOURCE_URI",
    "METADATA_LICENSOR_CC0",
    "MINT_ANCHOR_ANONYMOUS_WORK",
    "MINT_ANCHOR_FIRST_CONTRIBUTOR",
    "MINT_ANCHOR_PRIMARY_AUTHOR",
    "PROV",
    "RDF",
    "RDFS",
    "RECORDING_SOURCE_HELMET",
    "SENTINEL_AGENT_UNKNOWN",
    "SKOS",
    "XSD",
    "AdminMetadata",
    "HumanReview",
    "MarcConversion",
    "Reconciliation",
    "Synthesis",
    "WorkMergeDecision",
    "adminMetadata",
    "adminMetadataFor",
    "cacheHit",
    "candidateAuthorityUri",
    "chosenAuthorityUri",
    "confidence",
    "converterVersion",
    "dateGenerated",
    "decision",
    "descriptionAuthentication",
    "descriptionChangeDate",
    "descriptionConventions",
    "descriptionCreationDate",
    "descriptionLevel",
    "descriptionModifier",
    "divergingField",
    "embeddingSimilarity",
    "encodingLevel",
    "generationProcess",
    "helmetBibId",
    "inputLiteral",
    "lastCompactedAt",
    "lexicalSimilarity",
    "matchingField",
    "metadataLicensor",
    "mintAnchor",
    "modelId",
    "promptHash",
    "promptSource",
    "provider",
    "rationale",
    "rawResponse",
    "recordingSource",
    "reviewNote",
    "seed",
    "sourceConsulted",
    "sourceMetadata",
    "sourceVocabulary",
    "stage",
    "syntheticConfidence",
    "syntheticField",
    "syntheticMarcSource",
    "syntheticMethod",
    "syntheticSentinel",
    "syntheticTier",
    "syntheticValue",
    "temperature",
]
