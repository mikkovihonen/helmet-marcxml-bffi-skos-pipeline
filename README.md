# BFFI Pipeline — documentation package

This is the documentation set for the MARCXML → BFFI → Skosmos pipeline. Drop these files into the root of a new git repository and Claude Code will pick up the structure automatically.

## Layout

```
.
├── CLAUDE.md                              # short conventions file (read every session)
└── docs/
    ├── BUILD_PLAN.md                      # milestone-ordered build plan (M0–M13)
    ├── cataloguer-asks-fi.md              # Finnish version of cataloguer requests
    ├── validation-strategy.md             # five validation boundaries
    ├── local-inference.md                 # Apple Silicon / Ollama setup, models, throughput
    ├── marcxml-to-bffi-skosmos-pipeline.md# technical spec (what to build)
    ├── external-dependencies.md           # records to request from Helmet cataloguers
    └── ci-strategy.md                     # CI rationale and PR template
```

## How to use this package

1. Create your repo (e.g. `bffi-pipeline`) and copy these files into the root.
2. Initialize git, commit the docs as the first commit.
3. Open the repo with Claude Code: `claude .`
4. Start with: *"Read CLAUDE.md and docs/BUILD_PLAN.md. Implement M0. Stop after the definition of done is met."*
5. Verify, commit, then proceed milestone by milestone.

## File sizes

The split follows current Claude Code best practices (~100-line CLAUDE.md, with progressive disclosure into focused sub-documents). Claude Code reads `CLAUDE.md` on every session; sub-documents are loaded only when relevant.

```
CLAUDE.md                              ~60 lines  (~700 tokens)
docs/BUILD_PLAN.md                     ~440 lines (loaded when starting a milestone)
docs/validation-strategy.md            ~120 lines (loaded for validation work)
docs/local-inference.md                ~80 lines  (loaded for M6 work)
docs/external-dependencies.md          ~95 lines  (loaded at M5/M9/M10 gates)
docs/ci-strategy.md                    ~75 lines  (loaded for CI work)
docs/marcxml-to-bffi-skosmos-pipeline.md    (the spec, unchanged)
```

The total content is the same as the original monolithic CLAUDE.md, but each session only loads what's relevant to the current task.

## Adapting to your environment

A few things you'll want to tailor:

- **Hardware:** the docs assume a MacBook Pro M5 Max with 128 GB. If your hardware differs, update the memory budget in `docs/local-inference.md` and the throughput planning tables.
- **Model versions:** Qwen3 32B/72B are the committed primary/cascade models. If a clearly better multilingual model ships before you start M6, swap it in `CLAUDE.md`'s tech stack table and `docs/local-inference.md`.
- **License copyright line:** `LICENSE` will be created during M13 with `Copyright (c) <year> University Of Helsinki (The National Library Of Finland)`. Adjust to your contribution path.
- **URN namespace:** `http://urn.fi/URN:NBN:fi:bib:work:` is committed but should be coordinated with NLF before any production publish (Ask 4 in `docs/external-dependencies.md`).
