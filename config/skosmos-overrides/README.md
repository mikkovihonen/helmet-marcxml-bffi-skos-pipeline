# Skosmos Twig template overrides

Bind-mounted into the `bffi-skosmos` container via `docker-compose.yml`
to patch behaviour the stock Skosmos template gets wrong for our
BFFI canonical shape.

## Files

### `ConceptPropertyValue.php`

Mounted over `/var/www/html/src/model/ConceptPropertyValue.php`. One
behavioural change vs the upstream class (Skosmos 3.2): the
`getUri()` method detours through a project-specific predicate when
the underlying RDF resource is a blank node. Two detour predicates
are tried in order:

| Bnode shape | Detour predicate | Skosify pass that composes the label |
|---|---|---|
| `bffi:Contribution` | `bffi:agent` | `_synthesise_contribution_labels` |
| `bffi:Relation` | `bffi:associatedResource` | `_synthesise_relation_labels` |

```php
public function getUri()
{
    if ($this->resource->isBNode()) {
        $detourPredicates = array(
            '<http://urn.fi/URN:NBN:fi:schema:bffi:agent>',
            '<http://urn.fi/URN:NBN:fi:schema:bffi:associatedResource>',
        );
        foreach ($detourPredicates as $predicate) {
            $target = $this->resource->get($predicate);
            if ($target !== null && !$target->isBNode()) {
                return $target->getUri();
            }
        }
    }
    return $this->resource->getUri();
}
```

**Why.** BFFI's contribution chain is
`<work> bffi:contribution _:bnode . _:bnode bffi:agent <agent>`;
the relation chain is
`<entity> bffi:relation _:bnode . _:bnode bffi:associatedResource <hub>`.
Skosmos's default behaviour wraps either bnode value in `<a href>`
using the bnode's URI ("_:genid…"), which 404s. With this patch, the
link target becomes the agent / associated-resource URI (a real,
resolvable concept page) while `getLabel()` still walks
`skos:prefLabel` on the bnode — picking up the composed label
("Elgar, Edward (säveltäjä)" / "Julkaisuja (series)" etc.). Net
effect: the "Tekijyys" / "Liittyy" row renders
`<a href="target-uri">composed-label</a>`.

**Companion data-side trigger.** This override depends on the
`bffi:agent` triple being present on the in-memory EasyRdf resource
that Skosmos passes to `ConceptPropertyValue`. Skosmos's
`GenericSparql::generateConceptInfoQuery` only pulls a bnode's
outgoing properties (`?o ?oprop ?oval`) inside the OPTIONAL branch
gated by `?o rdf:value ?ov` — so by default a contribution bnode
arrives with just `rdf:type` + `skos:prefLabel` and `bffi:agent`
is missing. The `_synthesise_contribution_labels` Skosify pass
mirrors the composed prefLabel as `rdf:value` on every contribution
bnode, which trips that OPTIONAL and lets the deeper property fetch
happen. Without that trigger, this PHP override sees a near-empty
resource and falls through to the bnode URI (the link branch then
gets blocked by the Twig patch below). Keep the two in lock-step.

Other bnodes (notes, extent, tableOfContents) have no `bffi:agent`,
so `getUri()` returns the bnode URI as before; the template patch
below routes them to the text branch.

### `concept-card.inc.twig`

Mounted over `/var/www/html/src/view/concept-card.inc.twig`. One
behavioural change vs the upstream template (Skosmos 3.2):

| Line | Original | Patched |
|---|---|---|
| ~120 | `{% if propval.uri and property.type != 'rdf:type' %}` | `{% if propval.uri and property.type != 'rdf:type' and (propval.uri starts with '_:') == false %}` |

**Why.** Skosmos's `ConceptPropertyValue.uri` returns the literal
`"_:genid…"` for blank-node values. The original condition treats
that as a real resource URI and wraps it in `<a href="…">` — which
404s because bnode URIs aren't resolvable. The patched condition
forces the bnode into the literal/text branch so its
`skos:prefLabel` (added by `_synthesise_bnode_prefLabel_mirrors`
in `src/bffi_pipeline/stages/m10/skosify_run.py`) renders as
inline text.

**Coverage.** Affects every BFFI predicate whose value is a bnode
carrying a label — `bffi:note`, `bf:note`, `bffi:extent`,
`bffi:tableOfContents`, `bf:hasSeries`, `dct:isPartOf`,
`bf:language` / `bffi:language`, `bffi:role`, `bffi:contribution`,
`bffi:provisionActivity`, `bffi:classification`, and the rest
of the 12-predicate sweep in
`_BNODE_LABEL_PREDICATES`.

## Maintenance

Skosmos's upstream template can drift between releases. To re-sync:

```bash
# Extract the current upstream template from the container
docker exec bffi-skosmos cat /var/www/html/src/view/concept-card.inc.twig > /tmp/upstream.twig

# Diff against our override (the only patched lines should be the
# one-line condition change and the surrounding explanatory comment)
diff /tmp/upstream.twig config/skosmos-overrides/concept-card.inc.twig
```

Re-apply the patch if upstream changed the condition or the
surrounding template structure. Document any non-trivial diff in
this README.
