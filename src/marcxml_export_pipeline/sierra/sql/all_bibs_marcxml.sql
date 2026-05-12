-- One row per non-suppressed bib record, with leader and varfields (incl. subfields)
-- aggregated into JSON columns so a single streamed row carries everything needed
-- to produce one MARCXML file. The filename column is record_metadata.record_num
-- (the human-facing Sierra bib number, without the 'b' prefix or check digit).
-- record_last_updated_gmt is used by the processor to synthesize 005, which
-- Sierra writes at export time rather than persisting. items is the per-copy
-- holdings info used to emit one 852 datafield per non-suppressed linked item.
SELECT
    rm.record_num AS record_num,
    (
        SELECT to_jsonb(lf)
        FROM (
            SELECT
                record_status_code,
                record_type_code,
                bib_level_code,
                char_encoding_scheme_code,
                encoding_level_code,
                descriptive_cat_form_code,
                multipart_level_code,
                base_address
            FROM sierra_view.leader_field
            WHERE record_id = b.record_id
            LIMIT 1
        ) lf
    ) AS leader,
    (
        SELECT jsonb_agg(
            jsonb_build_object(
                'id',            vf.id,
                'marc_tag',      vf.marc_tag,
                'marc_ind1',     vf.marc_ind1,
                'marc_ind2',     vf.marc_ind2,
                'field_content', vf.field_content,
                'subfields', (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'tag',           sf.tag,
                            'content',       sf.content,
                            'display_order', sf.display_order
                        ) ORDER BY sf.display_order
                    )
                    FROM sierra_view.subfield sf
                    WHERE sf.varfield_id = vf.id
                )
            ) ORDER BY vf.marc_tag, vf.id
        )
        FROM sierra_view.varfield vf
        WHERE vf.record_id = b.record_id
    ) AS varfields,
    (
        -- Fixed-length controlfields (006/007/008) reconstructed from the
        -- per-position p00..p39 columns. NULL positions become spaces so
        -- subsequent characters stay at the right offset.
        SELECT jsonb_agg(
            jsonb_build_object(
                'control_num', cf.control_num,
                'varfield_type_code', cf.varfield_type_code,
                'occ_num', cf.occ_num,
                'content', concat(
                    COALESCE(cf.p00, ' '), COALESCE(cf.p01, ' '), COALESCE(cf.p02, ' '), COALESCE(cf.p03, ' '),
                    COALESCE(cf.p04, ' '), COALESCE(cf.p05, ' '), COALESCE(cf.p06, ' '), COALESCE(cf.p07, ' '),
                    COALESCE(cf.p08, ' '), COALESCE(cf.p09, ' '), COALESCE(cf.p10, ' '), COALESCE(cf.p11, ' '),
                    COALESCE(cf.p12, ' '), COALESCE(cf.p13, ' '), COALESCE(cf.p14, ' '), COALESCE(cf.p15, ' '),
                    COALESCE(cf.p16, ' '), COALESCE(cf.p17, ' '), COALESCE(cf.p18, ' '), COALESCE(cf.p19, ' '),
                    COALESCE(cf.p20, ' '), COALESCE(cf.p21, ' '), COALESCE(cf.p22, ' '), COALESCE(cf.p23, ' '),
                    COALESCE(cf.p24, ' '), COALESCE(cf.p25, ' '), COALESCE(cf.p26, ' '), COALESCE(cf.p27, ' '),
                    COALESCE(cf.p28, ' '), COALESCE(cf.p29, ' '), COALESCE(cf.p30, ' '), COALESCE(cf.p31, ' '),
                    COALESCE(cf.p32, ' '), COALESCE(cf.p33, ' '), COALESCE(cf.p34, ' '), COALESCE(cf.p35, ' '),
                    COALESCE(cf.p36, ' '), COALESCE(cf.p37, ' '), COALESCE(cf.p38, ' '), COALESCE(cf.p39, ' ')
                )
            ) ORDER BY cf.control_num, cf.occ_num, cf.id
        )
        FROM sierra_view.control_field cf
        WHERE cf.record_id = b.record_id
    ) AS controlfields,
    rm.record_last_updated_gmt AS record_last_updated_gmt,
    (
        -- One entry per non-suppressed linked item. Processor maps these to
        -- 852 datafields: $b=location_code, $h=call_number,
        -- $p=barcode, $t=copy_num (when > 1). ``itype_code_num`` joins to
        -- ``sierra_view.itype_property_myuser.code`` for the cataloguer-
        -- assigned item type; ``marcxml_export_pipeline.sierra.itype_to_rda``
        -- maps that code to RDA 336/337/338 codes that the processor
        -- synthesises onto the bib when no Sierra-side 33X varfield is
        -- coded (else the M2 marcxml-content-minimum gate drops the
        -- whole record).
        SELECT jsonb_agg(
            jsonb_build_object(
                'location_code', i.location_code,
                'copy_num', i.copy_num,
                'item_type_num', i.itype_code_num,
                'call_number', (
                    SELECT vf.field_content
                    FROM sierra_view.varfield vf
                    WHERE vf.record_id = i.record_id
                      AND vf.marc_tag IN ('090', '099')
                    ORDER BY vf.marc_tag, vf.occ_num
                    LIMIT 1
                ),
                'barcode', (
                    SELECT vf.field_content
                    FROM sierra_view.varfield vf
                    WHERE vf.record_id = i.record_id
                      AND vf.varfield_type_code = 'b'
                    ORDER BY vf.occ_num
                    LIMIT 1
                )
            ) ORDER BY bil.items_display_order, i.record_id
        )
        FROM sierra_view.item_record i
        JOIN sierra_view.bib_record_item_record_link bil
          ON bil.item_record_id = i.record_id
        WHERE bil.bib_record_id = b.record_id
          AND i.is_suppressed = false
    ) AS items,
    (
        -- Bib-level material code (joins to
        -- ``sierra_view.material_property_myuser.code``). Authoritative
        -- "what kind of manifestation is this bib" signal that the
        -- processor uses *first* for RDA-33X synthesis when no
        -- cataloguer-coded 33X varfield exists; items'
        -- ``itype_code_num`` is the fallback. See
        -- ``marcxml_export_pipeline.sierra.itype_to_rda.MATERIAL_TO_RDA``.
        SELECT bp.material_code
        FROM sierra_view.bib_record_property bp
        WHERE bp.bib_record_id = b.record_id
        LIMIT 1
    ) AS material_code
FROM sierra_view.bib_record b
LEFT JOIN sierra_view.record_metadata rm ON rm.id = b.record_id
WHERE b.is_suppressed = false
ORDER BY b.record_id
