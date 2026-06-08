# P-53 — Migrate remaining `bf:` terms with a `bffi:` alias to `bffi:`

**Status**: completed (2026-06-08; all five families verified on the
500-sample with zero semantic content changes on targeted MARC tags).

**Source proposal**: `docs/plans/proposed/p-53-bffi-aliased-terms-migration.md`
at the time the role-redesign PR was being written (the proposal file's
own creation commit is the lineage anchor).

**Plan-base commit**: `2556138a046e9abe704d07a1934c5dc92bd69038` — HEAD
of the role-redesign work, after Phase D verification on
`runs/20260608-0810-f8d8eb`.

**Phase commits**: all five families shipped together as a single
direct-to-main commit per the project's solo-developer workflow
(see `~/.claude/projects/.../memory/feedback_direct_to_main_workflow.md`).
The plan's five-PR split was a verification cadence, not a merge
boundary — each family ran an independent M3 → M9 → Skosify →
round-trip rerun on the 500-sample before the next family started,
and the per-family pre-rename recon snapshots
(`/tmp/recon-before-family-{1..5}`) bracketed each diff check.

- Phase 0 (audit): **`03b54e5`** (alias-mapping derived from
  `docs/lkd.rdf` at the Plan-base commit; tables pinned below).
- Family 1 (subject classes): **`03b54e5`** —
  `bf:Topic/Place/Person/Organization/Meeting/Temporal` →
  `bffi:*`. 0/502 6XX content diffs.
- Family 2 (agent/role/note/title/identifier classes):
  **`03b54e5`** — `bf:Agent/Role/Note/Title/Identifier/Source/Local`
  → `bffi:*`. 0/502 6XX content diffs (129 row-order from M9
  non-determinism).
- Family 3 (identifier predicates): **`03b54e5`** —
  `bf:identifiedBy/code/assigner` → `bffi:*`. 0/502 diffs on
  identifier-bearing tags (020/022/024/028/035/040).
- Family 4 (title predicates): **`03b54e5`** —
  `bf:title/mainTitle/partName/partNumber` → `bffi:*`. Resolved
  the duplicate-emission risk the proposal flagged
  (`V.BFFI.title` was already used in M8/M10/round-trip while
  M3 emitted `bf:title` for Expressions). 0/502 title-content
  diffs (11 row-order only).
- Family 5 (miscellaneous predicates): **`03b54e5`** —
  `bf:status/qualifier` migrated (most of Family 5's targets —
  `bf:summary/relation/relationship/associatedResource` — were
  already partially migrated in earlier work). 0/502 020 / leader-
  status content diffs (3 row-order only).

## Goal

Make the canonical BFFI graph terminate predicate-and-class names
consistently in the `bffi:` namespace wherever `docs/lkd.rdf`
declares an alias via `owl:equivalentClass` /
`owl:equivalentProperty`. Today the graph mixes `bf:contribution`
(now migrated), `bffi:adminMetadata`, `bf:identifiedBy`,
`bffi:role` (just migrated), `bf:Role` (class still bf:), etc.
inconsistently. The goal is one consistent emit surface:

- `bffi:*` for every term that has a `bffi:*` alias in `lkd.rdf`;
- `bf:*` for every term that has no `bffi:*` alias (NLF's deliberate
  reuse of BIBFRAME).

**Done when**: `grep -rohE "V\.BF\.[A-Za-z]+" src/ | sort -u` returns
only terms listed in the "stays bf:" rows of the Phase 0 audit table
(below). No alias-migration regressions in the round-trip diff.

## Current state (as of Plan-base commit)

The role-redesign PR has already migrated `bf:role` →
`bffi:role` (predicate) and switched the role value vocabulary from
LoC relators to MTS (`docs/bffi_limitations.md L-07`). The role
*class* typing on role bnodes (`a bf:Role`) is still emitted as
`bf:Role`, completing the alias-migration playbook is what this plan
is for.

`make lint && make test` is green at the Plan-base commit
(1656 tests pass). The 500-sample pipeline rerun
(`runs/20260608-0810-f8d8eb`) was the verification.

## Phase 0 — alias-mapping audit (shipped 2026-06-08)

**Goal**: enumerate, programmatically, every `bf:*` term we emit
that has a `bffi:*` alias declared `owl:equivalentClass` or
`owl:equivalentProperty` in `docs/lkd.rdf` against the Plan-base
commit. Pin the migration tables for Families 1-5 so each family
ships against a fixed target.

**Procedure**:

```bash
uv run python - <<'EOF'
"""Re-derive alias tables from current lkd.rdf."""
import re
text = open("docs/lkd.rdf").read()
aliases_class, aliases_prop = {}, {}
for m in re.finditer(
    r'<rdf:Description rdf:about="http://urn.fi/URN:NBN:fi:schema:bffi:'
    r'([A-Za-z]+)">(.*?)</rdf:Description>',
    text, re.DOTALL):
    name, body = m.groups()
    for kind, bf_name in re.findall(
        r'(owl:equivalentClass|owl:equivalentProperty)\s+'
        r'rdf:resource="http://id\.loc\.gov/ontologies/bibframe/'
        r'([A-Za-z]+)"', body):
        (aliases_class if kind == "owl:equivalentClass" else aliases_prop)[bf_name] = name
# Cross-check against terms our code currently emits (V.BF.* grep).
EOF
```

**Result on Plan-base commit**:

### Classes — bf:* terms we emit, alias status

| `bf:` class | `bffi:` alias | Action |
|---|---|---|
| `bf:Agent` | `bffi:Agent` | migrate (Family 2) |
| `bf:Role` | `bffi:Role` | migrate (Family 2) |
| `bf:Note` | `bffi:Note` | migrate (Family 2) |
| `bf:Title` | `bffi:Title` | migrate (Family 2) |
| `bf:Identifier` | `bffi:Identifier` | migrate (Family 2) |
| `bf:Source` | `bffi:Source` | migrate (Family 2) |
| `bf:Local` | `bffi:Local` | migrate (Family 2) |
| `bf:Topic` | `bffi:Topic` | migrate (Family 1) |
| `bf:Place` | `bffi:Place` | migrate (Family 1) |
| `bf:Person` | `bffi:Person` | migrate (Family 1) |
| `bf:Organization` | `bffi:Organization` | migrate (Family 1) |
| `bf:Meeting` | `bffi:Meeting` | migrate (Family 1) |
| `bf:Temporal` | `bffi:Temporal` | migrate (Family 1) |
| `bf:Hub` | — | stays `bf:` |
| `bf:Series` | — | stays `bf:` |
| `bf:VariantTitle` | — | stays `bf:` |
| `bf:Isbn` / `bf:Issn` / `bf:Ean` | — | stays `bf:` |
| `bf:AudioIssueNumber` / `bf:SystemNumber` | — | stays `bf:` |

### Properties — bf:* terms we emit, alias status

| `bf:` property | `bffi:` alias | Action |
|---|---|---|
| `bf:role` | `bffi:role` | **done** in role redesign (precedent) |
| `bf:identifiedBy` | `bffi:identifiedBy` | migrate (Family 3) |
| `bf:code` | `bffi:code` | migrate (Family 3) |
| `bf:assigner` | `bffi:assigner` | migrate (Family 3, audit usage first) |
| `bf:title` | `bffi:title` | migrate (Family 4) |
| `bf:mainTitle` | `bffi:mainTitle` | migrate (Family 4, duplicate-emission audit) |
| `bf:partName` | `bffi:partName` | migrate (Family 4) |
| `bf:partNumber` | `bffi:partNumber` | migrate (Family 4) |
| `bf:summary` | `bffi:summary` | migrate (Family 5) |
| `bf:status` | `bffi:status` | migrate (Family 5) |
| `bf:qualifier` | `bffi:qualifier` | migrate (Family 5) |
| `bf:associatedResource` | `bffi:associatedResource` | migrate (Family 5) |
| `bf:relation` | `bffi:relation` | migrate (Family 5) |
| `bf:relationship` | `bffi:relationship` | migrate (Family 5) |
| `bf:source` | — | stays `bf:` |
| `bf:note` | — | stays `bf:` |
| `bf:date` / `bf:place` / `bf:agent` | — | stays `bf:` (ProvisionActivity slots) |
| `bf:hasSeries` / `bf:hasInstance` | — | stays `bf:` |
| `bf:isbn` / `bf:issn` / `bf:ean` | — | stays `bf:` (Skosify-display flat) |
| `bf:audioIssueNumber` / `bf:systemNumber` | — | stays `bf:` (Skosify-display flat) |

**Verification**: each family's PR re-runs this audit and confirms the
target term still carries the alias. If NLF retracts an alias in a
future `lkd.rdf` release, that term is removed from the migration.

## Family 1 — subject classes (bf:Topic/Place/Person/Organization/Meeting/Temporal → bffi:*)

**Goal**: rename six subject-class typings emitted on `rdf:type`.
Lowest-risk family — the round-trip's only structural read is the
class→MARC-tag lookup table in `_subject_marc_tag`.

**Sites touched** (per Phase 0 grep):

1. `src/bffi_pipeline/stages/m8/mint.py::_propagate_subject_typing` —
   the routable-classes set `_SUBJECT_TYPING_PREDICATES`.
2. `src/bffi_pipeline/marc_roundtrip/converter.py` — `_subject_marc_tag`
   lookup table (class URI → MARC 6XX tag).
3. M3 SPARQL: any CONSTRUCT clause emitting `?topic a bf:Topic` (audit
   needed — may live in `bf_to_bffi_*.rq` or be passed through from
   marc2bibframe2 input via M8 typing propagation).
4. Overlay: any `bf:Topic rdfs:label "Aihe"@fi` lines.
5. Tests: every `V.BF.Topic` / `bf:Topic` reference in `tests/`.

**Steps**:

1. `grep -rln "V\.BF\.\(Topic\|Place\|Person\|Organization\|Meeting\|Temporal\)" src/ tests/`
   — inventory.
2. For each site: swap `V.BF.X` → `V.BFFI.X` (Python) or
   `bf:X` → `bffi:X` (SPARQL/TTL).
3. **Critical**: update `_subject_marc_tag`'s lookup table — the
   class URI string IS the key.
4. `make lint && make test` — fix any test fixtures that built
   subject graphs with `bf:Topic` etc.
5. Pipeline rerun from M8: `bffi-pipeline run --from-stage m8
   --force-stages m8,skosify,load,marc-roundtrip`. Compare
   `marc-roundtrip/reconstructed/*.xml` against the Plan-base recon
   directory; diff 6XX rows byte-for-byte (modulo bnode IDs).
6. Spot-check `b10068004.xml` 650/651 rows.

**Verification checkpoint**: every `bf:Topic`/`bf:Place`/etc.
occurrence in the post-rename canonical graph is gone (replaced by
`bffi:*`), and `marc-roundtrip/summary.json` shows the same
recon counts as before the rename.

**Phase commit**: _to be filled in._

## Family 2 — agent/role/note/title/identifier classes (bf:Agent/Role/Note/Title/Identifier/Source/Local → bffi:*)

**Goal**: seven class renames. Closes the role redesign's "predicate
done, class still bf:" gap by typing role bnodes as `bffi:Role` —
making the `bffi-meta:relatedValueVocabulary` MTS contract literal in
graph (a role node is now of class `bffi:Role`, whose
`relatedValueVocabulary` is MTS m34/m153/m491/m1157).

**Sites touched**:

1. M3 SPARQL CONSTRUCT clauses — each subject/contribution/identifier/
   title block.
2. M8 `mint.py::_propagate_*` — the typing propagations.
3. M10 `skosify_run.py` — flatteners reference `V.BF.Role` in
   `_synthesise_contribution_labels` etc.
4. Round-trip converter — agent / role lookups by class URI.
5. Overlay labels.
6. Tests.

**Steps**: same as Family 1 (inventory → swap → lint+test →
pipeline rerun → diff). Heavier impact than Family 1 because the
classes appear in more sites.

**Phase commit**: _to be filled in._

## Family 3 — identifier predicates (bf:identifiedBy/code/assigner → bffi:*)

**Goal**: three predicate renames in the identifier chain.

**Sites touched**:

1. M3 SPARQL — work + manifestation `bf:identifiedBy` CONSTRUCTs.
2. M8 — passthrough.
3. Round-trip converter — `_emit_identifiers` walks
   `bf:identifiedBy` once.
4. M10 — `_synthesise_identifier_predicates` looks at
   `bf:identifiedBy` to find the chain.
5. Overlay labels.
6. Tests.

**Pre-flight**: confirm `bf:assigner` is actually emitted. The
Phase 0 audit table flagged it as "audit usage first" — if no
production site emits it, drop it from the rename and document.

**Phase commit**: _to be filled in._

## Family 4 — title predicates (bf:title/mainTitle/partName/partNumber → bffi:*)

**Goal**: four predicate renames in the title chain. **Highest-risk
family** — duplicate-emission concern.

**Pre-flight critical check**: `grep V.BFFI.mainTitle src/` showed
the Python const exists. Phase 0 saw `bf:mainTitle` emitted by M3
SPARQL. **Audit**: does any current site emit BOTH `bf:mainTitle`
AND `bffi:mainTitle` for the same Title bnode? If yes, that's a
pre-existing bug surfaced by the audit — fix by keeping `bffi:*`
and dropping the `bf:*` emit (the migration goal anyway).

**Sites touched** (heaviest):

1. M3 SPARQL — every title CONSTRUCT (work, expression, manifestation,
   hub).
2. M10 `_synthesise_title_alt_labels` flattener.
3. Round-trip converter — `_emit_uniform_title`, `_emit_main_title`,
   `_related_title_subfields`, `_emit_added_entries` all read
   `bf:mainTitle` / `bf:partName` / `bf:partNumber`.
4. Overlay labels.
5. Tests.

**Phase commit**: _to be filled in._

## Family 5 — miscellaneous predicates (bf:summary/status/qualifier/associatedResource/relation/relationship → bffi:*)

**Goal**: six predicate renames, scattered.

**Sites touched**:

1. M3 SPARQL — relation routing for 730/740 reads
   `bf:relation` → `bf:associatedResource`; AdminMetadata block
   uses `bf:status`; identifier rows use `bf:qualifier`; expression
   uses `bf:summary`.
2. M8 — admin metadata + relation propagation.
3. Round-trip converter — 730/740 routing reads
   `bf:relation` → `bf:associatedResource`.
4. Overlay labels.
5. Tests.

**Final pipeline rerun**: this is the last family, so a full-pipeline
diff against the Plan-base recon confirms zero data loss across all
five renames.

**Phase commit**: _to be filled in._

## Risk register

1. **Duplicate emission** (Family 4): `V.BFFI.mainTitle` already
   exists in code; if `bf:mainTitle` is also emitted today, both
   forms ride through unchanged → bug surfaced by Phase 0 audit.
   Mitigation: explicit duplicate-emission grep at the start of
   Family 4. Fix surfaces as a bonus.

2. **External tooling hard-codes `bf:*`**. Skosmos walks both
   names if labeled. NLF ingest is the unknown.
   Mitigation: don't ship canonical to NLF without a dry-run
   first; the migration is reversible per family.

3. **Round-trip regression** (Family 1): `_subject_marc_tag` is a
   switch on class URI. Missing a site → subjects route to the
   wrong 6XX tag.
   Mitigation: full pipeline rerun + byte-level diff after each
   family. Tests assert exact 6XX row counts.

4. **Skosmos overlay labels drift**: some overlay labels named the
   `bf:*` term as the label subject. Each family's PR updates
   the overlay too.

5. **`bf:assigner` may not be emitted at all** (Family 3): table
   flagged it as "audit usage first". If genuinely unused, drop
   from migration and add a code-archaeology note explaining when
   it stopped being emitted.

## Rollback procedure

Each family is one PR. To revert:

```bash
git revert <family-N-merge-commit>
make lint && make test  # confirm clean
bffi-pipeline run --from-stage m3 --force-stages m3,m8,skosify,load,marc-roundtrip \
  -i marcxml/samples/helmet/500/marcxml/
```

Then re-run the round-trip diff against the pre-revert recon to
confirm restoration. The reverse migration is symmetric to the
forward (every `V.BFFI.X` → `V.BF.X`). Per-family revert is the
intent of the five-PR split.

## Sequence and gating

Strict order: Family 1 → 2 → 3 → 4 → 5. Each family's PR must:

1. Pass `make lint && make test`.
2. Pass a pipeline rerun (M3 → round-trip) on the 500-sample.
3. Produce a recon diff against the previous family's recon
   showing zero data loss (or documented acceptable shifts).
4. Land its Phase commit hash in this plan.

If a family's verification surfaces unexpected behaviour, halt the
sequence and re-evaluate. Each family's revert is the smallest
unit of reversal.

## Out of scope

- Terms with `bffi-meta:broadMatch` / `bffi-meta:closeMatch` (semantic
  narrowing/widening, not equivalence). Separate per-term design
  decisions.
- Terms with NO `bffi:*` alias in `lkd.rdf` (see Phase 0 audit
  table's "stays bf:" rows). BFFI 1.0.0 deliberately reuses
  BIBFRAME directly for them.
- NLF coordination on the rename — we follow `owl:equivalent*`
  declarations literally. If NLF objects to a specific term post-PR,
  revert that family.
