# Plans (rewrite branch)

Index of plans on the `rewrite` branch (the conversion-first pipeline reimplementation).

## Convention

- One plan per file: `docs/plans/p-NNN-<slug>.md` (three-digit zero-padded; flat structure — no sub-folders).
- Status is tracked **in this index**, not by sub-folder location. Update the status column when a plan moves between states.
- Filenames stay stable across status transitions (no `git mv`); the history of a single plan is the file's own `git log`.

## Status values

| Status | Meaning |
|---|---|
| **proposed** | Idea drafted; trade-offs on the record; no implementation yet. |
| **active** | At least one phase has shipped; work in progress. |
| **completed** | All phases done; remains in the index for reference. |
| **abandoned** | Dropped before completion; the file documents why. |

## Index

| Plan | Title | Status |
|---|---|---|

*(No plans yet on the `rewrite` branch. Plans can be imported from `main` as needed — `git show main:docs/plans/proposed/p-NN-<slug>.md` then write under the new naming.)*

## Importing from main

Documents on `main` can be brought across on demand:

```sh
# View a doc from main without checking it out
git show main:docs/plans/proposed/p-56-purge-bf-from-bffi-emit.md

# Bring it onto the rewrite branch under the new naming
git show main:docs/plans/proposed/p-56-purge-bf-from-bffi-emit.md > docs/plans/p-056-purge-bf-from-bffi-emit.md
git add docs/plans/p-056-purge-bf-from-bffi-emit.md
```

After importing, add the new file to the index above and update its status.
