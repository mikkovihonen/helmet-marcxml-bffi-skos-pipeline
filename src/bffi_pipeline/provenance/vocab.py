"""Namespaces and term URIs for the PROV-O + BFFI provenance graph.

The provenance graph is layered with the BFFI-native AdminMetadata view (see
spec § 8). M2 emits the first :data:`MarcConversion` Activities; later
milestones extend the vocabulary with :data:`WorkMergeDecision` (M6) and
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

# --- bffi-prov predicates emitted by M2 -----------------------------------

helmetBibId: URIRef = BFFI_PROV.helmetBibId
converterVersion: URIRef = BFFI_PROV.converterVersion

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
    "PROV",
    "RDF",
    "RDFS",
    "RECORDING_SOURCE_HELMET",
    "SKOS",
    "XSD",
    "AdminMetadata",
    "HumanReview",
    "MarcConversion",
    "WorkMergeDecision",
    "adminMetadata",
    "adminMetadataFor",
    "converterVersion",
    "dateGenerated",
    "descriptionAuthentication",
    "descriptionChangeDate",
    "descriptionConventions",
    "descriptionCreationDate",
    "descriptionLevel",
    "descriptionModifier",
    "encodingLevel",
    "generationProcess",
    "helmetBibId",
    "metadataLicensor",
    "recordingSource",
    "sourceMetadata",
]
