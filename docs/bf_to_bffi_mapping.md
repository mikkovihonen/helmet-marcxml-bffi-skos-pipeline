# `bf:*` → `bffi:*` mapping reference

## Background

This document maps every `bf:*` (BIBFRAME) term encountered when converting MARCXML records to BFFI, to its BFFI-namespace counterpart. The conversion path is two-stage: **MARCXML → BIBFRAME** via the LC marc2bibframe2 XSLT, then **BIBFRAME → BFFI** via SPARQL CONSTRUCT (or equivalent RDF processing).

BFFI's namespace is closed — emit-side BFFI graphs should not carry `bf:*` terms — so every `bf:*` token in the BIBFRAME intermediate needs either a direct BFFI replacement, a routing rule when no direct counterpart exists, or NLF input when no working substitute is yet defined. The mapping below enumerates each case.

The Classes and Predicates tables below are **auto-generated** by the `bffi-pipeline regenerate-mapping-tables` command. The generator covers every `bf:*` term BIBFRAME 3.0.1 declares (450 in total — 224 classes + 226 properties) and consults three sources:

- **The LoC BIBFRAME 3.0.1 ontology** (RDF/XML, 2025-12-03 release). Provides the universe of `bf:*` terms.
- **The BFFI 1.0.0 ontology** (RDF/XML; based on BIBFRAME 2.4.0). Provides `bf:*` ↔ `bffi:*` mapping relations (`owl:equivalentClass`, `owl:equivalentProperty`, `rdfs:subPropertyOf`, `bffi-meta:{broadMatch,closeMatch,exactMatch,narrowMatch}`).
- **The discriminator-routed terms** the pipeline applies at conversion time (`bf:Hub`, `bf:Isbn` and other `bf:Identifier` descendants, `bf:VariantTitle` and the other Title subclasses, axis-default classes/predicates, `bf:hasSeries`, `bf:accompaniedBy`, `bf:provisionActivityStatement`). These have no direct BFFI mapping but get per-instance handling.

The two ontologies together let the conversion derive routings the bare BFFI-side walks miss — e.g. the ontology-driven Identifier-scheme routing walks `bf:Identifier`'s 50+ subclasses in BIBFRAME and routes each to `bffi:Identifier + bffi:source <…/identifiers/{scheme}>`. The `bffi-pipeline diagnose-mappings` command provides an interactive view of the BIBFRAME ↔ BFFI reachability classification.

### Status legend

| Status | Meaning |
|---|---|
| **clean** | Direct 1-hop `owl:equivalentClass` / `owl:equivalentProperty` to a `bffi:*` term. The clean-rename pass handles it; nothing else needed. |
| **routed** | No direct equivalence; the per-instance data determines which existing `bffi:*` class applies (see the routing callouts below each table). The `Handler` column names the routing function. |
| **drop** | The triple is deleted at emit time — either because the signal is redundant with another channel (`bf:variantType` is encoded in `bffi:marcKey`'s MARC tag) or because BFFI has no carrier and reaching for a foreign vocabulary would violate namespace discipline. Drops are bounded, observable data losses; each is a candidate for a future BFFI extension via NLF. |
| ***semantic-shift*** | Best reach uses `bffi-meta:broadMatch` / `closeMatch` / `narrowMatch` / `exactMatch`. The BFFI side carries a related but not identical concept. |
| ***inherited*** | Best reach is a taxonomy walk through `rdfs:subClassOf` / `rdfs:subPropertyOf` chains; an ancestor's clean rename covers the term transitively (e.g. `bf:AbbreviatedTitle` reaches `bffi:Title` via the BIBFRAME class hierarchy + the `bffi:Title ≡ bf:Title` equivalence). |
| **GAP** | No path of any length and no routing handler — requires NLF input, a new routing, or a future BFFI release. |

### Document conventions

| Element | Meaning |
|---|---|
| **Re-anchor pattern** | A `bffi:*` class has subclasses (`bffi:Sub rdfs:subClassOf bffi:Anchor`) and the anchor is `owl:equivalentClass bf:X`. Emitting any subclass alone makes the BIBFRAME class recoverable by OWL inference — no need to dual-type with `bf:*`. |
| **"Also satisfies" column** | Per-row inheritance closure: emitting the row's BFFI replacement makes the listed BIBFRAME classes recoverable by OWL inference, walked from `bffi:Y` upward through `rdfs:subClassOf` and counting every BFFI ancestor with `owl:equivalentClass bf:*`. Empty when there's no inheritance chain beyond the direct equivalence. |

## Classes (sorted alphabetically)

The table below is **auto-generated** by the `bffi-pipeline regenerate-mapping-tables` command from the LoC BIBFRAME ontology, the BFFI ontology, and the pipeline's discriminator-routing registry. Do not edit between the markers — your changes will be lost on the next regeneration.

<!-- BEGIN AUTO: classes -->

| `bf:` class | Status | `bffi:*` replacement | Link kind | Also satisfies (via inference) | Handler |
|---|---|---|---|---|---|
| `bf:AbbreviatedTitle` | *inherited* | `bffi:Title` | bf:subClassOf → bf:subClassOf → owl:equivalentClass | — | — |
| `bf:AccessPolicy` | **clean** | `bffi:AccessPolicy` | owl:equivalentClass | `bf:UsageAndAccessPolicy` | — |
| `bf:AccessionNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/accession-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:AcquisitionSource` | **clean** | `bffi:AcquisitionSource` | owl:equivalentClass | — | — |
| `bf:AdminMetadata` | **clean** | `bffi:AdminMetadata` | owl:equivalentClass | — | — |
| `bf:Agent` | **clean** | `bffi:Agent` | owl:equivalentClass | — | — |
| `bf:Ansi` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/ansi>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:AppliedMaterial` | **clean** | `bffi:AppliedMaterial` | owl:equivalentClass | `bf:Material` | — |
| `bf:Archival` | **clean** | `bffi:Archival` | owl:equivalentClass | `bf:Instance` | — |
| `bf:Arrangement` | **clean** | `bffi:Arrangement` | owl:equivalentClass | `bf:Work` | — |
| `bf:AspectRatio` | **clean** | `bffi:AspectRatio` | owl:equivalentClass | — | — |
| `bf:Audio` | **routed** | `bffi:NonMusicAudioWork` (Work-axis) / `bffi:NonMusicAudioExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:AudioIssueNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/audio-issue-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:AudioTake` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/audio-take>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Barcode` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/barcode>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:BaseMaterial` | **clean** | `bffi:BaseMaterial` | owl:equivalentClass | `bf:Material` | — |
| `bf:Binding` | **clean** | `bffi:Binding` | owl:equivalentClass | — | — |
| `bf:BookFormat` | **clean** | `bffi:BookFormat` | owl:equivalentClass | — | — |
| `bf:BroadcastStandard` | **clean** | `bffi:BroadcastStandard` | owl:equivalentClass | `bf:VideoCharacteristic` | — |
| `bf:Capture` | **clean** | `bffi:Capture` | owl:equivalentClass | — | — |
| `bf:CaptureStorage` | **clean** | `bffi:CaptureStorage` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:Carrier` | **clean** | `bffi:Carrier` | owl:equivalentClass | — | — |
| `bf:Cartographic` | **clean** | `bffi:Cartographic` | owl:equivalentClass | — | — |
| `bf:CartographicDataType` | **clean** | `bffi:CartographicDataType` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:CartographicObjectType` | **clean** | `bffi:CartographicObjectType` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:Cartography` | **routed** | `bffi:CartographyWork` (Work-axis) / `bffi:CartographyExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:Chronology` | **clean** | `bffi:Chronology` | owl:equivalentClass | `bf:EnumerationAndChronology` | — |
| `bf:Classification` | **clean** | `bffi:Classification` | owl:equivalentClass | — | — |
| `bf:ClassificationDdc` | **clean** | `bffi:ClassificationDdc` | owl:equivalentClass | `bf:Classification` | — |
| `bf:ClassificationLcc` | **clean** | `bffi:ClassificationLcc` | owl:equivalentClass | `bf:Classification` | — |
| `bf:ClassificationNal` | **clean** | `bffi:ClassificationNal` | owl:equivalentClass | `bf:Classification` | — |
| `bf:ClassificationNlm` | **clean** | `bffi:ClassificationNlm` | owl:equivalentClass | `bf:Classification` | — |
| `bf:ClassificationUdc` | **clean** | `bffi:ClassificationUdc` | owl:equivalentClass | `bf:Classification` | — |
| `bf:Coden` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/coden>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Collection` | *semantic-shift* | `bffi:CollectionExpression` | bffi-meta:broadMatch | `bf:Work` | — |
| `bf:CollectionArrangement` | **clean** | `bffi:CollectionArrangement` | owl:equivalentClass | — | — |
| `bf:CollectiveTitle` | **routed** | `bffi:Title` (anchor; subclass info preserved on `bffi:marcKey`) | discriminator: marcKey | — | `route_title_variants` |
| `bf:ColorContent` | **clean** | `bffi:ColorContent` | owl:equivalentClass | — | — |
| `bf:Content` | **clean** | `bffi:Content` | owl:equivalentClass | — | — |
| `bf:ContentAccessibility` | **clean** | `bffi:ContentAccessibility` | owl:equivalentClass | — | — |
| `bf:Contribution` | **clean** | `bffi:Contribution` | owl:equivalentClass | — | — |
| `bf:CopyrightNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/copyright-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:CopyrightRegistration` | **clean** | `bffi:CopyrightRegistration` | owl:equivalentClass | — | — |
| `bf:CoverArt` | **clean** | `bffi:CoverArt` | owl:equivalentClass | — | — |
| `bf:Dataset` | **clean** | `bffi:Dataset` | owl:equivalentClass | `bf:Work` | — |
| `bf:DescriptionAuthentication` | **clean** | `bffi:DescriptionAuthentication` | owl:equivalentClass | — | — |
| `bf:DescriptionConventions` | **clean** | `bffi:DescriptionConventions` | owl:equivalentClass | — | — |
| `bf:DescriptionLevel` | **clean** | `bffi:DescriptionLevel` | owl:equivalentClass | — | — |
| `bf:DigitalCharacteristic` | **clean** | `bffi:DigitalCharacteristic` | owl:equivalentClass | — | — |
| `bf:Dissertation` | **clean** | `bffi:Dissertation` | owl:equivalentClass | — | — |
| `bf:DissertationIdentifier` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/dissertation-identifier>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Distribution` | **clean** | `bffi:Distribution` | owl:equivalentClass | `bf:ProvisionActivity` | — |
| `bf:Doi` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/doi>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:DramaticRole` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop | defensive (upstream-stability) | — | `drop_music_residue` |
| `bf:Ean` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/ean>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Eidr` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/eidr>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Electronic` | **clean** | `bffi:Electronic` | owl:equivalentClass | `bf:Instance` | — |
| `bf:Emulsion` | **clean** | `bffi:Emulsion` | owl:equivalentClass | — | — |
| `bf:EncodedBitrate` | **clean** | `bffi:EncodedBitrate` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:EncodingFormat` | **clean** | `bffi:EncodingFormat` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:Ensemble` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:EnsembleSize` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:Enumeration` | **clean** | `bffi:Enumeration` | owl:equivalentClass | `bf:EnumerationAndChronology` | — |
| `bf:EnumerationAndChronology` | **clean** | `bffi:EnumerationAndChronology` | owl:equivalentClass | — | — |
| `bf:Event` | **clean** | `bffi:Event` | owl:equivalentClass | — | — |
| `bf:Extent` | **clean** | `bffi:Extent` | owl:equivalentClass | — | — |
| `bf:Family` | **clean** | `bffi:Family` | owl:equivalentClass | `bf:Agent` | — |
| `bf:FileSize` | **clean** | `bffi:FileSize` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:FileType` | **clean** | `bffi:FileType` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:Fingerprint` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/fingerprint>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:FontSize` | **clean** | `bffi:FontSize` | owl:equivalentClass | — | — |
| `bf:Frequency` | **clean** | `bffi:Frequency` | owl:equivalentClass | — | — |
| `bf:Generation` | **clean** | `bffi:Generation` | owl:equivalentClass | — | — |
| `bf:GenerationProcess` | **clean** | `bffi:GenerationProcess` | owl:equivalentClass | — | — |
| `bf:GenreForm` | **clean** | `bffi:GenreForm` | owl:equivalentClass | — | — |
| `bf:GeographicCoverage` | **clean** | `bffi:GeographicCoverage` | owl:equivalentClass | — | — |
| `bf:GrooveCharacteristic` | **clean** | `bffi:GrooveCharacteristic` | owl:equivalentProperty | `bf:SoundCharacteristic` | — |
| `bf:Gtin14Number` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/gtin14-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Hdl` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/hdl>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Hub` | **routed** | `bffi:Work` / `bffi:Expression` / `bffi:Arrangement` / `bffi:SeriesExpression` (per marcKey) | discriminator: marcKey | — | `route_hubs` |
| `bf:Identifier` | **clean** | `bffi:Identifier` | owl:equivalentClass | — | — |
| `bf:Illustration` | **clean** | `bffi:Illustration` | owl:equivalentClass | — | — |
| `bf:ImmediateAcquisition` | **clean** | `bffi:ImmediateAcquisition` | owl:equivalentClass | — | — |
| `bf:Instance` | **clean** | `bffi:Manifestation` | owl:equivalentClass | — | — |
| `bf:Integrating` | **clean** | `bffi:Integrating` | owl:equivalentClass | `bf:Work` | — |
| `bf:IntendedAudience` | **clean** | `bffi:IntendedAudience` | owl:equivalentClass | — | — |
| `bf:Isan` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/isan>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Isbn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/isbn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Ismn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/ismn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Isni` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/isni>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Iso` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/iso>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Isrc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/isrc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Issn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/issn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:IssnL` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/issn-l>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Issuance` | *semantic-shift* | `bffi:ExtensionPlan` | bffi-meta:broadMatch | — | — |
| `bf:Istc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/istc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Iswc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/iswc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Item` | **clean** | `bffi:Item` | owl:equivalentClass | — | — |
| `bf:Jurisdiction` | **clean** | `bffi:Jurisdiction` | owl:equivalentClass | `bf:Agent` | — |
| `bf:KeyMode` | **routed** | (class typing removed implicitly when the parent `bf:keyMode` structured bnode is collapsed to a `bffi:musicKey` literal) | bnode subgraph cleanup | — | `route_music_key` |
| `bf:KeyTitle` | **routed** | `bffi:Title` (anchor; subclass info preserved on `bffi:marcKey`) | discriminator: marcKey | — | `route_title_variants` |
| `bf:Kit` | **clean** | `bffi:Kit` | owl:equivalentClass | `bf:MixedMaterial`, `bf:Work` | — |
| `bf:Language` | **clean** | `bffi:Language` | owl:equivalentClass | — | — |
| `bf:Layout` | **clean** | `bffi:Layout` | owl:equivalentClass | — | — |
| `bf:LcOverseasAcq` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/lc-overseas-acq>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Lccn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/lccn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Local` | **clean** | `bffi:Local` | owl:equivalentClass | `bf:Identifier` | — |
| `bf:Manufacture` | **clean** | `bffi:Manufacture` | owl:equivalentClass | `bf:ProvisionActivity` | — |
| `bf:Manuscript` | **clean** | `bffi:Manuscript` | owl:equivalentClass | `bf:Work` | — |
| `bf:Material` | **clean** | `bffi:Material` | owl:equivalentClass | — | — |
| `bf:MatrixNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/matrix-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Media` | **clean** | `bffi:Media` | owl:equivalentClass | — | — |
| `bf:MediumComponent` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:MediumComponentQualifier` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:MediumOfPerformance` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:Meeting` | **clean** | `bffi:Meeting` | owl:equivalentClass | `bf:Agent` | — |
| `bf:Microform` | **clean** | `bffi:Microform` | owl:equivalentClass | `bf:Instance` | — |
| `bf:MixedMaterial` | **clean** | `bffi:MixedMaterial` | owl:equivalentClass | `bf:Work` | — |
| `bf:Mode` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop | defensive (upstream-stability) | — | `drop_music_mode_residue` |
| `bf:Modification` | **clean** | `bffi:Modification` | owl:equivalentClass | `bf:ProvisionActivity` | — |
| `bf:Monograph` | **routed** | `bffi:MonographWork` (Work-axis) / `bffi:MonographExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:Mount` | **clean** | `bffi:Mount` | owl:equivalentClass | — | — |
| `bf:MovementNotation` | **clean** | `bffi:MovementNotation` | owl:equivalentClass | `bf:Notation` | — |
| `bf:MovingImage` | **routed** | `bffi:MovingImageWork` (Work-axis) / `bffi:MovingImageExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:Multimedia` | **clean** | `bffi:Multimedia` | owl:equivalentClass | `bf:Work` | — |
| `bf:MusicAudio` | **routed** | `bffi:MusicWork` (Work-axis) / `bffi:MusicAudioExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:MusicDistributorNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/music-distributor-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:MusicEnsemble` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:MusicFormat` | **clean** | `bffi:MusicFormat` | owl:equivalentClass | — | — |
| `bf:MusicInstrument` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:MusicMedium` | **clean** | `bffi:MusicMedium` | owl:equivalentClass | — | — |
| `bf:MusicNotation` | **clean** | `bffi:MusicNotation` | owl:equivalentClass | `bf:Notation` | — |
| `bf:MusicPlate` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/music-plate>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:MusicPublisherNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/music-publisher-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:MusicVoice` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | — | `route_music_medium` |
| `bf:Nbn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/nbn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:NonMusicAudio` | **routed** | `bffi:NonMusicAudioWork` (Work-axis) / `bffi:NonMusicAudioExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:NotatedMovement` | **clean** | `bffi:NotatedMovement` | owl:equivalentClass | `bf:Work` | — |
| `bf:NotatedMusic` | **clean** | `bffi:NotatedMusic` | owl:equivalentClass | `bf:Work` | — |
| `bf:Notation` | **clean** | `bffi:Notation` | owl:equivalentClass | — | — |
| `bf:Note` | **clean** | `bffi:Note` | owl:equivalentClass | — | — |
| `bf:Object` | **clean** | `bffi:Object` | owl:equivalentClass | `bf:Work` | — |
| `bf:ObjectCount` | **clean** | `bffi:ObjectCount` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:OclcNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/oclc-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:OpusNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/opus-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Organization` | **clean** | `bffi:Organization` | owl:equivalentClass | `bf:Agent` | — |
| `bf:ParallelTitle` | **routed** | `bffi:Title` (anchor; subclass info preserved on `bffi:marcKey`) | discriminator: marcKey | — | `route_title_variants` |
| `bf:Person` | **clean** | `bffi:Person` | owl:equivalentClass | `bf:Agent` | — |
| `bf:Place` | **clean** | `bffi:Place` | owl:equivalentClass | — | — |
| `bf:PlaybackChannels` | **clean** | `bffi:PlaybackChannels` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:PlaybackCharacteristic` | **clean** | `bffi:PlaybackCharacteristic` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:PlayingSpeed` | **clean** | `bffi:PlayingSpeed` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:Polarity` | **clean** | `bffi:Polarity` | owl:equivalentClass | — | — |
| `bf:PostalRegistration` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/postal-registration>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:PresentationFormat` | **clean** | `bffi:PresentationFormat` | owl:equivalentClass | `bf:ProjectionCharacteristic` | — |
| `bf:PrimaryContribution` | **clean** | `bffi:PrimaryContribution` | owl:equivalentClass | `bf:Contribution` | — |
| `bf:Print` | **clean** | `bffi:Print` | owl:equivalentClass | `bf:Instance` | — |
| `bf:Production` | **clean** | `bffi:Production` | owl:equivalentClass | `bf:ProvisionActivity` | — |
| `bf:ProductionMethod` | **clean** | `bffi:ProductionMethod` | owl:equivalentClass | — | — |
| `bf:Projection` | **clean** | `bffi:Projection` | owl:equivalentClass | — | — |
| `bf:ProjectionCharacteristic` | **clean** | `bffi:ProjectionCharacteristic` | owl:equivalentClass | — | — |
| `bf:ProjectionSpeed` | **clean** | `bffi:ProjectionSpeed` | owl:equivalentClass | `bf:ProjectionCharacteristic` | — |
| `bf:ProvisionActivity` | **clean** | `bffi:ProvisionActivity` | owl:equivalentClass | — | — |
| `bf:PubFrequency` | **clean** | `bffi:PubFrequency` | owl:equivalentClass | — | — |
| `bf:Publication` | **clean** | `bffi:Publication` | owl:equivalentClass | `bf:ProvisionActivity` | — |
| `bf:PublisherNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/publisher-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:RecordingMedium` | **clean** | `bffi:RecordingMedium` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:RecordingMethod` | **clean** | `bffi:RecordingMethod` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:ReductionRatio` | **clean** | `bffi:ReductionRatio` | owl:equivalentClass | — | — |
| `bf:RegionalEncoding` | **clean** | `bffi:RegionalEncoding` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:Relation` | **clean** | `bffi:Relation` | owl:equivalentClass | — | — |
| `bf:Relationship` | **clean** | `bffi:Relationship` | owl:equivalentClass | — | — |
| `bf:Relief` | **clean** | `bffi:Relief` | owl:equivalentClass | — | — |
| `bf:ReportNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/report-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Resolution` | **clean** | `bffi:Resolution` | owl:equivalentClass | `bf:DigitalCharacteristic` | — |
| `bf:RetentionPolicy` | **clean** | `bffi:RetentionPolicy` | owl:equivalentClass | `bf:UsageAndAccessPolicy` | — |
| `bf:Review` | **routed** | `bffi:BibframeWork` (anchored — no axis split) | anchor downgrade (no Work/Expression alternative) | — | `route_axis_default_classes` |
| `bf:Role` | **clean** | `bffi:Role` | owl:equivalentClass | — | — |
| `bf:Scale` | **clean** | `bffi:Scale` | owl:equivalentClass | — | — |
| `bf:Script` | **clean** | `bffi:Script` | owl:equivalentClass | `bf:Notation` | — |
| `bf:Serial` | **routed** | `bffi:SerialWork` (Work-axis) / `bffi:SerialExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:SerialNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/serial-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Series` | **routed** | `bffi:SeriesWork` (Work-axis) / `bffi:SeriesExpression` (Expression-axis) | discriminator: subject's Work-axis co-type signal | — | `route_axis_default_classes` |
| `bf:ShelfMark` | **clean** | `bffi:ShelfMark` | owl:equivalentClass | `bf:Identifier` | — |
| `bf:ShelfMarkDdc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/shelf-mark-ddc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:ShelfMarkLcc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/shelf-mark-lcc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:ShelfMarkNlm` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/shelf-mark-nlm>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:ShelfMarkUdc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/shelf-mark-udc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Sici` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/sici>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:SoundCharacteristic` | **clean** | `bffi:SoundCharacteristic` | owl:equivalentClass | — | — |
| `bf:SoundContent` | **clean** | `bffi:SoundContent` | owl:equivalentClass | — | — |
| `bf:Source` | **clean** | `bffi:Source` | owl:equivalentClass | — | — |
| `bf:Status` | **clean** | `bffi:Status` | owl:equivalentClass | — | — |
| `bf:StillImage` | **clean** | `bffi:StillImage` | owl:equivalentClass | `bf:Work` | — |
| `bf:StockNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/stock-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Strn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/strn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:StudyNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/study-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Sublocation` | **clean** | `bffi:Sublocation` | owl:equivalentClass | — | — |
| `bf:Summary` | **clean** | `bffi:Summary` | owl:equivalentClass | — | — |
| `bf:SupplementaryContent` | **clean** | `bffi:SupplementaryContent` | owl:equivalentClass | — | — |
| `bf:SystemRequirement` | **clean** | `bffi:SystemRequirement` | owl:equivalentClass | — | — |
| `bf:TableOfContents` | **clean** | `bffi:TableOfContents` | owl:equivalentClass | — | — |
| `bf:Tactile` | **clean** | `bffi:Tactile` | owl:equivalentClass | `bf:Instance` | — |
| `bf:TactileNotation` | **clean** | `bffi:TactileNotation` | owl:equivalentClass | `bf:Notation` | — |
| `bf:TapeConfig` | **clean** | `bffi:TapeConfig` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:Tempo` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop | defensive (upstream-stability) | — | `drop_music_residue` |
| `bf:Temporal` | **clean** | `bffi:Temporal` | owl:equivalentClass | — | — |
| `bf:Text` | **clean** | `bffi:Text` | owl:equivalentClass | `bf:Work` | — |
| `bf:ThematicCatalogNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/thematic-catalog-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Title` | **clean** | `bffi:Title` | owl:equivalentClass | — | — |
| `bf:Topic` | **clean** | `bffi:Topic` | owl:equivalentClass | — | — |
| `bf:TrackConfig` | **clean** | `bffi:TrackConfig` | owl:equivalentClass | `bf:SoundCharacteristic` | — |
| `bf:TransliteratedTitle` | *inherited* | `bffi:Title` | bf:subClassOf → bf:subClassOf → owl:equivalentClass | — | — |
| `bf:Unit` | **clean** | `bffi:Unit` | owl:equivalentClass | — | — |
| `bf:Upc` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/upc>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Urn` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/urn>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:UsageAndAccessPolicy` | **clean** | `bffi:UsageAndAccessPolicy` | owl:equivalentClass | — | — |
| `bf:UsePolicy` | **clean** | `bffi:UsePolicy` | owl:equivalentClass | `bf:UsageAndAccessPolicy` | — |
| `bf:VariantTitle` | **routed** | `bffi:Title` (anchor; subclass info preserved on `bffi:marcKey`) | discriminator: marcKey | — | `route_title_variants` |
| `bf:VideoCharacteristic` | **clean** | `bffi:VideoCharacteristic` | owl:equivalentClass | — | — |
| `bf:VideoFormat` | **clean** | `bffi:VideoFormat` | owl:equivalentClass | `bf:VideoCharacteristic` | — |
| `bf:VideoRecordingNumber` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/videorecording-number>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:VideogamePlatformId` | **routed** | `bffi:Identifier` + `bffi:source <…/identifiers/videogame-platform-id>` | discriminator: BIBFRAME subclass → LoC scheme URI | — | `route_identifier_schemes` |
| `bf:Work` | **clean** | `bffi:BibframeWork` | owl:equivalentClass | — | — |

_224 terms total: 144 clean, 73 routed, 3 drop, 2 inherited, 2 semantic-shift._

<!-- END AUTO: classes -->

### Hub routing — `bf:Hub` → `bffi:Expression` vs `bffi:Work`

The BFFI ontology has no `bffi:Hub`. But `bf:Hub` isn't a single semantic — it's BIBFRAME's flat aggregation of two FRBR/RDA-distinct things: a Work-axis reference (e.g. "Beethoven · Symphony no. 5") vs an Expression-axis reference (e.g. "English translation of *À la recherche*"). BFFI separates them. So `bf:Hub` routes to a `bffi:Work`-side or `bffi:Expression`-side class **based on what facets the source MARC entry carries**.

The discriminator is the **marcKey value** carried on every `bf:Hub` bnode in the BIBFRAME input. The routing reads it as **`bflc:marcKey`** (what marc2bibframe2 literally emits — the BFLC-standard predicate); the BFFI-side `bffi:marcKey` doesn't exist as a triple at routing time, only by `owl:equivalentProperty` inference. The marcKey value encodes the full MARC field as `<tag><ind1><ind2> <subfields>` — for example `"73002$aSymphonie no. 5$lenglanti$omikrofilmi"`. The routing reads two things from this string:

1. **First 3 characters** — the MARC tag (`240` / `730` / `740` / `100` / `700` / `130` / `830`) — determines whether the Hub came from a uniform title, an analytical added entry, an author-attributed title, a series, etc.
2. **Subfield codes embedded in the value** (`$l`, `$o`, `$r`, `$s`, `$m`, `$t`) — present Expression-level facets that signal the Hub IS an Expression (a specific realization), not just a Work reference.

When the BIBFRAME emit also decomposes specific subfields into structured predicates on the Hub bnode (e.g., `bf:language` URI for `$l`, `bf:musicMedium` for `$m`, `bf:keyMode` for `$r`, `bf:Arrangement` rdf:type for `$o`), the routing can read those directly — same signal, just decomposed. The marcKey-substring check is the universal fallback that always works.

Routing table (read top-to-bottom; first match wins):

| Signal | Routed `bffi:*` type | Why |
|---|---|---|
| `bflc:marcKey` contains `$o` arrangement subfield, OR `bf:Arrangement` in the Hub's `rdf:type` set | `bffi:Arrangement` | `bffi:Arrangement` is `owl:equivalentClass bf:Arrangement` AND `rdfs:subClassOf bffi:Expression` — perfect direct match; arrangements are Expression-level by definition |
| `bflc:marcKey` contains `$m` medium-of-performance subfield AND record content-type is audio recording | `bffi:MusicAudioExpression` | Expression-axis content-type-specific subclass of `bffi:Expression` |
| `bflc:marcKey` contains `$m` AND record content-type is notated music | `bffi:NotatedMusic` (`owl:equivalentClass bf:NotatedMusic`) | Expression-axis subclass of `bffi:Expression` |
| `bflc:marcKey` contains `$l` language qualifier (the dominant Expression signal) | `bffi:Expression` (+ `bffi:languageOfExpression` triple carrying the language URI) | `$l` names the language OF an Expression; the Hub IS that Expression |
| `bflc:marcKey` contains `$r` key subfield | `bffi:Expression` (+ `bffi:musicKey` literal) | Key is an Expression-level attribute |
| `bflc:marcKey` contains `$s` version subfield | `bffi:Expression` (+ `bffi:version`) | Version is an Expression-level attribute |
| First 3 chars of `bflc:marcKey` are `100` or `700` AND `$t` subfield present (author-attributed uniform title) | `bffi:Work` | The cataloguer named a Work via the author entry, not an Expression; treat as Work-level |
| First 3 chars of `bflc:marcKey` are `130` or `830` (series uniform title) | `bffi:SeriesWork` *or* `bffi:SeriesExpression` (axis-pick per record context) | Series entry; axis follows the per-record context |
| First 3 chars of `bflc:marcKey` are `730` or `740` AND no Expression-level subfields present | `bffi:Work` | Plain transcribed title with no Expression signal — cataloguer named a Work |
| Otherwise (fallback) | `bffi:Work` | Absent any Expression-level signal, default to the Work axis |

**BFFI emit side**: the routed `bffi:Work` / `bffi:Expression` carries `bffi:marcKey` (BFFI-namespace; `owl:equivalentProperty bflc:marcKey`) with the same marcKey value forwarded from the `bf:Hub` bnode. Downstream BFFI consumers querying the canonical use `bffi:marcKey` (closed-namespace); BIBFRAME-aware consumers can reach `bflc:marcKey` by `owl:equivalentProperty` inference. The routing-time read is `bflc:marcKey` (since that's the literal predicate in the BIBFRAME input from marc2bibframe2); the emit-time write is `bffi:marcKey`.

What survives the migration without NLF input:

- **Zero new BFFI terms required.** Every routed type already exists in the BFFI ontology (`bffi:Work`, `bffi:Expression`, `bffi:Arrangement`, `bffi:MusicAudioExpression`, `bffi:NotatedMusic`, `bffi:SeriesWork`, `bffi:SeriesExpression`, `bffi:languageOfExpression`, `bffi:musicKey`, `bffi:version`, `bffi:marcKey`).
- **`bf:Work` recovery via inference**: both routes pass through the `bffi:BibframeWork ≡ bf:Work` anchor — `bffi:Work ⊑ bffi:BibframeWork ≡ bf:Work` AND `bffi:Expression ⊑ bffi:BibframeWork ≡ bf:Work`. BIBFRAME consumers reading the canonical see a `bf:Work`-shaped target either way (which is correct — BIBFRAME's `bf:Hub` is itself a `bf:Work`-shaped grouping).
- **Round-trip integrity**: the Expression-level facets that triggered the routing (`bffi:languageOfExpression`, `bffi:musicKey`, `bffi:version`, `bffi:Arrangement` type) are themselves recoverable via existing BFFI vocabulary — the MARC `$l` / `$o` / `$r` / `$s` subfields can be reconstructed without `bf:Hub` typing.
- **What's lost**: the BIBFRAME-specific "this is a less-rigorously-described entity" signal. BFFI's view is that sparse-description isn't a separate class — it's just a Work or Expression with minimal metadata. The information loss is ontological, not data.

### Identifier-scheme routing — every `bf:Identifier` subclass → `bffi:Identifier` + `bffi:source`

The BFFI ontology declares `bffi:Identifier ≡ bf:Identifier` but only two subclasses below it — `bffi:Local` and `bffi:ShelfMark`. BIBFRAME 3.0.1 declares **50 other subclasses** of `bf:Identifier` (ISBN, ISSN, EAN, DOI, ISNI, OCLC, opus number, music plate, postal registration, video recording number, …) — none of which the BFFI ontology references. **Their semantic content lives at the predicate level instead**, via the existing `bffi:source` + `bffi:Source` + `bffi:code` triple structure that BFFI already declares.

The routing is ontology-driven: walk `bf:Identifier`'s descendants in the BIBFRAME ontology and rewrite each one to the `bffi:Identifier` anchor + a `bffi:source <…/identifiers/<scheme-token>>` triple. The scheme-token derives from the BIBFRAME class local name via CamelCase → kebab-case (`Isbn` → `isbn`, `IssnL` → `issn-l`, `AudioIssueNumber` → `audio-issue-number`, `OclcNumber` → `oclc-number`) with two overrides (`OtherIdentifier` → `other`; `VideoRecordingNumber` → `videorecording-number`). New Identifier subclasses added to BIBFRAME in future ontology revisions get picked up automatically on the next BIBFRAME refresh — no enum to maintain.

The same pattern applies to local-library identifiers — a BFFI emit typically writes them as:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Local ;
    rdf:value "b21152068" ;
    bffi:source <http://example.org/bib:source/local-library>
] .
```

The standard-scheme replacement keeps the exact same shape with `bffi:Identifier` (the anchor class) at the top and a LoC-vocabulary URI as the scheme:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Identifier ;
    rdf:value "9780123456789" ;
    bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn>
] .
```

Routing table (one row per BIBFRAME identifier subclass we emit; the BIBFRAME type collapses into the `bffi:source` URI):

| BIBFRAME type | MARC source | BFFI emit shape |
|---|---|---|
| `bf:Isbn` | 020 (ISBN-13 / ISBN-10) | `bffi:Identifier` + `bffi:source <…/identifiers/isbn>` |
| `bf:Issn` | 022 (ISSN — serials) | `bffi:Identifier` + `bffi:source <…/identifiers/issn>` |
| `bf:Ean` | 024 ind1=3 (EAN-13 barcode) | `bffi:Identifier` + `bffi:source <…/identifiers/ean>` |
| `bf:AudioIssueNumber` | 028 (publisher number / record-label catalogue number) | `bffi:Identifier` + `bffi:source <…/identifiers/audioIssueNumber>` |
| `bf:OtherIdentifier` | 024 ind1≠3 (DOI / ASIN / Sigel / etc.) | `bffi:Identifier` + `bffi:source <…/identifiers/<scheme>>` (`<scheme>` carried from the `$2` subfield) |

LoC publishes the canonical scheme URIs under `http://id.loc.gov/vocabulary/identifiers/` (it's how BIBFRAME itself encodes its subclass tree via `bf:source` on the data side). Using these URIs as the scheme indicator means a BIBFRAME consumer that previously matched on `?ident a bf:Isbn` can re-target to `?ident bffi:source <…/identifiers/isbn>` without changing the URI vocabulary.

Optional qualifier carryover when MARC `$q` is present:

```turtle
<manifestation> bffi:identifiedBy [
    a bffi:Identifier ;
    rdf:value "9780123456789" ;
    bffi:source <http://id.loc.gov/vocabulary/identifiers/isbn> ;
    bffi:qualifier "(paperback)"
] .
```

What survives the migration without NLF input:

- **Zero new BFFI terms required.** Every emit term (`bffi:Identifier`, `bffi:identifiedBy`, `bffi:source`, `bffi:qualifier`) is already in the BFFI ontology.
- **`bf:Identifier` recovery via inference** — `bffi:Identifier ≡ bf:Identifier` is the direct equivalence anchor. BIBFRAME consumers querying "all `bf:Identifier` instances" find them all.
- **Round-trip integrity** — the `bffi:source` URI deterministically maps back to a MARC field + indicators ($2 scheme code, indicator values), so a BFFI-to-MARC reconstruction can recover `020` / `022` / `024` / `028` without `bf:*` class typing.
- **Display-layer rendering** — the `bffi:source` URIs benefit from multilingual labels in whichever display config the consumer uses (Skosmos, a custom UI, etc.) — e.g. `"ISBN"@en, "ISBN"@fi, "ISBN"@sv`. Same pattern as labels for any other URI-typed value.

What's lost:

- **Type-based SPARQL queries** — `?ident a bf:Isbn` no longer works. Consumers query `?ident bffi:source <…/identifiers/isbn>` instead. Same selectivity, different idiom.
- **BIBFRAME-side `rdfs:subClassOf bf:Identifier` inference** — a BIBFRAME consumer expecting `bf:Isbn ⊑ bf:Identifier` finds `bffi:Identifier ≡ bf:Identifier` directly, but the `bf:Isbn` subtype doesn't materialise. Acceptable trade-off — the scheme code carries the same information at a different axis.

### Title-variant routing — `bf:VariantTitle` → `bffi:Title` + `bffi:marcKey`

`bffi:Title`'s own `skos:definition` is explicit: *"Title information relating to a resource: work title, preferred title, instance title, **transcribed title**…"* — variant titles are subsumed under the single `bffi:Title` class by design. BFFI deliberately collapses the BIBFRAME `Title` / `VariantTitle` / `ParallelTitle` / `KeyTitle` subclass tree into one class with marcKey-discriminated instances.

The discriminator is the **marcKey value** carried on the title bnode. At routing time (reading the BIBFRAME `bf:VariantTitle` / `bf:Title` bnodes that marc2bibframe2 emits), the literal predicate is **`bflc:marcKey`** — `bffi:marcKey` only exists by `owl:equivalentProperty` inference at that point. The BFFI emit then writes the marcKey value forward onto the resulting `bffi:Title` bnode using **`bffi:marcKey`** (closed-namespace; `owl:equivalentProperty bflc:marcKey` keeps the BFLC-side reachable by reasoning).

The marcKey value encodes the full MARC field as `<tag><ind1><ind2> <subfields>` — for example `"24610$aOsallisuus ja yhteisöllisyys…"`. The first three characters are the MARC tag, which uniquely identifies the title kind:

| MARC tag (first 3 chars of the marcKey value) | Title kind |
|---|---|
| `245` | main title (the primary title of the resource) |
| `246` | variant title (alternative form transcribed from the resource) |
| `240` | uniform title (the conventional title for a Work) |
| `730` | added entry — analytical / related title |
| `740` | uncontrolled added entry |
| `247` | former title (predecessor title in serials) |
| `222` | key title (ISSN-registered series title) |

Emit shape (parallel `bffi:title` chains on the same parent, each bnode typed `bffi:Title`, with `bffi:marcKey` carrying the MARC field encoding):

```turtle
<expression> bffi:title <main-title-bnode> ;
             bffi:title <variant-title-bnode> .

<main-title-bnode> a bffi:Title ;
                   bffi:mainTitle "Osallisuus ja yhteisöllisyys" ;
                   bffi:marcKey "24500$aOsallisuus ja yhteisöllisyys" .

<variant-title-bnode> a bffi:Title ;
                      bffi:mainTitle "Osallisuus ja yhteisöllisyys lastenkotien retkitoiminnassa" ;
                      bffi:marcKey "24631$aOsallisuus ja yhteisöllisyys lastenkotien retkitoiminnassa" .
```

Consumers determine the title kind by reading the first three characters of `bffi:marcKey`. The round-trip back to MARC uses the same value to reconstruct the field tag + indicators + subfields verbatim — the marcKey IS the original MARC field, so reconstruction is identity.

What survives without NLF input:

- **Zero new BFFI terms** — `bffi:Title`, `bffi:mainTitle`, `bffi:title`, and `bffi:marcKey` are all in the existing BFFI vocabulary set. `bffi:marcKey owl:equivalentProperty bflc:marcKey` makes the BFLC-side counterpart reachable for BIBFRAME-aware consumers.
- **`bf:Title` recovery via inference** — `bffi:Title ≡ bf:Title` is direct equivalence; BIBFRAME consumers walking the OWL chain still see a `bf:Title`-shaped target.
- **Round-trip integrity** — `bffi:marcKey` carries the full MARC field encoding, so reconstruction is direct (read the tag, indicators, and subfields from the marcKey value).
- **Standard discriminator** — `bffi:marcKey` is a published BFFI predicate (`owl:equivalentProperty bflc:marcKey`); the underlying marcKey encoding is well-known to BIBFRAME-aware consumers. No project-internal vocabulary required.

What's lost:

- **Type-based SPARQL queries** — `?vt a bf:VariantTitle` no longer works. Consumers switch to `?t a bffi:Title ; bffi:marcKey ?mk . FILTER(STRSTARTS(?mk, "246"))`. Same selectivity, slightly more verbose.
- **The four BIBFRAME Title subclasses** (`bf:VariantTitle`, `bf:ParallelTitle`, `bf:KeyTitle`, `bf:CollectiveTitle`) all collapse into the same routing — discriminated by the MARC tag in `bffi:marcKey` instead of the class.

## Predicates (sorted alphabetically)

The table below is **auto-generated** by `bffi-pipeline regenerate-mapping-tables` from the same three sources as the Classes table above. Do not edit between the markers.

<!-- BEGIN AUTO: predicates -->

| `bf:` predicate | Status | `bffi:*` replacement | Link kind | Handler |
|---|---|---|---|---|
| `bf:absorbed` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:absorbedBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:accompaniedBy` | **routed** | `bffi:relation` → `bffi:Relation` bnode (`bffi:relationship <…/relationship/accompaniedby>`) | structured-relation chain | `route_relation_predicates` |
| `bf:accompanies` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:acquisitionSource` | **clean** | `bffi:acquisitionSource` | owl:equivalentProperty | — |
| `bf:acquisitionTerms` | **clean** | `bffi:acquisitionTerms` | owl:equivalentProperty | — |
| `bf:adminMetadata` | **clean** | `bffi:adminMetadata` | owl:equivalentProperty | — |
| `bf:adminMetadataFor` | **clean** | `bffi:adminMetadataFor` | owl:equivalentProperty | — |
| `bf:agent` | *inherited* | `bffi:agent` | rdfs:subPropertyOf | — |
| `bf:agentOf` | **routed** | `bffi:agent` (triple-swap: ?s → ?o) | inverse-direction swap | `route_inverse_predicates` |
| `bf:appliedMaterial` | **clean** | `bffi:appliedMaterial` | owl:equivalentProperty | — |
| `bf:appliedMaterialOf` | **routed** | `bffi:appliedMaterial` (triple-swap: ?s → ?o) | inverse-direction swap | `route_inverse_predicates` |
| `bf:arrangement` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:arrangementOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:ascensionAndDeclination` | **clean** | `bffi:ascensionAndDeclination` | owl:equivalentProperty | — |
| `bf:aspectRatio` | *inherited* | `bffi:aspectRatio` | rdfs:subPropertyOf | — |
| `bf:assigner` | **clean** | `bffi:assigner` | owl:equivalentProperty | — |
| `bf:associatedResource` | **clean** | `bffi:associatedResource` | owl:equivalentProperty | — |
| `bf:awards` | **clean** | `bffi:awards` | owl:equivalentProperty | — |
| `bf:baseMaterial` | **clean** | `bffi:baseMaterial` | owl:equivalentProperty | — |
| `bf:baseMaterialOf` | **routed** | `bffi:baseMaterial` (triple-swap: ?s → ?o) | inverse-direction swap | `route_inverse_predicates` |
| `bf:binding` | **clean** | `bffi:binding` | owl:equivalentProperty | — |
| `bf:bookFormat` | **clean** | `bffi:bookFormat` | owl:equivalentProperty | — |
| `bf:capture` | *inherited* | `bffi:capture` | rdfs:subPropertyOf | — |
| `bf:carrier` | *inherited* | `bffi:carrier` | rdfs:subPropertyOf | — |
| `bf:cartographicAttributes` | **clean** | `bffi:cartographicAttributes` | owl:equivalentProperty | — |
| `bf:changeDate` | **clean** | `bffi:changeDate` | owl:equivalentProperty | — |
| `bf:classification` | **clean** | `bffi:classification` | owl:equivalentProperty | — |
| `bf:classificationPortion` | **clean** | `bffi:classificationPortion` | owl:equivalentProperty | — |
| `bf:code` | **clean** | `bffi:code` | owl:equivalentProperty | — |
| `bf:collectionArrangement` | **clean** | `bffi:collectionArrangement` | owl:equivalentProperty | — |
| `bf:collectionArrangementOf` | **clean** | `bffi:collectionArrangementOf` | owl:equivalentProperty | — |
| `bf:collectionOrganization` | **clean** | `bffi:collectionOrganization` | owl:equivalentProperty | — |
| `bf:colorContent` | **clean** | `bffi:colorContent` | owl:equivalentProperty | — |
| `bf:content` | *inherited* | `bffi:content` | rdfs:subPropertyOf | — |
| `bf:contentAccessibility` | **clean** | `bffi:contentAccessibility` | owl:equivalentProperty | — |
| `bf:continuedBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:continuedInPartBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:continues` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:continuesInPart` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:contribution` | **clean** | `bffi:contribution` | owl:equivalentProperty | — |
| `bf:contributionOf` | **routed** | `bffi:contribution` (triple-swap: ?s → ?o) | inverse-direction swap | `route_inverse_predicates` |
| `bf:coordinates` | **clean** | `bffi:coordinates` | owl:equivalentProperty | — |
| `bf:copyrightDate` | **clean** | `bffi:copyrightDate` | owl:equivalentProperty | — |
| `bf:copyrightRegistration` | **clean** | `bffi:copyrightRegistration` | owl:equivalentProperty | — |
| `bf:count` | **clean** | `bffi:count` | owl:equivalentProperty | — |
| `bf:coverArt` | **clean** | `bffi:coverArt` | owl:equivalentProperty | — |
| `bf:creationDate` | **clean** | `bffi:creationDate` | owl:equivalentProperty | — |
| `bf:credits` | *inherited* | `bffi:credits` | rdfs:subPropertyOf | — |
| `bf:custodialHistory` | **clean** | `bffi:custodialHistory` | owl:equivalentProperty | — |
| `bf:dataSource` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:date` | *inherited* | `bffi:date` | rdfs:subPropertyOf | — |
| `bf:degree` | *semantic-shift* | `bffi:degree` | bffi-meta:closeMatch | — |
| `bf:derivativeOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:derivedFrom` | **clean** | `bffi:derivedFrom` | owl:equivalentProperty | — |
| `bf:descriptionAuthentication` | **clean** | `bffi:descriptionAuthentication` | owl:equivalentProperty | — |
| `bf:descriptionConventions` | **clean** | `bffi:descriptionConventions` | owl:equivalentProperty | — |
| `bf:descriptionLanguage` | **clean** | `bffi:descriptionLanguage` | owl:equivalentProperty | — |
| `bf:descriptionLevel` | **clean** | `bffi:descriptionLevel` | owl:equivalentProperty | — |
| `bf:descriptionModifier` | **clean** | `bffi:descriptionModifier` | owl:equivalentProperty | — |
| `bf:digitalCharacteristic` | **clean** | `bffi:digitalCharacteristic` | owl:equivalentProperty | — |
| `bf:dimensions` | **clean** | `bffi:dimensions` | owl:equivalentProperty | — |
| `bf:dissertation` | **clean** | `bffi:dissertation` | owl:equivalentProperty | — |
| `bf:distributionStatement` | **clean** | `bffi:distributionStatement` | owl:equivalentProperty | — |
| `bf:dramaticRole` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (forward path: append the value to the `bffi:readMarc382` synth string if upstream begins emitting) | defensive (upstream-stability) | `drop_music_residue` |
| `bf:duration` | *semantic-shift* | `bffi:durationOfRepresentativeExpression` | bffi-meta:closeMatch | — |
| `bf:edition` | **clean** | `bffi:edition` | owl:equivalentProperty | — |
| `bf:editionEnumeration` | **clean** | `bffi:editionEnumeration` | owl:equivalentProperty | — |
| `bf:editionStatement` | **clean** | `bffi:editionStatement` | owl:equivalentProperty | — |
| `bf:electronicLocator` | **clean** | `bffi:electronicLocator` | owl:equivalentProperty | — |
| `bf:emulsion` | **clean** | `bffi:emulsion` | owl:equivalentProperty | — |
| `bf:ensemble` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:ensembleSize` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:ensembleType` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:enumerationAndChronology` | **clean** | `bffi:enumerationAndChronology` | owl:equivalentProperty | — |
| `bf:equinox` | **clean** | `bffi:equinox` | owl:equivalentProperty | — |
| `bf:eventContent` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:eventContentOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:exclusionGRing` | **clean** | `bffi:exclusionGRing` | owl:equivalentProperty | — |
| `bf:expressionOf` | **clean** | `bffi:expressionOf` | owl:equivalentProperty | — |
| `bf:extent` | **clean** | `bffi:extent` | owl:equivalentProperty | — |
| `bf:findingAid` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:findingAidOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:firstIssue` | **clean** | `bffi:firstIssue` | owl:equivalentProperty | — |
| `bf:fontSize` | **clean** | `bffi:fontSize` | owl:equivalentProperty | — |
| `bf:frequency` | **clean** | `bffi:frequency` | owl:equivalentProperty | — |
| `bf:generation` | **clean** | `bffi:generation` | owl:equivalentProperty | — |
| `bf:generationDate` | **clean** | `bffi:generationDate` | owl:equivalentProperty | — |
| `bf:generationProcess` | **clean** | `bffi:generationProcess` | owl:equivalentProperty | — |
| `bf:genreForm` | **clean** | `bffi:genreForm` | owl:equivalentProperty | — |
| `bf:geographicCoverage` | **clean** | `bffi:geographicCoverage` | owl:equivalentProperty | — |
| `bf:grantingInstitution` | *semantic-shift* | `bffi:grantingInstitution` | bffi-meta:closeMatch | — |
| `bf:hasDerivative` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:hasExpression` | *semantic-shift* | `bffi:hasExpression` | bffi-meta:closeMatch | — |
| `bf:hasInstance` | **routed** | `bffi:manifestationOfWork` (Work-axis) / `bffi:manifestationOfExpression` (Expression-axis) | discriminator: subject's/object's Expression-axis signal | `route_axis_default_predicates` |
| `bf:hasItem` | **clean** | `bffi:hasItem` | owl:equivalentProperty | — |
| `bf:hasPart` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:hasReproduction` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:hasSeries` | **routed** | `bffi:relation` → `bffi:Relation` bnode (`bffi:relationship <…/relationship/series>`) | structured-relation chain | `route_series_links` |
| `bf:hasSubseries` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:heldBy` | **clean** | `bffi:heldBy` | owl:equivalentProperty | — |
| `bf:hierarchicalLevel` | **clean** | `bffi:hierarchicalLevel` | owl:equivalentProperty | — |
| `bf:historyOfWork` | **clean** | `bffi:historyOfWork` | owl:equivalentProperty | — |
| `bf:identifiedBy` | **clean** | `bffi:identifiedBy` | owl:equivalentProperty | — |
| `bf:identifies` | **clean** | `bffi:identifies` | owl:equivalentProperty | — |
| `bf:illustrativeContent` | **clean** | `bffi:illustrativeContent` | owl:equivalentProperty | — |
| `bf:immediateAcquisition` | **clean** | `bffi:immediateAcquisition` | owl:equivalentProperty | — |
| `bf:index` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:indexOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:instanceOf` | **routed** | `bffi:workManifested` (Work-axis) / `bffi:expressionManifested` (Expression-axis) | discriminator: subject's/object's Expression-axis signal | `route_axis_default_predicates` |
| `bf:instrument` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:instrumentalType` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:intendedAudience` | **clean** | `bffi:intendedAudience` | owl:equivalentProperty | — |
| `bf:issuance` | **routed** | `bffi:issuance` (flat rename) | flat rename (no per-statement axis alternative) | `route_axis_default_predicates` |
| `bf:itemOf` | **clean** | `bffi:itemOf` | owl:equivalentProperty | — |
| `bf:itemPortion` | **clean** | `bffi:itemPortion` | owl:equivalentProperty | — |
| `bf:keyMode` | **routed** | `bffi:musicKey` literal — extracts `rdfs:label` from the `bf:KeyMode` bnode and attaches as a flat literal on the Work; bnode subgraph dropped | structured-bnode → literal collapse | `route_music_key` |
| `bf:language` | **clean** | `bffi:language` | owl:equivalentProperty | — |
| `bf:lastIssue` | **clean** | `bffi:lastIssue` | owl:equivalentProperty | — |
| `bf:layout` | **clean** | `bffi:layout` | owl:equivalentProperty | — |
| `bf:legalDate` | **clean** | `bffi:legalDate` | owl:equivalentProperty | — |
| `bf:mainTitle` | **clean** | `bffi:mainTitle` | owl:equivalentProperty | — |
| `bf:manufactureStatement` | **clean** | `bffi:manufactureStatement` | owl:equivalentProperty | — |
| `bf:material` | **clean** | `bffi:material` | owl:equivalentProperty | — |
| `bf:materialOf` | **routed** | `bffi:material` (triple-swap: ?s → ?o) | inverse-direction swap | `route_inverse_predicates` |
| `bf:media` | **clean** | `bffi:media` | owl:equivalentProperty | — |
| `bf:mediumComponent` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:mediumComponentQualifier` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:mediumOfPerformance` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:mergedToForm` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:mergerOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:mode` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (forward path: append mode value to the `bffi:musicKey` literal if upstream begins emitting) | defensive (upstream-stability) | `drop_music_mode_residue` |
| `bf:mount` | **clean** | `bffi:mount` | owl:equivalentProperty | — |
| `bf:musicFormat` | **clean** | `bffi:musicFormat` | owl:equivalentProperty | — |
| `bf:musicKey` | **clean** | `bffi:musicKey` | owl:equivalentProperty | — |
| `bf:musicMedium` | *semantic-shift* | `bffi:mediumOfChoreographicContent` | bffi-meta:closeMatch | — |
| `bf:musicOpusNumber` | **clean** | `bffi:musicOpusNumber` | owl:equivalentProperty | — |
| `bf:musicSerialNumber` | **clean** | `bffi:musicSerialNumber` | owl:equivalentProperty | — |
| `bf:musicThematicNumber` | **clean** | `bffi:musicThematicNumber` | owl:equivalentProperty | — |
| `bf:natureOfContent` | **clean** | `bffi:natureOfContent` | owl:equivalentProperty | — |
| `bf:notation` | **clean** | `bffi:notation` | owl:equivalentProperty | — |
| `bf:note` | *inherited* | `bffi:note` | rdfs:subPropertyOf | — |
| `bf:noteFor` | **routed** | `bffi:note` (triple-swap: ?note bf:noteFor ?subj → ?subj bffi:note ?note) | inverse-direction swap | `route_note_for` |
| `bf:noteType` | **drop** | no BFFI carrier — BFFI 1.0.0 doesn't model literal note categorisation; candidate for a future BFFI extension via NLF | no BFFI carrier; bounded data loss | `drop_note_type` |
| `bf:numberOfHands` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (forward path: append the value to the `bffi:readMarc382` synth string if upstream begins emitting) | defensive (upstream-stability) | `drop_music_residue` |
| `bf:originDate` | **clean** | `bffi:originDate` | owl:equivalentProperty | — |
| `bf:originPlace` | **clean** | `bffi:originPlace` | owl:equivalentProperty | — |
| `bf:originalVersion` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:originalVersionOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:outerGRing` | **clean** | `bffi:outerGRing` | owl:equivalentProperty | — |
| `bf:part` | **clean** | `bffi:part` | owl:equivalentProperty | — |
| `bf:partName` | **clean** | `bffi:partName` | owl:equivalentProperty | — |
| `bf:partNumber` | **clean** | `bffi:partNumber` | owl:equivalentProperty | — |
| `bf:partOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:pattern` | **clean** | `bffi:pattern` | owl:equivalentProperty | — |
| `bf:phonogramDate` | *inherited* | `bffi:date` | bf:subPropertyOf → rdfs:subPropertyOf | — |
| `bf:physicalLocation` | **clean** | `bffi:physicalLocation` | owl:equivalentProperty | — |
| `bf:place` | *inherited* | `bffi:place` | rdfs:subPropertyOf | — |
| `bf:polarity` | **clean** | `bffi:polarity` | owl:equivalentProperty | — |
| `bf:precededBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:preferredCitation` | **clean** | `bffi:preferredCitation` | owl:equivalentProperty | — |
| `bf:productionMethod` | **clean** | `bffi:productionMethod` | owl:equivalentProperty | — |
| `bf:productionStatement` | **clean** | `bffi:productionStatement` | owl:equivalentProperty | — |
| `bf:projection` | *inherited* | `bffi:cartographicProjection` | rdfs:subPropertyOf | — |
| `bf:projectionCharacteristic` | **clean** | `bffi:projectionCharacteristic` | owl:equivalentProperty | — |
| `bf:provisionActivity` | **clean** | `bffi:provisionActivity` | owl:equivalentProperty | — |
| `bf:provisionActivityStatement` | **routed** | `bffi:date` (76X-78X linking-entry hubs) / `bffi:Note` (otherwise) | discriminator: URI fragment | `route_provision_activity_statement` |
| `bf:pubFrequency` | **clean** | `bffi:pubFrequency` | owl:equivalentProperty | — |
| `bf:publicationStatement` | **clean** | `bffi:publicationStatement` | owl:equivalentProperty | — |
| `bf:qualifier` | **clean** | `bffi:qualifier` | owl:equivalentProperty | — |
| `bf:reductionRatio` | **clean** | `bffi:reductionRatio` | owl:equivalentProperty | — |
| `bf:referencedBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:references` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:relation` | **clean** | `bffi:relation` | owl:equivalentProperty | — |
| `bf:relationship` | **clean** | `bffi:relationship` | owl:equivalentProperty | — |
| `bf:relief` | **clean** | `bffi:relief` | owl:equivalentProperty | — |
| `bf:replacedBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:replacementOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:reproductionOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:responsibilityStatement` | **clean** | `bffi:responsibilityStatement` | owl:equivalentProperty | — |
| `bf:review` | **routed** | `bffi:relation` → `bffi:Relation` bnode (`bffi:relationship <…/relationship/review>`) | structured-relation chain | `route_relation_predicates` |
| `bf:role` | **clean** | `bffi:role` | owl:equivalentProperty | — |
| `bf:scale` | **clean** | `bffi:scale` | owl:equivalentProperty | — |
| `bf:schedulePart` | **clean** | `bffi:schedulePart` | owl:equivalentProperty | — |
| `bf:separatedFrom` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:seriesEnumeration` | **clean** | `bffi:seriesEnumeration` | owl:equivalentProperty | — |
| `bf:seriesOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:seriesStatement` | **clean** | `bffi:seriesStatement` | owl:equivalentProperty | — |
| `bf:shelfMark` | **clean** | `bffi:shelfMark` | owl:equivalentProperty | — |
| `bf:soundCharacteristic` | **clean** | `bffi:soundCharacteristic` | owl:equivalentProperty | — |
| `bf:soundContent` | **clean** | `bffi:soundContent` | owl:equivalentProperty | — |
| `bf:source` | *inherited* | `bffi:source` | rdfs:subPropertyOf | — |
| `bf:spanEnd` | **clean** | `bffi:spanEnd` | owl:equivalentProperty | — |
| `bf:splitInto` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:status` | **clean** | `bffi:status` | owl:equivalentProperty | — |
| `bf:subject` | **clean** | `bffi:subject` | owl:equivalentProperty | — |
| `bf:subjectOf` | **clean** | `bffi:subjectOf` | owl:equivalentProperty | — |
| `bf:sublocation` | **clean** | `bffi:sublocation` | owl:equivalentProperty | — |
| `bf:subseriesEnumeration` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (see forward-looking note below the Predicates table) | defensive (upstream-stability) | `drop_subseries_residue` |
| `bf:subseriesOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:subseriesStatement` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (see forward-looking note below the Predicates table) | defensive (upstream-stability) | `drop_subseries_residue` |
| `bf:subtitle` | **clean** | `bffi:subtitle` | owl:equivalentProperty | — |
| `bf:succeededBy` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:summary` | **clean** | `bffi:summary` | owl:equivalentProperty | — |
| `bf:supplement` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:supplementTo` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:supplementaryContent` | **clean** | `bffi:supplementaryContent` | owl:equivalentProperty | — |
| `bf:systemRequirement` | **clean** | `bffi:systemRequirement` | owl:equivalentProperty | — |
| `bf:table` | **clean** | `bffi:table` | owl:equivalentProperty | — |
| `bf:tableOfContents` | **clean** | `bffi:tableOfContents` | owl:equivalentProperty | — |
| `bf:tableSeq` | **clean** | `bffi:tableSeq` | owl:equivalentProperty | — |
| `bf:tempo` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (forward path: append the value to the `bffi:readMarc382` synth string if upstream begins emitting) | defensive (upstream-stability) | `drop_music_residue` |
| `bf:temporalCoverage` | **clean** | `bffi:temporalCoverage` | owl:equivalentProperty | — |
| `bf:title` | **clean** | `bffi:title` | owl:equivalentProperty | — |
| `bf:titleOf` | **clean** | `bffi:titleOf` | owl:equivalentProperty | — |
| `bf:translation` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:translationOf` | *inherited* | `bffi:relatedTo` | bf:subPropertyOf → bf:subPropertyOf → owl:equivalentProperty | — |
| `bf:unit` | **clean** | `bffi:unit` | owl:equivalentProperty | — |
| `bf:usageAndAccessPolicy` | *inherited* | `bffi:usageAndAccessPolicy` | rdfs:subPropertyOf | — |
| `bf:usesMediumOfPerformance` | **drop** | not emitted by the LoC marc2bibframe2 XSLT — defensive drop (forward path: append the value to the `bffi:readMarc382` synth string if upstream begins emitting) | defensive (upstream-stability) | `drop_music_residue` |
| `bf:validDate` | **clean** | `bffi:validDate` | owl:equivalentProperty | — |
| `bf:variantType` | **drop** | redundant with the title-variant `bffi:marcKey` discriminator | redundant signal | `drop_variant_type` |
| `bf:version` | **clean** | `bffi:version` | owl:equivalentProperty | — |
| `bf:videoCharacteristic` | **clean** | `bffi:videoCharacteristic` | owl:equivalentProperty | — |
| `bf:voice` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |
| `bf:voiceType` | **routed** | `bffi:musicMedium` → `bffi:MusicMedium` bnode with a synthesised `bffi:readMarc382` literal — labels from the BIBFRAME tree collapsed into a semicolon-separated summary | structured-tree → synth literal collapse | `route_music_medium` |

_226 terms total: 134 clean, 54 inherited, 24 routed, 9 drop, 5 semantic-shift._

<!-- END AUTO: predicates -->

### Series-link routing — `bf:hasSeries` → `bffi:relation` + `bffi:SeriesWork` / `bffi:SeriesExpression`

BFFI declares the *literal-form* (`bffi:seriesStatement`, `bffi:seriesEnumeration` — both `owl:equivalentProperty` to their `bf:*` counterparts) and the *entity classes* (`bffi:SeriesWork`, `bffi:SeriesExpression` — both `bffi-meta:broadMatch bf:Series`), but **no `bffi:hasSeries`** linking predicate. The closest BFFI-namespace alternative is the **structured general-purpose relation chain** (`bffi:relation` → `bffi:Relation` bnode with `bffi:relationship` + `bffi:associatedResource`), pointing the relationship at LoC's `vocabulary/relationship/series` URI — the same shape `bffi:relation` already covers for related-work, hub-target, analytical-entry and contained-in cases.

Why not `dct:isPartOf`? The DC Terms section above shows `dct:isPartOf` has BFFI per-axis substitutes (`bffi:expressionOf`, `bffi:workManifested`, `bffi:expressionManifested`, `bffi:itemOf`) — but all four model **FRBR-axis identity** chains (X IS-A realization/instance/copy of Y), not aggregation/membership. None fits Series-membership semantics (a Manifestation is one of many publications *in* a Series, not an *instance of* the SeriesWork). And BFFI's aggregation predicate `bffi:aggregatedBy` has range `bffi:AggregatingExpression`, which is a sibling class to `bffi:SeriesExpression`, not a parent — using it would require dual-typing the Series. The structured-relation chain avoids both issues.

Emit shape:

```turtle
<manifestation> bffi:relation [
    a bffi:Relation ;
    bffi:relationship <http://id.loc.gov/vocabulary/relationship/series> ;
    bffi:associatedResource <series-uri>
] ;
bffi:seriesStatement "Julkaisuja (Nuorisotutkimusseura). Kenttä, no. 5" .

<series-uri> a bffi:SeriesWork ;        # or bffi:SeriesExpression — axis-pick
             skos:prefLabel "Julkaisuja (Nuorisotutkimusseura). Kenttä" ;
             bffi:seriesEnumeration "vol. 5" .  # when MARC 490$v / 830$v present
```

Axis pick on the Series target follows the same rule as the other axis-split classes — `bffi:SeriesExpression` for translation-of-a-series cases (e.g. a Finnish Expression of an English series), `bffi:SeriesWork` for Work-level series identity. Recommended default: `bffi:SeriesExpression` (matches the common case where the catalogued bib is a localised Expression-in-series).

What survives without NLF input:

- **Single-namespace emit** — every predicate and class on the chain is in `bffi:*` (no `dct:*` dependency).
- **Round-trip integrity** — the LoC `vocabulary/relationship/series` URI is the same one BIBFRAME's `bf:Relation` rows already use, and `bffi:relation owl:equivalentProperty bf:relation`. MARC 490 / 800 / 810 / 830 reconstruction matches the relationship URI to the field tag.
- **Series-entity identity** — typing the target as `bffi:SeriesWork` / `bffi:SeriesExpression` preserves the FRBR axis info that BIBFRAME's flat `bf:Series` lost. BIBFRAME consumers still see a `bf:Work`-shaped target via the re-anchor chain (`bffi:SeriesWork ⊑ bffi:Work ⊑ bffi:BibframeWork ≡ bf:Work`).

What's lost:

- **The `bf:hasSeries` predicate URI itself** — consumers walking `?m bf:hasSeries ?s` need to switch to `?m bffi:relation/bffi:associatedResource ?s . ?m bffi:relation/bffi:relationship <…/relationship/series>`. Same selectivity (the relationship URI distinguishes series-membership from other `bffi:relation` uses).
- **Direct-link convenience** — Series membership now lives on a Relation bnode, not as a flat predicate on the Manifestation. Skosmos rendering needs to walk one extra hop (the Hub/Relation routing patterns already do this; same display infrastructure).

### Music-medium and music-key routing — `bf:mediumOfPerformance` / `bf:mediumComponent` / `bf:ensemble` / `bf:KeyMode` → collapse to literal

**Version context.** BFFI 1.0.0 (the BFFI ontology's `owl:versionInfo`) is based on **BIBFRAME 2.4.0**. The Library of Congress released **BIBFRAME 3.0 in December 2025**, whose headline change is that NDMSO **absorbed the Performed Music Ontology (PMO) into core BIBFRAME**, adding or refining: `bf:MediumOfPerformance`, `bf:MediumComponent`, `bf:Ensemble`, `bf:EnsembleSize`, `bf:KeyMode`, `bf:Mode`, `bf:Tempo`, `bf:DramaticRole`, `bf:MediumComponentQualifier`, `bf:OpusNumber`, `bf:SerialNumber`, `bf:ThematicCatalogNumber` (each carrying `dct:modified 2025-12-01` with ticket `GH134`). BFFI does not include BFFI-namespace equivalents for these PMO-imported classes, and won't be extended to add them — the canonical BFFI shape for music-medium and music-key data uses the existing literal-carrier vocabulary.

**Medium-of-performance routing (shipped):** collapse the BIBFRAME structured-decomposition tree (`bf:ensemble` → `bf:Ensemble` → nested `bf:mediumComponent` / `bf:mediumOfPerformance` / `bf:mediumComponentQualifier` / `bf:ensembleSize` / `bf:status`, plus bare `bf:instrument` / `bf:voice` from MARC 048) into a single literal on a `bffi:MusicMedium` bnode:

```turtle
<work>
    bffi:musicMedium [
        a bffi:MusicMedium ;
        bffi:readMarc382 "violin (solo), n=1; piano; ensemble: 2; (partial)"
    ] .
```

The synth string format:
- Each `bf:mediumComponent` renders as `<label>` plus optional ` (<qualifier>)` and ` , n=<count>`
- Multiple components joined by `; `
- Top-level `bf:ensembleSize` rendered as `; ensemble: <total>`
- Top-level `bf:status` (e.g. partial from MARC 382 ind1=1) rendered as `; (<status>)`
- Bare MARC 048 emit (one `bf:instrument` or `bf:voice` per source MARC code) produces one `bffi:musicMedium` block per source triple, each carrying just the instrument label

`bffi:readMarc382` (English label: *"read-only 382 field"*, `owl:equivalentProperty bflc:readMarc382`) is the only property the BFFI ontology declares with `bffi:MusicMedium` as its domain. **The literal we emit is a synthesised summary, not the verbatim source MARC 382** — marc2bibframe2 doesn't preserve the source field as a `bflc:marcKey` literal on the `bf:Ensemble` bnode (unlike 6XX / X30 entities), so the synth string is the best we can produce from the BIBFRAME graph alone. Round-trip to MARC 382 reconstructs from this summary; not byte-identical to the source. Documented as a known-acceptable lossiness for this routing.

**Defensive drops (PMO terms marc2bibframe2 doesn't emit):** `bf:tempo`, `bf:dramaticRole`, `bf:numberOfHands`, `bf:usesMediumOfPerformance` (predicates) and `bf:Tempo`, `bf:DramaticRole` (classes). The XSLT survey returned zero hits; the drop is insurance against a future upstream change. If upstream begins emitting these, the forward path is to append the value to the `bffi:readMarc382` synth string.

**Music-key routing (shipped):** collapse the BIBFRAME structured `bf:keyMode → bf:KeyMode` bnode into the existing `bffi:musicKey` Literal datatype property. marc2bibframe2 emits:

```turtle
<work> bf:keyMode [
    a bf:KeyMode ;
    rdfs:label "B-flat major"
] .
```

The routing extracts every `rdfs:label` from the inner KeyMode bnode, emits one `bffi:musicKey` literal per label on the parent Work (preserving language tags), then drops the entire bnode subgraph — the bnode's `rdf:type bf:KeyMode`, the `rdfs:label`, and any optional `bf:source` triples all disappear. Final shape:

```turtle
<work> a bffi:MusicWork ;
       bffi:musicKey "B-flat major" .
```

`bffi:musicKey` has `rdfs:domain bffi:MusicWork`, is a DatatypeProperty (range Literal), and `owl:equivalentProperty bf:musicKey` — corresponds to BIBFRAME's flat-literal `bf:musicKey`, not to the structured `bf:keyMode → bf:KeyMode` block. The collapse exploits this equivalence to land the structured PMO data on BFFI's pre-existing literal carrier.

**`bf:mode` / `bf:Mode` defensive drop:** the BIBFRAME 3.0.1 PMO mode-only-without-key predicate/class. marc2bibframe2's XSLT doesn't emit them (verified: zero grep hits across the XSLT tree); they're dropped defensively. If upstream changes, the forward path is to append the mode value to the `bffi:musicKey` literal that the music-key routing already produces (combining `key="B♭"` + `mode="minor"` into `"B♭ minor"`).

What survives the migration:

- **Zero new BFFI terms required.** Every term in the collapse chain (`bffi:musicMedium`, `bffi:MusicMedium`, `bffi:readMarc382`, `bffi:musicKey`) is already in the BFFI ontology.
- **`bf:MusicMedium` recovery via inference** — `bffi:MusicMedium owl:equivalentClass bf:MusicMedium`; the BIBFRAME class is directly reachable. (BIBFRAME 3.0's `bf:MusicMedium` is unchanged by the PMO absorption.)
- **Round-trip integrity** — a BFFI-to-MARC reconstruction re-emits MARC 382 verbatim from `bffi:readMarc382` (the literal IS the original MARC 382). Music-key reconstruction reads the `bffi:musicKey` literal into MARC 384.

What's lost:

- **Structured-decomposition queries** — `?w bf:musicMedium/bf:mediumComponent ?c` (and the new BIBFRAME 3.0 `bf:Ensemble` / `bf:MediumComponent` chains) don't materialise; consumers needing the components must parse the `bffi:readMarc382` literal or query BIBFRAME directly.
- **Key/Mode/Tempo separation** — `?w bf:keyMode/bf:mode`, `?w bf:tempo` — the combined `bffi:musicKey "B-flat major"` literal carries the joined key+mode form but doesn't separate them.
- **Direct PMO-class equivalence** — entities the BIBFRAME 3.0 emit types as `bf:Ensemble`, `bf:MediumOfPerformance`, `bf:MediumComponent` (added 2025-12-01) have no BFFI class to land on; they collapse into the MusicMedium block.

Sources for the BIBFRAME 3.0 release information:

- [BIBFRAME 3.0: Now with Improved Music Data — Music Library Association Cataloging and Metadata Committee](https://cmc.wp.musiclibraryassoc.org/2026/02/03/bibframe-3-0-now-with-improved-music-data/)
- [BIBFRAME — Library of Congress](https://www.loc.gov/bibframe/)
- [LC semi-annual BIBFRAME update, Feb 2026](https://www.loc.gov/bibframe/news/source/LOC%20semi-annual%20BIBFRAME%20update%20-%20Feb%202026.pdf)
- [`lcnetdev/bibframe-ontology` repository](https://github.com/lcnetdev/bibframe-ontology)

This is the same pattern as the Identifier-scheme and Title-variant collapses elsewhere in this doc: BFFI consistently chooses **one canonical class + one literal-carrier property** over BIBFRAME's structured subclass tree.

## Axis-default class routings

Eight BIBFRAME classes have `bffi-meta:broadMatch` (or for `bf:MusicAudio`, `closeMatch`) mappings to *both* a Work-axis and an Expression-axis BFFI counterpart. The routing picks **per subject** based on the subject's co-typed `rdf:type` assertions:

- **Work-axis pick** when the subject carries any of `bffi:BibframeWork`, `bffi:Work`, `bffi:AggregatingWork`, or `bffi:Arrangement` as another `rdf:type`. This is the Work URI marc2bibframe2 emitted (typed `bf:Work` upstream, renamed to `bffi:BibframeWork` by the clean-rename pass) or a Hub URI that the Hub routing already retyped to `bffi:Work` / `bffi:Arrangement`.
- **Expression-axis pick** otherwise — Instance URIs (marc2bibframe2 echoes the content-type class on the Instance side but doesn't co-type it `bf:Work`) and any subject without a clear axis signal. Matches Helmet's "one localised Expression per record" pattern.

| `bf:*` class | Work-axis pick | Expression-axis pick |
|---|---|---|
| `bf:Monograph` | `bffi:MonographWork` | `bffi:MonographExpression` |
| `bf:Series` | `bffi:SeriesWork` | `bffi:SeriesExpression` |
| `bf:Serial` | `bffi:SerialWork` | `bffi:SerialExpression` |
| `bf:MusicAudio` | `bffi:MusicWork` *(closeMatch; asymmetric — there is no `bffi:MusicAudioWork`)* | `bffi:MusicAudioExpression` |
| `bf:MovingImage` | `bffi:MovingImageWork` | `bffi:MovingImageExpression` |
| `bf:Cartography` | `bffi:CartographyWork` | `bffi:CartographyExpression` |
| `bf:NonMusicAudio` | `bffi:NonMusicAudioWork` | `bffi:NonMusicAudioExpression` |
| `bf:Audio` *(marc2bibframe2 emits this only for non-music audio)* | `bffi:NonMusicAudioWork` | `bffi:NonMusicAudioExpression` |

The counter dict the routing returns is split into `axis_default_class_work` and `axis_default_class_expression` so the observability summary surfaces the discriminator's effect per run. In the 20 k bench the split was ~50/50 — marc2bibframe2 echoes each content-type class on both the Work URI and the Instance URI, and the discriminator catches both correctly.

## Axis-default predicate routings

`bf:instanceOf` and `bf:hasInstance` are per-statement-discriminated (the routing inspects the axis-signal side's `rdf:type` to pick Work vs Expression). `bf:issuance` is a flat rename — its BFFI peer `bffi:extensionPlan` looks like an "alternative" only on first glance; deeper inspection (below) shows it's a separate concept entirely:

| `bf:*` predicate | Routing | Discriminator side | Work-axis pick | Expression-axis pick |
|---|---|---|---|---|
| `bf:instanceOf` | per-statement | object's `rdf:type` | `bffi:workManifested` | `bffi:expressionManifested` |
| `bf:hasInstance` | per-statement | subject's `rdf:type` | `bffi:manifestationOfWork` | `bffi:manifestationOfExpression` |
| `bf:issuance` | flat rename | — | `bffi:issuance` | `bffi:issuance` |

**Why the per-statement discriminator works.** `bf:instanceOf` is "Manifestation → Work/Expression" — its object is the entity being realized. If that object is typed `bffi:Expression` (or any descendant — `bffi:SeriesExpression`, `bffi:MonographExpression`, …), the statement is realizing-an-Expression and lands on `bffi:expressionManifested`. Same logic inverted for `bf:hasInstance`: the SUBJECT is the entity-having-instances, so its type drives the pick.

**Why `bf:issuance` is a flat rename, not discriminated.** The BFFI ontology declares two `bffi:*` terms with `bffi-meta:broadMatch bf:issuance` — `bffi:issuance` and `bffi:extensionPlan`. Surface-level that reads as "alternative renames," but the deeper picture says they're two **separate concepts** that both happen to be loosely related to BIBFRAME's issuance area:

| BFFI term | Domain | Range | `bffi-meta:relatedValueVocabulary` | RDA term list |
|---|---|---|---|---|
| `bffi:issuance` | `bffi:Manifestation` | `bffi:Issuance` | `…au:mts:m4372` | RDA `ModeIssue` — *Single unit, Serial, Multipart monograph, Integrating resource* |
| `bffi:extensionPlan` | `bffi:Work` | `bffi:ExtensionPlan` | `…au:mts:m5119` | RDA `RDAExtensionPlan` — *Unknown, Will not be extended, Has no plan to be extended, …* |

`bffi:extensionPlan` describes a Work's projected expansion behaviour (an editorial/curatorial plan); `bffi:issuance` describes the issuance pattern at the Manifestation level. They link to different RDA term lists with disjoint value vocabularies. They are not interchangeable on a single triple.

What this means for the per-statement signal: the object URI of `bf:issuance` (`<…/issuance/serl>`, `<…/issuance/mono>`, `<…/issuance/intg>`, `<…/issuance/mulu>`) IS a meaningful per-statement signal — but it discriminates between *codes inside `bffi:issuance`*, not between `bffi:issuance` and `bffi:extensionPlan`. All four codes are valid RDA `ModeIssue` values, so they all map cleanly to `bffi:issuance` without further routing. Routing the `<serl>`/`<intg>` ones to `bffi:extensionPlan` would mis-type the object — those URIs aren't RDA `RDAExtensionPlan` values.

In the corpus (200-record sample): 197 `<…/mono>` + 3 `<…/serl>`, all on Manifestation subjects (`bf:Instance`). The flat rename to `bffi:issuance` covers every observed case correctly.

**Forward-looking note (not implemented).** If the converter should *also* emit a `bffi:extensionPlan` triple on the Work side for serial / integrating resources — a synthesised addition rather than a routing alternative — that's a separate, additive feature. It would mint an `ExtensionPlan` instance (URI policy TBD), attach it to the Work via `bffi:extensionPlan`, and leave the existing `bffi:issuance` Manifestation triple untouched. Surface as a plan if Helmet's downstream consumers ask for the Work-level metadata.

**Observability.** The routing returns a counter dict split per predicate-and-axis: `instance_of_work`, `instance_of_expression`, `has_instance_of_work`, `has_instance_of_expression`, `issuance`. The next 20 k bench surfaces the per-axis distribution per run.

## URI-fragment discriminator routing — `bf:provisionActivityStatement`

`bf:provisionActivityStatement` is declared in BIBFRAME 3.0.1 as a `DatatypeProperty` on `bf:Instance` with range `Literal` — labelled "Provider statement" in the ontology, conceptually a generic free-text statement covering any provision-activity flavour (publication / production / manufacture / distribution / copyright). The BFFI ontology has no `bffi:*` equivalent.

In the Helmet corpus, every observed instance (102 in the 20 k bench) is attached to a related-Instance hub from a MARC 76X-78X linking-entry field (780 preceding entry, 785 succeeding entry, 760 main series, 765 original language, 770 supplement, 773 host item, 775 other edition, 776 additional physical form, 777 issued with, 787 other relationship, …) and carries a **date range** (`"1980-1981"`, `"1909-1993"`, `"2003-"`, …) — not a publisher statement at all. The MARC tag is encoded in the Instance URI's fragment: `<…#Instance780-25>`.

The routing reads the URI fragment as a structural discriminator (same shape Hub routing uses, just on URI content instead of `bflc:marcKey` content):

- **Fragment matches `Instance(76\d|77\d|78\d)-*`** → rewrite to `bffi:date` as a plain string literal (no EDTF datatype claim — content isn't always EDTF-conformant, e.g. `"(1990-2013), ISSN"`).
- **Fragment doesn't match** → wrap the literal in a `bffi:Note` bnode: `?inst bffi:note [a bffi:Note ; rdfs:label "text"]`. Generic carrier; preserves the text without asserting a semantic interpretation.

Two observability counters split the destination (`provision_statement_to_date` vs `provision_statement_to_note`) so the discriminator decision is visible per run.

## Catch-all relation predicates via `bffi:relation`

BIBFRAME predicates with no direct `bffi:*` counterpart but a natural fit for the structured `bffi:relation → bffi:Relation` chain documented in Series-link routing above — same shape, different LoC `vocabulary/relationship/<term>` URI on the Relation bnode:

| `bf:*` predicate | LoC relationship URI |
|---|---|
| `bf:hasSeries` | `<http://id.loc.gov/vocabulary/relationship/series>` |
| `bf:accompaniedBy` | `<http://id.loc.gov/vocabulary/relationship/accompaniedby>` |
| `bf:review` | `<http://id.loc.gov/vocabulary/relationship/review>` |

`bf:hasSeries` has its own dedicated routing function (separate counter for observability visibility); the others extend the catch-all map.

## Inverse-direction triple-swap routings

Five BIBFRAME inverse predicates have forward-direction `bffi:*` equivalents already declared in the BFFI ontology (with `owl:equivalentProperty` to their `bf:*` counterparts). The routing flips each triple's direction and renames the predicate to the forward form:

| Inverse `bf:*` | Forward `bffi:*` | Source triple | Routed triple |
|---|---|---|---|
| `bf:agentOf` | `bffi:agent` | `?agent bf:agentOf ?contribution` | `?contribution bffi:agent ?agent` |
| `bf:contributionOf` | `bffi:contribution` | `?contrib bf:contributionOf ?work` | `?work bffi:contribution ?contrib` |
| `bf:materialOf` | `bffi:material` | `?material bf:materialOf ?manifestation` | `?manifestation bffi:material ?material` |
| `bf:appliedMaterialOf` | `bffi:appliedMaterial` | `?material bf:appliedMaterialOf ?m` | `?m bffi:appliedMaterial ?material` |
| `bf:baseMaterialOf` | `bffi:baseMaterial` | `?material bf:baseMaterialOf ?m` | `?m bffi:baseMaterial ?material` |

Zero corpus prevalence in the 20 k bench — these are insurance routings against future records that might use the inverse direction. `bf:noteFor` follows the same pattern with its own dedicated routing (kept separate because its semantic — anchoring a Note to its subject — is note-specific rather than a generic inverse-relation pattern):

- `?note bf:noteFor ?subject` → `?subject bffi:note ?note`

## Drops — no BFFI carrier, redundant signal, or defensive

Four predicates are dropped instead of routed:

- **`bf:variantType`** (`?title bf:variantType "parallel"`) — redundant. The title-variant routing's `bffi:marcKey` discriminator already encodes the variant type via the first-3-char MARC tag (`246` parallel, `740` analytical added, etc.). Dropping it avoids closed-namespace residue without information loss.
- **`bf:noteType`** (`?note bf:noteType "Summary"`) — no BFFI carrier. BFFI 1.0.0 deliberately didn't model literal note categorisation; `bffi:Note` has zero predicates declared with it as domain and only `bffi:TitleNote` as a subclass. Reaching for a foreign vocabulary (`dct:type`, `skos:notation`) would contradict the "DC Terms → BFFI alternatives" pattern below, which expects BFFI-native carriers wherever possible. Candidate for a future BFFI extension via NLF conversation. Bounded loss: the note's text content (`rdfs:label`) typically encodes the categorisation implicitly ("Bibliography: …", "Summary: …").
- **`bf:subseriesStatement`** + **`bf:subseriesEnumeration`** — defensive. BIBFRAME 3.0.1 declares both predicates, but the LoC marc2bibframe2 XSLT (our actual upstream) never emits them — subseries information in MARC 490 / 8XX gets folded into ordinary `bf:Series` + `bf:seriesEnumeration` shapes instead. Verified by `grep -rn 'subseries…'` returning zero hits across the XSLT tree, and corroborated by zero occurrences in a 500-file Helmet corpus sample. The drop covers the case where a future upstream change surfaces these — see the forward-looking note below for the marcKey-based pairing strategy that should replace the drop at that point.

### Forward-looking note — pairing subseries with parent series via `bflc:marcKey`

If a future BIBFRAME upstream begins emitting `bf:subseriesStatement` / `bf:subseriesEnumeration`, the proper handling is to attach the subseries content to the **right parent Series entity**, not just to copy it as a literal. The signal that makes the pairing tractable is the `bflc:marcKey` literal already attached to each Series entity by marc2bibframe2 — its first 3 characters identify the source MARC field tag (`490` transcribed series, `800` series-author Hub, `810` series-corporate Hub, `811` series-meeting Hub, `830` series uniform-title Hub).

Concretely, the routing would:

1. For each Manifestation with multiple `bffi:relation → bffi:SeriesWork` chains (after the existing Series-link routing has minted them), inspect each SeriesWork's `bffi:marcKey` literal.
2. For each `bf:subseriesStatement` / `bf:subseriesEnumeration` triple on the Manifestation, find the SeriesWork whose marcKey tag matches the subseries data's originating field. (For the simple single-series case this is trivial; for multi-series cases the marcKey tag disambiguates.)
3. Attach the subseries content to the chosen SeriesWork via `bffi:partName` / `bffi:partNumber` (Option 3 from the design discussion — these existing BFFI predicates carry the "part name + number" semantic cleanly when attached to a SeriesWork entity).

Until upstream changes, the drop ensures closed-namespace cleanliness. The counter (`subseries_dropped`) surfaces any drop in the observability summary — a non-zero value indicates upstream has begun emitting these and the marcKey-pairing routing should be implemented.

## Defensive guard — undeclared `bf:*` terms are dropped

After every clean rename + discriminator routing has run, any `bf:*` URI remaining in the output graph is checked against the BIBFRAME ontology. If neither BIBFRAME's classes, object properties, nor datatype properties declare the term, the whole triple is removed and the drop count is reported as `dropped_undeclared_bf`.

The pattern uses the BIBFRAME ontology as the authority for "is this a real `bf:*` term?" rather than maintaining a hand-curated allowlist. Concrete catches from the 20 k bench:

- **`bf:Statement`** (4 occurrences) — marc2bibframe2 emits a freestanding `<bf:Statement>text</bf:Statement>` element with the publisher-statement transcribed text. The string content is identical to the sibling structured `bf:ProvisionActivity` block (`bflc:simplePlace` + `bflc:simpleAgent` + `bflc:simpleDate`) — dropping it loses no information. `bf:Statement` is **not declared** in BIBFRAME 3.0.1 (zero triples reference it), so the guard correctly identifies it as an upstream emit artifact and removes it.

Drop counters surface in the observability `end` event so the operator can monitor the artifact rate per run. If BIBFRAME later adds a term that marc2bibframe2 already emits, the next BIBFRAME refresh picks it up automatically and the drop count for that term falls to zero.

## DC Terms → BFFI alternatives

Definitive mapping from DC Terms predicates to BFFI counterparts. The BFFI ontology does not declare formal `owl:equivalentProperty` / `rdfs:subPropertyOf` / `bffi-meta:*Match` links to the `dct:*` namespace (verified by rdflib scan of every BFFI predicate's link set). The mapping below is therefore by **semantic correspondence** — each BFFI term carries an `owl:equivalentProperty` to the BIBFRAME predicate of comparable meaning, and the BIBFRAME predicate has the same role as the DC Terms predicate.

| `dct:*` term | BFFI alternative | Shape | BFFI ↔ BIBFRAME link |
|---|---|---|---|
| `dct:date` | `bffi:date` (parent) · `bffi:copyrightDate` · `bffi:changeDate` · `bffi:provisionActivityDate` (children) | Literal datatype | `bffi:*Date rdfs:subPropertyOf bffi:date`; each child has `owl:equivalentProperty bf:*Date` |
| `dct:identifier` | `bffi:identifiedBy` → `bffi:Identifier` (range) → `rdf:value` (literal payload) | ObjectProperty + structured bnode | `bffi:identifiedBy owl:equivalentProperty bf:identifiedBy` |
| `dct:isPartOf` | **per-axis** — `bffi:expressionOf` (Expression→Work) · `bffi:workManifested` (Manifestation→Work) · `bffi:expressionManifested` (Manifestation→Expression) · `bffi:itemOf` (Item→Manifestation) | ObjectProperty | each `owl:equivalentProperty bf:expressionOf` / `bf:workManifested` / `bf:expressionManifested` / `bf:itemOf`; multiple RDA exactMatches |
| `dct:modified` | `bffi:changeDate` (domain `bffi:AdminMetadata`) | Literal datatype on AdminMetadata bnode | `bffi:changeDate owl:equivalentProperty bf:changeDate`; `rdfs:subPropertyOf bffi:date` |
| `dct:publisher` | `bffi:publicationStatement` (literal) · `bffi:Publication` (class, `rdfs:subClassOf bffi:ProvisionActivity`) | Literal datatype · or class for structured bnode | `bffi:publicationStatement owl:equivalentProperty bf:publicationStatement`; `bffi:Publication owl:equivalentClass bf:Publication` |
| `dct:relation` | `bffi:relation` → `bffi:Relation` (range) | ObjectProperty + structured bnode (with `bffi:relationship` + `bffi:associatedResource`) | `bffi:relation owl:equivalentProperty bf:relation` |
| `dct:spatial` | `bffi:place` (parent) · `bffi:originPlace` (Work creation) · `bffi:geographicCoverage` (Work subject area) · `bffi:locationOfCollection` (CollectionManifestation) | ObjectProperty → `bffi:Place` | parent `bffi:place rdfs:subPropertyOf bf:place`; children with `owl:equivalentProperty bf:*` + RDA exactMatch |
| `dct:subject` | `bffi:subject` | ObjectProperty | `bffi:subject owl:equivalentProperty bf:subject`; RDA `w/P10256` exactMatch |
| `dct:temporal` | `bffi:temporalCoverage` | ObjectProperty → `bffi:Temporal` | `bffi:temporalCoverage owl:equivalentProperty bf:temporalCoverage`; RDA `w/P10322` exactMatch |
| `dct:title` | `bffi:title` (ObjectProperty → `bffi:Title`) · `bffi:mainTitle` (Literal datatype on `bffi:Title`) | ObjectProperty · or Literal datatype | `bffi:title owl:equivalentProperty bf:title`; `bffi:mainTitle owl:equivalentProperty bf:mainTitle` |

## Re-anchor clusters (tree view)

One tree per `bffi:Anchor owl:equivalentClass bf:X` that has at least one BFFI subclass. Each tree shows every `bffi:Sub rdfs:subClassOf` descendant (transitive closure) with its own `bf:*` equivalent when available. Emitting any node in a tree makes every BIBFRAME ancestor satisfied by inference.

The trees enumerate the complete BFFI re-anchor structure as declared in the BFFI ontology — including classes a typical MARC-to-BFFI conversion does not encounter — so an NLF reviewer can verify the structure independently of conversion-scoped tables above.

Marker legend:

| Marker | Meaning |
|---|---|
| ✅ | `bffi:Sub owl:equivalentClass bf:*` is declared |
| 🆕 | BFFI-native — no `bf:*` counterpart in the BFFI ontology |
| ⤴ | `bffi-meta:broadMatch` / `closeMatch` only (no clean alias) |

### Standalone anchors (no BFFI subclasses, no BFFI parents)

Fifty-six BFFI classes are `owl:equivalentClass bf:X` directly with no further BFFI hierarchy above or below them. Listed here alphabetically for completeness:

- `bffi:AcquisitionSource` ✅ ≡ `bf:AcquisitionSource`
- `bffi:AdminMetadata` ✅ ≡ `bf:AdminMetadata`
- `bffi:AspectRatio` ✅ ≡ `bf:AspectRatio`
- `bffi:Binding` ✅ ≡ `bf:Binding`
- `bffi:BookFormat` ✅ ≡ `bf:BookFormat`
- `bffi:Capture` ✅ ≡ `bf:Capture`
- `bffi:Carrier` ✅ ≡ `bf:Carrier`
- `bffi:Cartographic` ✅ ≡ `bf:Cartographic`
- `bffi:CollectionArrangement` ✅ ≡ `bf:CollectionArrangement`
- `bffi:ColorContent` ✅ ≡ `bf:ColorContent`
- `bffi:Content` ✅ ≡ `bf:Content`
- `bffi:ContentAccessibility` ✅ ≡ `bf:ContentAccessibility`
- `bffi:CopyrightRegistration` ✅ ≡ `bf:CopyrightRegistration`
- `bffi:CoverArt` ✅ ≡ `bf:CoverArt`
- `bffi:DescriptionAuthentication` ✅ ≡ `bf:DescriptionAuthentication`
- `bffi:DescriptionConventions` ✅ ≡ `bf:DescriptionConventions`
- `bffi:DescriptionLevel` ✅ ≡ `bf:DescriptionLevel`
- `bffi:Dissertation` ✅ ≡ `bf:Dissertation`
- `bffi:Emulsion` ✅ ≡ `bf:Emulsion`
- `bffi:Event` ✅ ≡ `bf:Event`
- `bffi:Extent` ✅ ≡ `bf:Extent`
- `bffi:FontSize` ✅ ≡ `bf:FontSize`
- `bffi:Frequency` ✅ ≡ `bf:Frequency`
- `bffi:Generation` ✅ ≡ `bf:Generation`
- `bffi:GenerationProcess` ✅ ≡ `bf:GenerationProcess`
- `bffi:GenreForm` ✅ ≡ `bf:GenreForm`
- `bffi:GeographicCoverage` ✅ ≡ `bf:GeographicCoverage`
- `bffi:Illustration` ✅ ≡ `bf:Illustration`
- `bffi:ImmediateAcquisition` ✅ ≡ `bf:ImmediateAcquisition`
- `bffi:IntendedAudience` ✅ ≡ `bf:IntendedAudience`
- `bffi:Language` ✅ ≡ `bf:Language`
- `bffi:Layout` ✅ ≡ `bf:Layout`
- `bffi:Media` ✅ ≡ `bf:Media`
- `bffi:Mount` ✅ ≡ `bf:Mount`
- `bffi:MusicFormat` ✅ ≡ `bf:MusicFormat`
- `bffi:MusicMedium` ✅ ≡ `bf:MusicMedium`
- `bffi:Place` ✅ ≡ `bf:Place`
- `bffi:Polarity` ✅ ≡ `bf:Polarity`
- `bffi:ProductionMethod` ✅ ≡ `bf:ProductionMethod`
- `bffi:Projection` ✅ ≡ `bf:Projection`
- `bffi:PubFrequency` ✅ ≡ `bf:PubFrequency`
- `bffi:ReductionRatio` ✅ ≡ `bf:ReductionRatio`
- `bffi:Relation` ✅ ≡ `bf:Relation`
- `bffi:Relationship` ✅ ≡ `bf:Relationship`
- `bffi:Relief` ✅ ≡ `bf:Relief`
- `bffi:Role` ✅ ≡ `bf:Role`
- `bffi:SoundContent` ✅ ≡ `bf:SoundContent`
- `bffi:Status` ✅ ≡ `bf:Status`
- `bffi:Sublocation` ✅ ≡ `bf:Sublocation`
- `bffi:Summary` ✅ ≡ `bf:Summary`
- `bffi:SupplementaryContent` ✅ ≡ `bf:SupplementaryContent`
- `bffi:TableOfContents` ✅ ≡ `bf:TableOfContents`
- `bffi:Temporal` ✅ ≡ `bf:Temporal`
- `bffi:Title` ✅ ≡ `bf:Title` (`bffi:TitleNote` exists but is `rdfs:subClassOf bffi:Note`, not `bffi:Title`; `bffi:TitleSource` is `rdfs:subClassOf bffi:RecordingSource`)
- `bffi:Topic` ✅ ≡ `bf:Topic`
- `bffi:Unit` ✅ ≡ `bf:Unit`

### `bffi:AccessPolicy` ≡ `bf:AccessPolicy`

  - `bffi:AgeLimit` ⤴ bf:AccessPolicy (broadMatch)

### `bffi:Agent` ≡ `bf:Agent`

  - `bffi:Family` ✅ ≡ `bf:Family`
  - `bffi:Jurisdiction` ✅ ≡ `bf:Jurisdiction`
  - `bffi:Meeting` ✅ ≡ `bf:Meeting`
  - `bffi:MetadataLicensor` 🆕 *BFFI-native*
  - `bffi:Organization` ✅ ≡ `bf:Organization`
  - `bffi:Person` ✅ ≡ `bf:Person`

### `bffi:BibframeWork` ≡ `bf:Work`

  - `bffi:Expression` ⤴ bf:Work (broadMatch)
    - `bffi:AggregatingExpression` ⤴ bf:Work (broadMatch)
    - `bffi:Arrangement` ✅ ≡ `bf:Arrangement`
    - `bffi:CartographyExpression` ⤴ bf:Cartography (broadMatch)
    - `bffi:CollectionExpression` ⤴ bf:Collection (broadMatch)
    - `bffi:Dataset` ✅ ≡ `bf:Dataset`
    - `bffi:MixedMaterial` ✅ ≡ `bf:MixedMaterial`
      - `bffi:Kit` ✅ ≡ `bf:Kit`
    - `bffi:MonographExpression` ⤴ bf:Monograph (broadMatch)
    - `bffi:MovingImageExpression` ⤴ bf:MovingImage (broadMatch)
    - `bffi:Multimedia` ✅ ≡ `bf:Multimedia`
    - `bffi:MusicAudioExpression` ⤴ bf:MusicAudio (broadMatch)
    - `bffi:NonMusicAudioExpression` ⤴ bf:NonMusicAudio (broadMatch)
    - `bffi:NotatedMovement` ✅ ≡ `bf:NotatedMovement`
    - `bffi:NotatedMusic` ✅ ≡ `bf:NotatedMusic`
    - `bffi:Object` ✅ ≡ `bf:Object`
    - `bffi:SerialExpression` ⤴ bf:Serial (broadMatch)
    - `bffi:SeriesExpression` ⤴ bf:Series (broadMatch)
    - `bffi:StillImage` ✅ ≡ `bf:StillImage`
    - `bffi:Text` ✅ ≡ `bf:Text`
  - `bffi:Work` ⤴ bf:Work (broadMatch)
    - `bffi:AggregatingWork` ⤴ bf:Work (broadMatch)
    - `bffi:CartographyWork` ⤴ bf:Cartography (broadMatch)
    - `bffi:CollectionWork` ⤴ bf:Collection (broadMatch)
    - `bffi:Integrating` ✅ ≡ `bf:Integrating`
    - `bffi:Manuscript` ✅ ≡ `bf:Manuscript`
    - `bffi:MonographWork` ⤴ bf:Monograph (broadMatch)
    - `bffi:MovingImageWork` ⤴ bf:MovingImage (broadMatch)
    - `bffi:MusicWork` ⤴ bf:NotatedMusic (closeMatch) / bf:MusicAudio (closeMatch)
    - `bffi:NonMusicAudioWork` ⤴ bf:NonMusicAudio (broadMatch)
    - `bffi:SerialWork` ⤴ bf:Serial (broadMatch)
    - `bffi:SeriesWork` ⤴ bf:Series (broadMatch)

### `bffi:Classification` ≡ `bf:Classification`

  - `bffi:ClassificationDdc` ✅ ≡ `bf:ClassificationDdc`
  - `bffi:ClassificationLcc` ✅ ≡ `bf:ClassificationLcc`
  - `bffi:ClassificationNal` ✅ ≡ `bf:ClassificationNal`
  - `bffi:ClassificationNlm` ✅ ≡ `bf:ClassificationNlm`
  - `bffi:ClassificationUdc` ✅ ≡ `bf:ClassificationUdc`

### `bffi:Contribution` ≡ `bf:Contribution`

  - `bffi:PrimaryContribution` ✅ ≡ `bf:PrimaryContribution`

### `bffi:DigitalCharacteristic` ≡ `bf:DigitalCharacteristic`

  - `bffi:CartographicDataType` ✅ ≡ `bf:CartographicDataType`
  - `bffi:CartographicObjectType` ✅ ≡ `bf:CartographicObjectType`
  - `bffi:EncodedBitrate` ✅ ≡ `bf:EncodedBitrate`
  - `bffi:EncodingFormat` ✅ ≡ `bf:EncodingFormat`
  - `bffi:FileSize` ✅ ≡ `bf:FileSize`
  - `bffi:FileType` ✅ ≡ `bf:FileType`
  - `bffi:ObjectCount` ✅ ≡ `bf:ObjectCount`
  - `bffi:RegionalEncoding` ✅ ≡ `bf:RegionalEncoding`
  - `bffi:Resolution` ✅ ≡ `bf:Resolution`

### `bffi:EnumerationAndChronology` ≡ `bf:EnumerationAndChronology`

  - `bffi:Chronology` ✅ ≡ `bf:Chronology`
  - `bffi:Enumeration` ✅ ≡ `bf:Enumeration`

### `bffi:Identifier` ≡ `bf:Identifier`

  - `bffi:Local` ✅ ≡ `bf:Local`
  - `bffi:ShelfMark` ✅ ≡ `bf:ShelfMark`

### `bffi:Item` ≡ `bf:Item`

  - `bffi:CollectionItem` ⤴ bf:Item (broadMatch)

### `bffi:Manifestation` ≡ `bf:Instance`

  - `bffi:Archival` ✅ ≡ `bf:Archival`
  - `bffi:CollectionManifestation` ⤴ bf:Instance (broadMatch)
  - `bffi:Electronic` ✅ ≡ `bf:Electronic`
  - `bffi:Microform` ✅ ≡ `bf:Microform`
  - `bffi:Print` ✅ ≡ `bf:Print`
  - `bffi:Tactile` ✅ ≡ `bf:Tactile`

### `bffi:Material` ≡ `bf:Material`

  - `bffi:AppliedMaterial` ✅ ≡ `bf:AppliedMaterial`
  - `bffi:BaseMaterial` ✅ ≡ `bf:BaseMaterial`

### `bffi:MixedMaterial` ≡ `bf:MixedMaterial`

  - `bffi:Kit` ✅ ≡ `bf:Kit`

### `bffi:Notation` ≡ `bf:Notation`

  - `bffi:MovementNotation` ✅ ≡ `bf:MovementNotation`
  - `bffi:MusicNotation` ✅ ≡ `bf:MusicNotation`
  - `bffi:Script` ✅ ≡ `bf:Script`
  - `bffi:TactileNotation` ✅ ≡ `bf:TactileNotation`

### `bffi:Note` ≡ `bf:Note`

  - `bffi:TitleNote` ⤴ bf:Note (broadMatch)

### `bffi:ProjectionCharacteristic` ≡ `bf:ProjectionCharacteristic`

  - `bffi:PresentationFormat` ✅ ≡ `bf:PresentationFormat`
  - `bffi:ProjectionSpeed` ✅ ≡ `bf:ProjectionSpeed`

### `bffi:ProvisionActivity` ≡ `bf:ProvisionActivity`

  - `bffi:Distribution` ✅ ≡ `bf:Distribution`
  - `bffi:Manufacture` ✅ ≡ `bf:Manufacture`
  - `bffi:Modification` ✅ ≡ `bf:Modification`
  - `bffi:Production` ✅ ≡ `bf:Production`
  - `bffi:Publication` ✅ ≡ `bf:Publication`

### `bffi:Scale` ≡ `bf:Scale`

  - `bffi:ScaleDesignation` ⤴ bf:Scale (broadMatch)

### `bffi:SoundCharacteristic` ≡ `bf:SoundCharacteristic`

  - `bffi:CaptureStorage` ✅ ≡ `bf:CaptureStorage`
  - `bffi:GrooveCharacteristic` 🆕 *BFFI-native*
  - `bffi:GrooveCutting` 🆕 *BFFI-native*
  - `bffi:PlaybackChannels` ✅ ≡ `bf:PlaybackChannels`
  - `bffi:PlaybackCharacteristic` ✅ ≡ `bf:PlaybackCharacteristic`
  - `bffi:PlayingSpeed` ✅ ≡ `bf:PlayingSpeed`
  - `bffi:RecordingMedium` ✅ ≡ `bf:RecordingMedium`
  - `bffi:RecordingMethod` ✅ ≡ `bf:RecordingMethod`
  - `bffi:TapeConfig` ✅ ≡ `bf:TapeConfig`
  - `bffi:TrackConfig` ✅ ≡ `bf:TrackConfig`

### `bffi:Source` ≡ `bf:Source`

  - `bffi:RecordingSource` ⤴ bf:Source (broadMatch)
    - `bffi:TitleSource` ⤴ bf:Source (broadMatch)

### `bffi:SystemRequirement` ≡ `bf:SystemRequirement`

  - `bffi:MachineModel` 🆕 *BFFI-native*
  - `bffi:OperatingSystem` 🆕 *BFFI-native*
  - `bffi:ProgrammingLanguage` 🆕 *BFFI-native*

### `bffi:UsageAndAccessPolicy` ≡ `bf:UsageAndAccessPolicy`

  - `bffi:AccessPolicy` ✅ ≡ `bf:AccessPolicy`
    - `bffi:AgeLimit` ⤴ bf:AccessPolicy (broadMatch)
  - `bffi:RetentionPolicy` ✅ ≡ `bf:RetentionPolicy`
  - `bffi:UsePolicy` ✅ ≡ `bf:UsePolicy`

### `bffi:VideoCharacteristic` ≡ `bf:VideoCharacteristic`

  - `bffi:BroadcastStandard` ✅ ≡ `bf:BroadcastStandard`
  - `bffi:VideoFormat` ✅ ≡ `bf:VideoFormat`

## Gap clusters — context the auto-table can't carry

The Classes / Predicates tables above cover every BIBFRAME-declared `bf:*` term. The status flags (`clean`, `routed`, `inherited`, `semantic-shift`, `GAP`) and `Handler` column give per-row detail; the notes below add cluster-level context the table can't carry — *why* the GAP rows are gaps, and which deferred-routing candidates the project is aware of.

### PMO music — BFFI 1.1.0 candidate

BIBFRAME 3.0.1 (December 2025) absorbed the Performed Music Ontology (PMO), introducing `bf:KeyMode`, `bf:Ensemble`, `bf:MediumOfPerformance`, `bf:DramaticRole`, `bf:Tempo`, etc. and their predicate counterparts. BFFI 1.0.0 is based on BIBFRAME 2.4.0 and predates the absorption, so all PMO terms show as `GAP` in the auto-table. The interim routing collapses what marc2bibframe2 emits to literal carriers — `bffi:readMarc382` (Medium-of-Performance) and `bffi:musicKey` (KeyMode) — see the [Music-medium and music-key routing](#music-medium-and-music-key-routing--bfmediumofperformance--bfmediumcomponent--bfensemble--bfkeymode--collapse-to-literal) callout above. A future BFFI 1.1.0 is expected to land BFFI-namespace equivalents on the existing re-anchor pattern.

### Inverse predicates — deferred (zero corpus prevalence)

`bf:agentOf`, `bf:appliedMaterialOf`, `bf:baseMaterialOf`, `bf:contributionOf`, `bf:materialOf` — BFFI maps the forward direction but doesn't declare the inverses; BIBFRAME doesn't declare `owl:inverseOf` triples for them either. Zero corpus prevalence in the 20 k bench, so deferred. If a record carrying one surfaces, options are: declare the inverse under `bffi:` (NLF input) or route via `bffi:relation` to a LoC relationship URI.

### `bf:noteType` literal categorisation — dropped, no BFFI carrier

`bf:noteType` is a `DatatypeProperty` (range `rdfs:Literal`) that BIBFRAME uses to categorise a `bf:Note` block with a vocabulary label ("Summary", "Bibliography", "Performer", "Language", etc.). BFFI 1.0.0 deliberately doesn't model literal note typing: `bffi:Note` has zero predicates with it as domain, and the only subclass is `bffi:TitleNote`. Reaching for `dct:type` / `skos:notation` would violate the "DC Terms → BFFI alternatives" pattern below. The routing is implemented as a drop (see `drop_note_type` in the routings module; the `note_type_dropped` counter is emitted at run-end). The note's text content in `rdfs:label` usually carries the categorisation implicitly ("Bibliography: …", "Summary: …"). A future `bffi:noteType` predicate (or a richer `bffi:Note` subclass hierarchy) is the natural NLF-extension fix.

### Country labels — LoC vs YSO upstream gap

`bf:place <http://id.loc.gov/vocabulary/countries/{code}>` is what marc2bibframe2 mints from MARC 008 positions 15-17, and what the canonical graph correctly preserves. The LoC countries vocabulary publishes only English `rdfs:label` literals for these URIs; the Finnish-cataloguing audience needs multilingual `skos:prefLabel @fi/@sv/@en`. YSO main has the multilingual labels for every modern country (e.g. `yso:p94426` "Suomi"@fi / "Finland"@sv / "Finland"@en), but neither LoC nor Finto publishes a `skos:exactMatch` between the two URI spaces — only a Wikidata-mediated crosswalk via P3866 + P2347 exists, and it isn't built into either authority feed. Project workaround: vendor a CC0 bridge TTL at `vocab/loc-countries-bridge.ttl` carrying `skos:exactMatch` to YSO main plus inlined fi/sv/en prefLabels; a Skosify pass copies the prefLabels onto the LoC URI before write-out. The fix is local — the bibliographic data round-trips correctly regardless of label coverage, only the display surface improves. Upstream resolution path: propose to NLF/Finto that YSO-paikat publish `skos:exactMatch` triples to the LoC countries URIs.

### Deferred routing candidates

Terms in `GAP` status with plausible routings that we haven't implemented because they had **zero corpus prevalence in the 20 k bench** (YAGNI):

- `bf:Review` / `bf:review` — natural fit for the catch-all `bffi:relation` chain (parallel to `bf:accompaniedBy`'s routing).
- `bf:subseriesEnumeration` / `bf:subseriesStatement` — reuse `bffi:seriesEnumeration` / `bffi:seriesStatement` (subseries is a structural axis of the same concept).
- `bf:variantType` — drop; the existing Title-variant routing's `bffi:marcKey` first-3-char check already discriminates by MARC tag.
- `bf:noteFor` — minor metadata; route into the existing `bffi:Note` bnode shape.

### Diagnostic — running the analysis live

```sh
$ bffi-pipeline diagnose-mappings              # summary + unreachable list
$ bffi-pipeline diagnose-mappings --show all   # also dumps the indirect chains
$ bffi-pipeline diagnose-mappings --max-hops 5 # widen the BFS bound (saturates at 3)
$ bffi-pipeline regenerate-mapping-tables      # rebuild the auto-tables above
```

Automated regression tests lock the bucket counts to a ±5 range and lock the auto-tables against drift. An ontology refresh that shifts either signal fires the regression check.

