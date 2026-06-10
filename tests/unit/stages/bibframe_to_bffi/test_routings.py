"""Unit tests for the P-56 Phase 4 discriminator routings."""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bffi_pipeline.stages.bibframe_to_bffi.routings import (
    AXIS_DEFAULT_CLASSES,
    BF,
    BFFI,
    BFLC,
    DCT,
    INVERSE_PREDICATE_ROUTINGS,
    RELATION_PREDICATE_ROUTINGS,
    SERIES_RELATIONSHIP,
    TITLE_VARIANT_CLASSES,
    _hub_target_type,
    _identifier_scheme_token,
    apply_all_routings,
    drop_undeclared_bf_terms,
    drop_variant_type,
    loc_scheme_uri,
    rename_bflc_marckey,
    route_axis_default_classes,
    route_axis_default_predicates,
    route_hubs,
    route_identifier_schemes,
    route_inverse_predicates,
    route_note_for,
    route_note_type,
    route_provision_activity_statement,
    route_relation_predicates,
    route_series_links,
    route_title_variants,
)

# --- bflc:marcKey rename ------------------------------------------------


def test_rename_bflc_marckey_rewrites_predicate_and_keeps_literal() -> None:
    g = Graph()
    s = URIRef("http://example.org/m")
    g.add((s, BFLC.marcKey, Literal("24500$aA Title")))
    rewritten = rename_bflc_marckey(g)
    assert rewritten == 1
    assert (s, BFLC.marcKey, Literal("24500$aA Title")) not in g
    assert (s, BFFI.marcKey, Literal("24500$aA Title")) in g


# --- Identifier-scheme routing -----------------------------------------


def test_route_identifier_schemes_rewrites_each_loc_class() -> None:
    g = Graph()
    isbn_node = URIRef("http://example.org/isbn-1")
    issn_node = URIRef("http://example.org/issn-1")
    g.add((isbn_node, RDF.type, BF.Isbn))
    g.add((isbn_node, RDF.value, Literal("9780123456789")))
    g.add((issn_node, RDF.type, BF.Issn))

    rewritten = route_identifier_schemes(g)
    assert rewritten == 2

    # Both nodes now type as bffi:Identifier.
    assert (isbn_node, RDF.type, BFFI.Identifier) in g
    assert (issn_node, RDF.type, BFFI.Identifier) in g

    # And carry the LoC scheme URI via bffi:source.
    assert (isbn_node, BFFI.source, loc_scheme_uri(BF.Isbn)) in g
    assert (issn_node, BFFI.source, loc_scheme_uri(BF.Issn)) in g

    # rdf:value passes through untouched.
    assert (isbn_node, RDF.value, Literal("9780123456789")) in g

    # The bf:* typing triples are gone.
    assert (isbn_node, RDF.type, BF.Isbn) not in g
    assert (issn_node, RDF.type, BF.Issn) not in g


def test_route_identifier_schemes_routes_classes_we_never_hardcoded() -> None:
    """The ontology-driven routing picks up every bf:Identifier subclass
    in BIBFRAME 3.0.1, not just the 16 we hard-coded originally. bf:Doi
    and bf:OclcNumber were never in the old hard-coded table; they
    should now route automatically via the subclass walk."""
    g = Graph()
    doi_node = URIRef("http://example.org/doi-1")
    oclc_node = URIRef("http://example.org/oclc-1")
    g.add((doi_node, RDF.type, BF.Doi))
    g.add((oclc_node, RDF.type, BF.OclcNumber))

    rewritten = route_identifier_schemes(g)
    assert rewritten == 2

    assert (doi_node, RDF.type, BFFI.Identifier) in g
    assert (oclc_node, RDF.type, BFFI.Identifier) in g
    assert (doi_node, BFFI.source, loc_scheme_uri(BF.Doi)) in g
    assert (oclc_node, BFFI.source, loc_scheme_uri(BF.OclcNumber)) in g


def test_route_identifier_schemes_skips_already_renamed_block() -> None:
    """A graph that already has only bffi:Identifier (no bf:Isbn) is a no-op."""
    g = Graph()
    s = URIRef("http://example.org/ident")
    g.add((s, RDF.type, BFFI.Identifier))
    g.add((s, BFFI.source, loc_scheme_uri(BF.Isbn)))
    assert route_identifier_schemes(g) == 0


# --- scheme-token derivation --------------------------------------------


def test_identifier_scheme_token_convention_handles_camelcase() -> None:
    """The default CamelCase → kebab-case convention handles all the
    standard cases from BIBFRAME 3.0.1's Identifier subclasses."""
    assert _identifier_scheme_token("Isbn") == "isbn"
    assert _identifier_scheme_token("Issn") == "issn"
    assert _identifier_scheme_token("IssnL") == "issn-l"
    assert _identifier_scheme_token("Ean") == "ean"
    assert _identifier_scheme_token("AudioIssueNumber") == "audio-issue-number"
    assert _identifier_scheme_token("MusicPlate") == "music-plate"
    assert _identifier_scheme_token("OclcNumber") == "oclc-number"
    assert _identifier_scheme_token("Doi") == "doi"


def test_identifier_scheme_token_applies_overrides() -> None:
    """Two BIBFRAME class names need explicit override tokens because
    they don't match the convention."""
    # "OtherIdentifier" drops the "Identifier" suffix.
    assert _identifier_scheme_token("OtherIdentifier") == "other"
    # "VideoRecordingNumber" fuses video+recording into one token.
    assert _identifier_scheme_token("VideoRecordingNumber") == "videorecording-number"


def test_loc_scheme_uri_builds_full_loc_vocabulary_path() -> None:
    """The public helper composes the LoC vocabulary stem + the token."""
    assert str(loc_scheme_uri(BF.Isbn)) == "http://id.loc.gov/vocabulary/identifiers/isbn"
    assert (
        str(loc_scheme_uri(BF.OtherIdentifier)) == "http://id.loc.gov/vocabulary/identifiers/other"
    )


# --- Title-variant routing ---------------------------------------------


def test_route_title_variants_collapses_subclasses_to_bffi_title() -> None:
    g = Graph()
    for i, bf_class in enumerate(TITLE_VARIANT_CLASSES):
        node = URIRef(f"http://example.org/t-{i}")
        g.add((node, RDF.type, bf_class))

    rewritten = route_title_variants(g)
    assert rewritten == len(TITLE_VARIANT_CLASSES)
    # All four type slots collapse to bffi:Title.
    for i, bf_class in enumerate(TITLE_VARIANT_CLASSES):
        node = URIRef(f"http://example.org/t-{i}")
        assert (node, RDF.type, BFFI.Title) in g
        assert (node, RDF.type, bf_class) not in g


# --- Series-link routing -----------------------------------------------


def test_route_series_links_emits_structured_relation_bnode() -> None:
    g = Graph()
    m = URIRef("http://example.org/manifestation")
    series = URIRef("http://example.org/series-1")
    g.add((m, BF.hasSeries, series))

    rewritten = route_series_links(g)
    assert rewritten == 1

    # bf:hasSeries triple is gone.
    assert (m, BF.hasSeries, series) not in g

    # Manifestation now has a bffi:relation to a fresh Relation bnode.
    rel_objs = list(g.objects(m, BFFI.relation))
    assert len(rel_objs) == 1
    rel = rel_objs[0]

    # Relation bnode carries the series relationship and points at series.
    assert (rel, RDF.type, BFFI.Relation) in g
    assert (rel, BFFI.relationship, SERIES_RELATIONSHIP) in g
    assert (rel, BFFI.associatedResource, series) in g


# --- Hub routing --------------------------------------------------------


def test_hub_target_type_marckey_dispatch_table() -> None:
    """Spot-check the routing-table decisions from the mapping doc."""
    # Default: no marcKey → Work.
    assert _hub_target_type("") == BFFI.Work
    # $o arrangement → Arrangement.
    assert _hub_target_type("24000$aFoo$oarrangement") == BFFI.Arrangement
    # $l language → Expression.
    assert _hub_target_type("73002$aFoo$lenglanti") == BFFI.Expression
    # $r key → Expression.
    assert _hub_target_type("24000$aFoo$rD major") == BFFI.Expression
    # 100 + $t (author-attributed Work).
    assert _hub_target_type("1001 $aBach$tBrandenburg concertos") == BFFI.Work
    # 130 series → SeriesExpression (axis default).
    assert _hub_target_type("13000$aSeries title") == BFFI.SeriesExpression
    # 830 series → SeriesExpression.
    assert _hub_target_type("83000$aSeries title") == BFFI.SeriesExpression
    # 730 plain (no Expression signal) → Work.
    assert _hub_target_type("73002$aPlain transcribed title") == BFFI.Work
    # 740 plain → Work.
    assert _hub_target_type("74002$aAnother") == BFFI.Work


def test_route_hubs_picks_type_from_marckey() -> None:
    g = Graph()
    hub = URIRef("http://example.org/hub")
    g.add((hub, RDF.type, BF.Hub))
    g.add((hub, BFFI.marcKey, Literal("73002$aSymphonie no. 5$lenglanti")))

    rewritten = route_hubs(g)
    assert rewritten == 1
    # $l in the marcKey routes the Hub to Expression.
    assert (hub, RDF.type, BFFI.Expression) in g
    assert (hub, RDF.type, BF.Hub) not in g


def test_route_hubs_defaults_to_work_when_marckey_absent() -> None:
    g = Graph()
    hub = URIRef("http://example.org/hub")
    g.add((hub, RDF.type, BF.Hub))
    rewritten = route_hubs(g)
    assert rewritten == 1
    assert (hub, RDF.type, BFFI.Work) in g


# --- apply_all_routings -------------------------------------------------


def test_apply_all_routings_returns_per_routing_counts() -> None:
    """End-to-end on a small synthetic graph: every routing fires at least
    once, the counter dict shape matches the documented keys."""
    g = Graph()
    # Identifier
    isbn = URIRef("http://example.org/isbn")
    g.add((isbn, RDF.type, BF.Isbn))
    # Title variant
    vt = URIRef("http://example.org/vt")
    g.add((vt, RDF.type, BF.VariantTitle))
    # bf:Audio — folded into axis-default class routing. Without a
    # Work-axis co-type the discriminator picks the Expression variant.
    au = URIRef("http://example.org/audio")
    g.add((au, RDF.type, BF.Audio))
    # Series link
    m = URIRef("http://example.org/m")
    s = URIRef("http://example.org/s")
    g.add((m, BF.hasSeries, s))
    # Hub (with a marcKey that needs the bflc rename first)
    hub = URIRef("http://example.org/hub")
    g.add((hub, RDF.type, BF.Hub))
    g.add((hub, BFLC.marcKey, Literal("73002$aFoo")))

    counters = apply_all_routings(g)

    assert counters == {
        "bflc_marckey_renamed": 1,
        "identifier_scheme": 1,
        "title_variant": 1,
        "series_link": 1,
        "relation_predicate": 0,
        "hub": 1,
        "inverse_predicate": 0,
        "note_for": 0,
        "note_type": 0,
        "variant_type_dropped": 0,
        "axis_default_class_work": 0,
        "axis_default_class_expression": 1,  # bf:Audio → NonMusicAudioExpression
        "instance_of_work": 0,
        "instance_of_expression": 0,
        "has_instance_of_work": 0,
        "has_instance_of_expression": 0,
        "issuance": 0,
        "provision_statement_to_date": 0,
        "provision_statement_to_note": 0,
        "dropped_undeclared_bf": 0,
    }

    # The Hub routing reads bffi:marcKey (after rename), so the rename
    # must have run first — Hub's chosen type is Work since the marcKey
    # is a plain 730 without Expression-level signals.
    assert (hub, RDF.type, BFFI.Work) in g
    # bf:Audio without a Work signal lands on the Expression-axis variant.
    assert (au, RDF.type, BFFI.NonMusicAudioExpression) in g


# --- routing 6 (axis-default classes) -----------------------------------


def test_route_axis_default_classes_picks_expression_when_no_work_signal() -> None:
    """Default for subjects with no Work-axis co-type: every axis-split
    ``bf:*`` class lands on the Expression-axis BFFI variant. This is
    the Helmet "Instance URI" pattern — bf:Instance is the subject,
    bf:Monograph is just the content-type echo, no Work signal."""
    g = Graph()
    for i, bf_class in enumerate(AXIS_DEFAULT_CLASSES):
        node = URIRef(f"http://example.org/c-{i}")
        g.add((node, RDF.type, bf_class))
    counters = route_axis_default_classes(g)
    assert counters == {
        "axis_default_class_work": 0,
        "axis_default_class_expression": len(AXIS_DEFAULT_CLASSES),
    }
    for i, (bf_class, (_work_pick, expr_pick)) in enumerate(AXIS_DEFAULT_CLASSES.items()):
        node = URIRef(f"http://example.org/c-{i}")
        assert (node, RDF.type, expr_pick) in g
        assert (node, RDF.type, bf_class) not in g


def test_route_axis_default_classes_picks_work_when_subject_co_typed_bibframework() -> None:
    """A Work URI emitted by marc2bibframe2 is typed ``bf:Work`` (renamed
    to ``bffi:BibframeWork`` by the clean-rename pass that runs before
    any routing) AND ``bf:Monograph`` / etc. The discriminator catches
    the Work signal and routes to the Work-axis BFFI variant."""
    g = Graph()
    work = URIRef("http://example.org/work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((work, RDF.type, BF.Monograph))

    counters = route_axis_default_classes(g)
    assert counters == {
        "axis_default_class_work": 1,
        "axis_default_class_expression": 0,
    }
    assert (work, RDF.type, BFFI.MonographWork) in g
    assert (work, RDF.type, BF.Monograph) not in g
    # The original Work signal is preserved on the subject.
    assert (work, RDF.type, BFFI.BibframeWork) in g


def test_route_axis_default_classes_music_audio_picks_bffi_music_work() -> None:
    """``bf:MusicAudio`` has asymmetric naming in lkd.rdf — there is no
    ``bffi:MusicAudioWork``; the Work-axis pick is ``bffi:MusicWork``."""
    g = Graph()
    work = URIRef("http://example.org/music-work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((work, RDF.type, BF.MusicAudio))

    route_axis_default_classes(g)
    assert (work, RDF.type, BFFI.MusicWork) in g
    # Belt-and-braces: confirm we didn't mint the non-existent class.
    assert (work, RDF.type, URIRef(str(BFFI) + "MusicAudioWork")) not in g


def test_route_axis_default_classes_hub_routed_work_signal_picks_work() -> None:
    """Hub routing (which runs before axis-default-class) may have re-typed
    a subject as ``bffi:Work`` / ``bffi:Arrangement`` / etc. without the
    ``bffi:BibframeWork`` co-type. The discriminator must still catch
    those as Work-axis signals (since they're under bffi:BibframeWork
    via the lkd.rdf class hierarchy)."""
    g = Graph()
    hub = URIRef("http://example.org/hub-as-work")
    g.add((hub, RDF.type, BFFI.Work))  # Hub-routed; not BibframeWork
    g.add((hub, RDF.type, BF.Serial))

    route_axis_default_classes(g)
    assert (hub, RDF.type, BFFI.SerialWork) in g
    assert (hub, RDF.type, BF.Serial) not in g


def test_route_axis_default_classes_audio_folded_into_routing() -> None:
    """``bf:Audio`` is now handled by route_axis_default_classes (was a
    separate route_audio function). marc2bibframe2 emits ``bf:Audio``
    only for non-music audio, so the picks mirror NonMusicAudio's."""
    g = Graph()
    work_audio = URIRef("http://example.org/audio-work")
    g.add((work_audio, RDF.type, BFFI.BibframeWork))
    g.add((work_audio, RDF.type, BF.Audio))

    inst_audio = URIRef("http://example.org/audio-instance")
    g.add((inst_audio, RDF.type, BF.Audio))

    route_axis_default_classes(g)
    # Work URI → Work-axis pick.
    assert (work_audio, RDF.type, BFFI.NonMusicAudioWork) in g
    # Instance URI (no Work signal) → Expression-axis pick.
    assert (inst_audio, RDF.type, BFFI.NonMusicAudioExpression) in g


def test_route_axis_default_classes_no_op_when_already_routed() -> None:
    """Subjects already typed with a BFFI axis variant aren't re-routed —
    only ``bf:*`` axis-split subjects are touched."""
    g = Graph()
    s = URIRef("http://example.org/s")
    g.add((s, RDF.type, BFFI.SeriesExpression))
    counters = route_axis_default_classes(g)
    assert counters == {
        "axis_default_class_work": 0,
        "axis_default_class_expression": 0,
    }


# --- routing 7 (axis-default predicates) --------------------------------


# --- route_provision_activity_statement (URI-fragment discriminator) ----


def test_route_provision_activity_statement_routes_succession_link_to_bffi_date() -> None:
    """``bf:provisionActivityStatement`` on an Instance whose URI carries
    a MARC 76X-78X tag in its fragment (the marc2bibframe2 shape for
    related-Instance hubs from succession-link MARC fields) routes to
    ``bffi:date`` — those statements are date ranges per the corpus.
    """
    g = Graph()
    # Mimics the corpus shape: Instance URI fragment carries the MARC tag.
    inst = URIRef("http://example.org/bib1#Instance780-25")
    g.add((inst, BF.provisionActivityStatement, Literal("1980-1981")))

    counters = route_provision_activity_statement(g)
    assert counters == {"provision_statement_to_date": 1, "provision_statement_to_note": 0}
    assert (inst, BFFI.date, Literal("1980-1981")) in g
    assert (inst, BF.provisionActivityStatement, Literal("1980-1981")) not in g


def test_route_provision_activity_statement_handles_succession_tag_range() -> None:
    """The pattern matches all MARC 760-789 (linking-entry fields) —
    main series (760), has subseries (762), original language (765),
    translation (767), supplements (770/772), host item (773),
    constituent (774), other edition (775), additional physical form
    (776), issued with (777), preceding entry (780), succeeding (785),
    data source (786), other relationship (787)."""
    g = Graph()
    for tag in ("760", "762", "765", "770", "775", "780", "785", "787"):
        inst = URIRef(f"http://example.org/bib1#Instance{tag}-1")
        g.add((inst, BF.provisionActivityStatement, Literal(f"date-range-{tag}")))

    counters = route_provision_activity_statement(g)
    assert counters["provision_statement_to_date"] == 8
    assert counters["provision_statement_to_note"] == 0


def test_route_provision_activity_statement_falls_back_to_note_for_non_succession_context() -> None:
    """Instance URI without the 76X-78X fragment pattern falls back to
    a ``bffi:Note`` bnode wrapper. The text is preserved without an
    EDTF semantic claim."""
    g = Graph()
    inst = URIRef("http://example.org/bib1#Instance")  # no succession tag
    g.add((inst, BF.provisionActivityStatement, Literal("(1990-2013), ISSN")))

    counters = route_provision_activity_statement(g)
    assert counters == {"provision_statement_to_date": 0, "provision_statement_to_note": 1}
    # The original triple is gone.
    assert (inst, BF.provisionActivityStatement, Literal("(1990-2013), ISSN")) not in g
    # An object property points at a fresh Note bnode.
    note_objects = list(g.objects(inst, BFFI.note))
    assert len(note_objects) == 1
    note = note_objects[0]
    # The Note bnode is typed and carries the literal as rdfs:label.
    assert (note, RDF.type, BFFI.Note) in g
    assert (note, RDFS.label, Literal("(1990-2013), ISSN")) in g


def test_route_provision_activity_statement_ignores_unrelated_triples() -> None:
    """The routing only touches triples with ``bf:provisionActivityStatement``
    as the predicate. Other triples on the same subject pass through."""
    g = Graph()
    inst = URIRef("http://example.org/bib1#Instance780-25")
    g.add((inst, RDF.type, BF.Instance))
    g.add((inst, BF.provisionActivityStatement, Literal("1980-1981")))

    route_provision_activity_statement(g)
    # bf:Instance typing triple still present.
    assert (inst, RDF.type, BF.Instance) in g


# --- drop_undeclared_bf_terms (BIBFRAME-ontology-guarded drop) ----------


def test_drop_undeclared_bf_terms_removes_bf_statement_artifact() -> None:
    """``bf:Statement`` isn't declared in BIBFRAME 3.0.1 — marc2bibframe2
    emits it as a flat-text duplicate of the structured ProvisionActivity
    block. Dropping it leaves no information lost (the same content is
    in the sibling structured block)."""
    g = Graph()
    instance = URIRef("http://example.org/instance")
    # Add a known-good triple (bf:provisionActivity is declared).
    g.add((instance, BF.provisionActivity, URIRef("http://example.org/pa")))
    # Add the undeclared bf:Statement artifact predicate.
    g.add((instance, BF.Statement, Literal("Helsinki: Publisher, 2001")))

    dropped = drop_undeclared_bf_terms(g)
    assert dropped == 1
    # Known triple still there.
    assert (instance, BF.provisionActivity, URIRef("http://example.org/pa")) in g
    # Artifact triple gone.
    assert (instance, BF.Statement, Literal("Helsinki: Publisher, 2001")) not in g


def test_drop_undeclared_bf_terms_handles_undeclared_class_in_object_slot() -> None:
    """The guard fires on any of (subject, predicate, object) slots —
    an unknown bf:* in the rdf:type object also triggers a drop."""
    g = Graph()
    s = URIRef("http://example.org/s")
    # bf:UnknownClass isn't in BIBFRAME 3.0.1.
    g.add((s, RDF.type, URIRef("http://id.loc.gov/ontologies/bibframe/UnknownClass")))
    assert drop_undeclared_bf_terms(g) == 1


def test_drop_undeclared_bf_terms_keeps_known_bf_triples() -> None:
    """The guard ONLY drops triples whose bf:* URIs aren't in BIBFRAME.
    Triples using only declared bf:* terms (or no bf:* at all) pass
    through unchanged."""
    g = Graph()
    s = URIRef("http://example.org/s")
    # All three of these terms are declared in BIBFRAME 3.0.1.
    g.add((s, RDF.type, BF.Work))
    g.add((s, BF.mainTitle, Literal("A Title")))
    g.add((s, BF.identifiedBy, URIRef("http://example.org/id")))
    # And one triple with NO bf:* at all.
    g.add((s, RDF.value, Literal("payload")))

    assert drop_undeclared_bf_terms(g) == 0
    assert len(list(g)) == 4


def test_drop_undeclared_bf_terms_ignores_non_bf_namespace_uris() -> None:
    """An unknown URI in a different namespace (rdf:, skos:, example.org)
    is NOT a BIBFRAME artifact and should pass through. The guard fires
    only on the ``bf:`` namespace."""
    g = Graph()
    s = URIRef("http://example.org/s")
    # bf:notInOntology IS a bf:* artifact → drops
    g.add((s, URIRef("http://id.loc.gov/ontologies/bibframe/notInOntology"), Literal("x")))
    # http://example.org/whatever is NOT a bf:* artifact → keeps
    g.add((s, URIRef("http://example.org/whatever"), Literal("y")))

    assert drop_undeclared_bf_terms(g) == 1
    # The non-bf: triple survives.
    assert (s, URIRef("http://example.org/whatever"), Literal("y")) in g


# --- routing 8 (catch-all relation-predicate routing) -------------------


def test_route_relation_predicates_handles_bf_accompaniedby() -> None:
    """``bf:accompaniedBy`` is a true gap in lkd.rdf (no bffi:* equivalent).
    The catch-all routing maps it through the structured bffi:relation
    chain with a LoC-namespaced relationship URI — same shape as Series-link."""
    g = Graph()
    book = URIRef("http://example.org/book")
    cd = URIRef("http://example.org/cd")
    g.add((book, BF.accompaniedBy, cd))

    rewritten = route_relation_predicates(g)
    assert rewritten == 1
    assert (book, BF.accompaniedBy, cd) not in g

    rel_objs = list(g.objects(book, BFFI.relation))
    assert len(rel_objs) == 1
    rel = rel_objs[0]
    assert (rel, RDF.type, BFFI.Relation) in g
    assert (rel, BFFI.relationship, RELATION_PREDICATE_ROUTINGS[BF.accompaniedBy]) in g
    assert (rel, BFFI.associatedResource, cd) in g


def test_route_relation_predicates_skips_bf_hasseries() -> None:
    """``bf:hasSeries`` has its own dedicated routing function so the
    catch-all leaves it alone (avoid double-counting in the
    observability summary)."""
    g = Graph()
    m = URIRef("http://example.org/m")
    s = URIRef("http://example.org/s")
    g.add((m, BF.hasSeries, s))
    rewritten = route_relation_predicates(g)
    assert rewritten == 0
    # bf:hasSeries triple is still there — series_link routing handles it.
    assert (m, BF.hasSeries, s) in g


def test_route_axis_default_predicates_instance_of_picks_work_when_object_untyped() -> None:
    """``bf:instanceOf`` with an untyped object lands on the Work-axis
    default ``bffi:workManifested`` — the safe pick matching marc2bibframe2's
    predominant emit (Instance → Work)."""
    g = Graph()
    inst = URIRef("http://example.org/inst")
    work = URIRef("http://example.org/work")
    g.add((inst, BF.instanceOf, work))

    counters = route_axis_default_predicates(g)
    assert counters["instance_of_work"] == 1
    assert counters["instance_of_expression"] == 0
    assert (inst, BFFI.workManifested, work) in g
    assert (inst, BF.instanceOf, work) not in g


def test_route_axis_default_predicates_instance_of_expression_object_picks_expression() -> None:
    """``bf:instanceOf`` with an object typed bffi:Expression lands on
    ``bffi:expressionManifested`` — the discriminator catches the
    Expression-axis signal on the object side."""
    g = Graph()
    inst = URIRef("http://example.org/inst")
    expr = URIRef("http://example.org/expr")
    g.add((expr, RDF.type, BFFI.Expression))
    g.add((inst, BF.instanceOf, expr))

    counters = route_axis_default_predicates(g)
    assert counters["instance_of_expression"] == 1
    assert counters["instance_of_work"] == 0
    assert (inst, BFFI.expressionManifested, expr) in g


def test_route_axis_default_predicates_has_instance_picks_work_when_subject_untyped() -> None:
    """``bf:hasInstance`` with an untyped subject lands on
    ``bffi:manifestationOfWork`` (the inverse-direction default)."""
    g = Graph()
    work = URIRef("http://example.org/work")
    inst = URIRef("http://example.org/inst")
    g.add((work, BF.hasInstance, inst))

    counters = route_axis_default_predicates(g)
    assert counters["has_instance_of_work"] == 1
    assert (work, BFFI.manifestationOfWork, inst) in g


def test_route_axis_default_predicates_has_instance_expression_subject_picks_expression() -> None:
    """``bf:hasInstance`` from an Expression URI lands on
    ``bffi:manifestationOfExpression`` — discriminator on subject side."""
    g = Graph()
    expr = URIRef("http://example.org/expr")
    inst = URIRef("http://example.org/inst")
    g.add((expr, RDF.type, BFFI.SeriesExpression))  # a leaf Expression-axis class
    g.add((expr, BF.hasInstance, inst))

    counters = route_axis_default_predicates(g)
    assert counters["has_instance_of_expression"] == 1
    assert (expr, BFFI.manifestationOfExpression, inst) in g


def test_route_axis_default_predicates_issuance_is_flat_rename_regardless_of_context() -> None:
    """``bf:issuance`` always renames to ``bffi:issuance`` — the
    mapping-doc-listed alternative ``bffi:extensionPlan`` has a
    different domain AND range, so it isn't a per-statement substitute."""
    g = Graph()
    # Manifestation context
    inst = URIRef("http://example.org/inst")
    serl = URIRef("http://id.loc.gov/vocabulary/issuance/serl")
    g.add((inst, BF.issuance, serl))
    # Work context — same outcome.
    work = URIRef("http://example.org/work")
    g.add((work, RDF.type, BFFI.BibframeWork))
    g.add((work, BF.issuance, serl))

    counters = route_axis_default_predicates(g)
    assert counters["issuance"] == 2
    assert (inst, BFFI.issuance, serl) in g
    assert (work, BFFI.issuance, serl) in g
    # And neither bf:issuance triple survives.
    assert (inst, BF.issuance, serl) not in g
    assert (work, BF.issuance, serl) not in g


def test_route_axis_default_predicates_counter_dict_shape() -> None:
    """Empty graph: counter dict carries all five keys at zero — locks
    the shape downstream observability code depends on."""
    counters = route_axis_default_predicates(Graph())
    assert counters == {
        "instance_of_work": 0,
        "instance_of_expression": 0,
        "has_instance_of_work": 0,
        "has_instance_of_expression": 0,
        "issuance": 0,
    }


# --- inverse-predicate triple-swap --------------------------------------


def test_route_inverse_predicates_swaps_subject_and_object() -> None:
    """``?s bf:agentOf ?o`` rewrites as ``?o bffi:agent ?s`` — the
    triple direction flips, the predicate switches to the forward
    form. Same shape for the four other inverse predicates."""
    g = Graph()
    agent = URIRef("http://example.org/agent")
    contrib = URIRef("http://example.org/contribution")
    g.add((agent, BF.agentOf, contrib))

    rewritten = route_inverse_predicates(g)
    assert rewritten == 1
    # Original direction gone; canonical forward direction present.
    assert (agent, BF.agentOf, contrib) not in g
    assert (contrib, BFFI.agent, agent) in g


def test_route_inverse_predicates_covers_all_five_pairs() -> None:
    """Smoke-test the entire INVERSE_PREDICATE_ROUTINGS registry —
    each (bf:Xof → bffi:X) pair fires once on a representative triple."""
    g = Graph()
    for i, bf_pred in enumerate(INVERSE_PREDICATE_ROUTINGS):
        s = URIRef(f"http://example.org/s-{i}")
        o = URIRef(f"http://example.org/o-{i}")
        g.add((s, bf_pred, o))

    rewritten = route_inverse_predicates(g)
    assert rewritten == len(INVERSE_PREDICATE_ROUTINGS)
    for i, (bf_pred, bffi_forward) in enumerate(INVERSE_PREDICATE_ROUTINGS.items()):
        s = URIRef(f"http://example.org/s-{i}")
        o = URIRef(f"http://example.org/o-{i}")
        assert (o, bffi_forward, s) in g
        assert (s, bf_pred, o) not in g


# --- note-shape routings ------------------------------------------------


def test_route_note_for_swaps_to_forward_bffi_note() -> None:
    """``?note bf:noteFor ?subject`` becomes ``?subject bffi:note ?note`` —
    same swap pattern as the inverse predicates, but bf:noteFor isn't
    in the INVERSE_PREDICATE_ROUTINGS registry (it has its own routing
    because its semantic — *anchoring a Note to its subject* — is
    note-specific rather than a generic inverse-relation pattern)."""
    g = Graph()
    note = URIRef("http://example.org/note")
    subject = URIRef("http://example.org/subject")
    g.add((note, BF.noteFor, subject))

    rewritten = route_note_for(g)
    assert rewritten == 1
    assert (subject, BFFI.note, note) in g
    assert (note, BF.noteFor, subject) not in g


def test_route_note_type_renames_to_dct_type() -> None:
    """``?note bf:noteType "Summary"`` becomes ``?note dct:type "Summary"`` —
    DC Terms' standard categorisation predicate stands in for the
    missing ``bffi:noteType`` (allowed by BFFI namespace discipline:
    reuse a standard term)."""
    g = Graph()
    note = URIRef("http://example.org/note")
    g.add((note, BF.noteType, Literal("Summary")))

    rewritten = route_note_type(g)
    assert rewritten == 1
    assert (note, DCT.type, Literal("Summary")) in g
    assert (note, BF.noteType, Literal("Summary")) not in g


# --- bf:Review (class) + bf:review (predicate) -------------------------


def test_route_review_class_via_axis_default_anchored_at_bibframework() -> None:
    """``bf:Review`` has no parent in BIBFRAME — both AXIS_DEFAULT_CLASSES
    slots collapse to ``bffi:BibframeWork`` so the routing picks that
    anchor regardless of co-type signal."""
    g = Graph()
    review = URIRef("http://example.org/review-work")
    g.add((review, RDF.type, BF.Review))

    route_axis_default_classes(g)
    assert (review, RDF.type, BFFI.BibframeWork) in g
    assert (review, RDF.type, BF.Review) not in g


def test_route_review_predicate_via_relation_chain() -> None:
    """``?w bf:review ?r`` rewrites to the structured ``bffi:relation`` chain
    with ``…/relationship/review`` (parallel to bf:accompaniedBy)."""
    g = Graph()
    work = URIRef("http://example.org/work")
    target = URIRef("http://example.org/reviewed")
    g.add((work, BF.review, target))

    rewritten = route_relation_predicates(g)
    assert rewritten == 1
    rel_objs = list(g.objects(work, BFFI.relation))
    assert len(rel_objs) == 1
    rel = rel_objs[0]
    assert (rel, RDF.type, BFFI.Relation) in g
    assert (
        rel,
        BFFI.relationship,
        URIRef("http://id.loc.gov/vocabulary/relationship/review"),
    ) in g
    assert (rel, BFFI.associatedResource, target) in g


# --- bf:variantType drop -----------------------------------------------


def test_drop_variant_type_removes_redundant_triple() -> None:
    """``bf:variantType`` info is already encoded in the marcKey first-3-
    char MARC tag (246 parallel / 740 added-entry / etc.). The predicate
    is redundant signal; drop it so it doesn't pollute the closed-namespace
    emit graph."""
    g = Graph()
    title = URIRef("http://example.org/title")
    g.add((title, BF.variantType, Literal("parallel")))
    # An unrelated triple should survive.
    g.add((title, BFFI.marcKey, Literal("24631$aParallel form")))

    dropped = drop_variant_type(g)
    assert dropped == 1
    assert (title, BF.variantType, Literal("parallel")) not in g
    assert (title, BFFI.marcKey, Literal("24631$aParallel form")) in g
