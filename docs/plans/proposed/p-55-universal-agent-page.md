# P-55 — Universal agent page in Skosmos

**Status**: proposed.

**Scope**: every agent referenced in the BFFI graph becomes a concept-renderable page in a sibling Skosmos vocab (`bffiAgents`), showing identifying metadata (from KANTO when reconciled, from cataloguer text otherwise) plus inbound links to every Work / Expression / Manifestation the agent is associated with. Agents lacking an authority URI get a deterministic name-hash URI so all "Smith, John" entries collapse onto one page; that page carries a flag making clear the URI is **not authoritative** and may conflate distinct people.

**Proposal-base commit**: TBD (post-genid-leak-fix run).

## Motivation

Cataloguers asked for two distinct lookup modes on the same surface:

1. **Authority-led**: "show me everything in Helmet attributed to KANTO `finaf:000185126` (Edward Elgar)."
2. **Name-led**: "show me everything in Helmet attributed to anyone called 'Korhonen, Anna' even if we haven't reconciled to KANTO yet."

Neither mode exists in Skosmos today. Reconciled agents land at their KANTO URIs and DO have concept pages, but those pages show only the KANTO record's own data — no inbound links from the pipeline's Works / Expressions / Manifestations. Unreconciled agents land at per-record raw URIs (`bib:raw/<bib>#Agent100-N`) — no aggregation across records that mention the same name, no readable concept page.

Helmet's corpus is ~800 k records. Cataloguers won't create KANTO records for every personal name in 850 k contributions. The pipeline needs a useful surface for the long tail of unreconciled agents too — explicitly typed as "best-effort name aggregation, not authority work."

## Approach

Three building blocks. Each is independently shippable; the value compounds.

### Block 1 — Name-hash agent URIs + structured birth/death years (M9)

Two coupled changes:

**B1a — parse MARC 100/700 `$d` and emit structured birth/death years.**

Today the `$d` content ("1857-1934", "1850-", "1920?-1980", "n. 1850") is preserved in `rdfs:label` / `skos:prefLabel` as a free-text suffix on the agent name — no structured year predicates emitted on raw bib agents. Add a parser that pulls birth and death years (when present and unambiguous) out of `$d` and emits them as RDA predicates matching the KANTO shape we already see in Fuseki:

```turtle
<agent:9c1a…> rda:P50121 "1857" ;   # date of birth
              rda:P50120 "1934" .    # date of death
```

This mirrors KANTO's serialisation exactly so the Skosmos page renders KANTO-reconciled and name-hash agents under the same property rows — no special-casing in `skosmos-config.ttl`. Parser handles the four dominant Helmet conventions: `"1857-1934"`, `"1850-"` (alive / unknown death), `"-1934"` (unknown birth), `"n. 1850"` / `"approx. 1850"` / `"1850?"` (approximate — emit but flag `bffi-prov:approximate "true"` so the Skosmos page can italicise). Drops `$d` entirely if it doesn't parse (cataloguer-typed free-text like "kuningatar" → no year).

**B1b — name-hash URI minting tier.**

Add a tier between KANTO/VIAF reconciliation miss and the current `bib:raw/<bib>#Agent100-N` fallback:

```python
# src/bffi_pipeline/stages/m9/agent_resolver.py (new file or extend
# graph_mutate.py)

def _mint_name_hashed_agent_uri(label: str, birth: str | None,
                                death: str | None) -> URIRef:
    normalised = unicodedata.normalize("NFKD", label).casefold()
    normalised = re.sub(r"[^\w\s]", "", normalised)
    normalised = " ".join(normalised.split())
    # Date suffix is the key disambiguation signal — same name, different
    # years → different person. See Open Q #1 for the merge-precision
    # rationale.
    date_token = f"|{birth or ''}-{death or ''}"
    digest = hashlib.sha1((normalised + date_token).encode("utf-8")).hexdigest()
    return URIRef(f"http://urn.fi/URN:NBN:fi:bib:agent:{digest}")
```

**The date suffix in the hash is load-bearing.** Without it, "Smith, John (1850-1910)" and "Smith, John (1920-1980)" — two distinct people separated by 70 years — collapse to one page; cataloguers reviewing the page would have to mentally disambiguate. Including `$d` in the hash splits them automatically when years are known. Agents without `$d` data hash on label-only (so "Korhonen, Anna" with no birth-year still aggregates across records). KANTO data has `$d` ~85% of the time for personal names; Helmet's pre-RDA records less often (~30% sampled). Worth a measurement on the corpus before locking in.

The URI namespace `http://urn.fi/URN:NBN:fi:bib:agent:` already exists for the sentinel `…:agent:unknown` (per CLAUDE.md committed identifiers). Extending it with hash-suffixed siblings is consistent with that precedent.

Every name-hash agent carries provenance triples:

```turtle
<agent:9c1a…> a bffi:Agent ;
              skos:prefLabel "Korhonen, Anna, 1857-1934"@fi ;
              rda:P50121 "1857" ;
              rda:P50120 "1934" ;
              bffi-prov:nameCollapsed "true"^^xsd:boolean ;
              bffi-prov:authorityResolved "false"^^xsd:boolean .
```

`bffi-prov:nameCollapsed` is the **non-authoritative flag the cataloguers asked for**. The Skosmos page template reads it and renders a prominent banner ("⚠ Tämä on nimitiivistys, ei auktorisoitu henkilö — sisältää mahdollisesti useita eri henkilöitä, joilla on sama nimi"). New term — extends the `bffi-prov:` namespace (ours per CLAUDE.md namespace discipline).

**Touchpoints**:
- `src/bffi_pipeline/stages/m2_post/correlator.py` (or new helper) — `$d` parser; runs at MARC→BIBFRAME post-processing so the parsed years are on the agent regardless of whether M9 reconciles.
- `src/bffi_pipeline/stages/m9/graph_mutate.py` — call site that currently keeps the raw URI on KANTO miss.
- `src/bffi_pipeline/provenance/vocab.py` — declare `nameCollapsed`, `authorityResolved`, `approximate`.
- `src/bffi_pipeline/uris.py` — `mint_name_hashed_agent_uri(label, birth, death)` helper.
- Provenance: every name-hash mint logs a `bffi-prov:NameCollapseActivity` so the rationale (no KANTO match + the date signal used) is auditable.

**Normalization choices** (need confirmation from cataloguers):
- NFKD normalize + casefold → "Korhonen, Anna" == "korhonen,anna" == "KORHONEN ANNA"
- Strip punctuation (commas, periods, parentheses) but preserve diacritics-after-NFKD
- Collapse whitespace
- **Include parsed birth/death years in the hash input** (above). Empty if absent.
- Does NOT split surnames/forenames — the cataloguer-typed inverted form ("Korhonen, Anna") and the natural form ("Anna Korhonen") hash differently. Could add a heuristic that detects the inversion via comma-presence, but pre-research: ~98% of Helmet personal-name 100/700 fields use the inverted form, so the gain is small and the risk of over-merging is non-trivial.

### Block 2 — Inverse triples for inbound link rendering (Skosify)

Skosmos's concept page query (`GenericSparql::generateConceptInfoQuery`) walks one hop in each direction. BFFI's contribution chain (`<work> bffi:contribution _:bnode . _:bnode bffi:agent <agent>`) puts the Work TWO hops behind the agent — Skosmos picks up the bnode (as `?sp ?uri ?op`) but stops there.

Add a Skosify pass that emits forward-direction triples on the agent:

```python
def _synthesise_agent_inverse_predicates(graph: Graph) -> int:
    for entity, contribution in graph.subject_objects(V.BFFI.contribution):
        entity_type = next(graph.objects(entity, V.RDF.type), None)
        inverse_pred = {
            V.BFFI.Work: V.BFFI_PROV.contributorToWork,
            V.BFFI.Expression: V.BFFI_PROV.contributorToExpression,
            V.BFFI.Manifestation: V.BFFI_PROV.contributorToManifestation,
        }.get(entity_type)
        if inverse_pred is None:
            continue
        for agent in graph.objects(contribution, V.BFFI.agent):
            graph.add((agent, inverse_pred, entity))
```

Three new terms in `bffi-prov:` (closed-namespace-rule: lkd.rdf has no inverse for `bffi:contribution`, so extend our own namespace per CLAUDE.md):

- `bffi-prov:contributorToWork`
- `bffi-prov:contributorToExpression`
- `bffi-prov:contributorToManifestation`

These render on the agent page as three sections — exactly the cataloguer-facing axis the user asked for.

**Touchpoints**:
- `src/bffi_pipeline/stages/m10/skosify_run.py` — new pass + flatteners-chain entry.
- `src/bffi_pipeline/provenance/vocab.py` — three new term declarations.
- `config/skosmos-config.ttl` — Finnish / Swedish / English labels for the three predicates.
- Tests: one per axis (Work / Expression / Manifestation) + one for the KANTO-reconciled vs name-hash split.

### Block 3 — `bffiAgents` Skosmos vocab + named graph

Add a new vocab to `config/skosmos-config.ttl`:

```turtle
:bffiAgents a skosmos:Vocabulary, void:Dataset ;
    dct:title "BFFI agents (Helmet)"@en ;
    skosmos:shortName "bffi-agents" ;
    skosmos:sparqlGraph <http://urn.fi/URN:NBN:fi:bib:graph:agents> ;
    skosmos:defaultLanguage "fi" ;
    skosmos:language "fi", "sv", "en" .
```

Two routing options for the data:

**Option 3A** — emit agents into a new named graph `bib:graph:agents` (alongside the existing `bib:graph:works` / `expressions` / `manifestations`). M10's loader gets a fourth load target.

**Option 3B** — keep agents in the same graph as bibliographic entities; let Skosmos's vocab `skosmos:concept` filter (`?uri a bffi:Agent`) scope the page. Slightly simpler M10 changes; more cross-vocab queries.

Recommended: **3A** (separate graph). Aligns with the existing per-class graph pattern; cleaner observability (counts per graph); side-steps the "agent shows up under Works vocab's search" pitfall.

**Touchpoints**:
- `config/skosmos-config.ttl` — new vocab block + labels for:
    - the three Block-2 inverse predicates (`bffi-prov:contributorTo*`)
    - the RDA birth / death year predicates (`rda:P50121` = "Syntymävuosi" / `P50120` = "Kuolinvuosi"). These are needed for **both** KANTO-reconciled agents (where the years already exist in Fuseki but render as unlabeled rows) and Block-1 name-hash agents (where B1a now emits them).
    - The `bffi-prov:nameCollapsed` / `authorityResolved` / `approximate` flags.
- `src/bffi_pipeline/stages/m10/load.py` — fourth named graph target.
- `config/fuseki-config.ttl` — verify text index covers the new graph (likely already does via `tdb:unionDefaultGraph`).
- `config/skosmos-overrides/concept-card.inc.twig` — top-of-page banner that fires when `bffi-prov:nameCollapsed "true"` is on the concept. One conditional block in the template.

## Open questions

1. **Cross-record name aggregation correctness threshold**. The proposed normalization (`label + $d-years → SHA-1`, see B1b) splits people with distinct lifespans correctly but still merges legitimately-distinct contemporaries with no `$d` data (and the ~70% of pre-RDA Helmet records with no `$d` at all). Acceptable per the user's stated goal ("an aggregation primitive, not authority work") but worth measuring: on a sample of 100 unreconciled name-hash agents, how often do the merged records look like the same person vs distinct people? Suggests a follow-up cataloguer-review TSV ranking name-hash agents by Work-count for human inspection. **Resolution metric**: estimate the corpus split between `$d`-present and `$d`-absent personal names in the first verification phase; if `$d`-absent dominates, the merge-precision story is weaker and the cataloguer banner needs sharper wording.

2. **VIAF promotion path**. Today's M9 chain: KANTO → VIAF → raw. With name-hash inserted before raw, a future VIAF promotion (re-reconciling existing name-hash agents when VIAF data improves) would need a migration step. Manageable — name-hash URIs are deterministic on `(label + $d-years)`, so the next run's M9 either still produces the same hash (no change) or hits a new VIAF match (mint the new URI, mark the old name-hash URI `owl:sameAs` for one run before deletion).

3. **Where do organisations (MARC 110 / 710) and conferences (111 / 711) fit?** Same shape, same merge problem — "Sibelius-Akatemia" appears under several spellings. Corporate bodies and conferences don't carry `$d` years, so the merge-precision angle in B1b doesn't help; the hash falls back to label-only and the cataloguer banner does more work. Suggests the proposal should cover all agent classes (Person, CorporateBody, Meeting), not just Person.

4. **Skosmos page UX for an agent with 500+ contributions**. Some prolific authors (Astrid Lindgren, Edward Elgar) will have thousands of attributed Manifestations after the full-corpus run. Skosmos's default concept page renders all values in one list; needs either client-side pagination (Twig template patch) or a cutoff with "see all" link to a SPARQL-backed lister. Defer to a follow-up — the basic page works at first; pagination is a polish item.

## Risks

- **Hash collisions across legitimately-different normalizations of similar names**. NFKD + casefold + strip-punctuation should be safe but worth a unit test on the Unicode edge cases (Finnish ä/ö, Swedish å, names with apostrophes like "O'Brien").
- **Re-run instability if normalization rules change**. Pinning the normalization algorithm to a versioned function is essential — every hash is a stable identifier across runs, and changing the algorithm reshuffles every name-hash URI in the graph. Suggests `mint_name_hashed_agent_uri_v1` with explicit version suffix and a future migration story.
- **Skosmos performance**. The inverse-predicate emission adds 3× the contribution-triple count (one per axis). At 850 k contributions × 3 = ~2.5 M new triples. Fuseki TDB2 handles this comfortably; the page-render query stays one-hop.

## Rollback

- Block 1 (name-hash URIs): one Skosify-stage-only re-run after deleting the new tier reverts to per-record raw URIs. No persisted state to migrate; URIs are derived.
- Block 2 (inverse triples): single-pass removal, no downstream consumers depend on these.
- Block 3 (Skosmos vocab): remove the vocab block from `config/skosmos-config.ttl` and restart Skosmos. The named graph stays in Fuseki harmless.

## Suggested next step

Confirm with the user:

1. **Block ordering**. Block 1 alone gives stable agent URIs but no agent page yet. Block 2 alone shows inverse links on KANTO agents but unreconciled agents stay per-record. Block 3 alone is just config. The MVP that delivers the user's stated goal is **Blocks 1 + 2 + 3 in one ship**.

2. **Normalization rule**. Block 1's hash input is `(NFKD-casefolded-label, parsed-birth-year, parsed-death-year)`. Confirm this is the right shape — the birth-year inclusion is what splits "Smith, John (1850-1910)" from "Smith, John (1920-1980)". Alternatives: label-only (more merging, simpler), or label + year + role-axis (more splitting, more brittle).

3. **Organisation / conference scope**. Confirm whether 110/710 (CorporateBody) and 111/711 (Meeting) should be included in Block 1 from the start (recommended: yes).

Then graduate to `docs/plans/backlog/` with phased verification checkpoints — Block 1 has a clear "agent count by tier" metric; Block 2 has a "inverse triples emitted per axis" count; Block 3 is operational verification (load Skosmos, click an agent page, see the banner + the three sections).
