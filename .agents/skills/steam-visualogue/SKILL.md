---
name: steam-visualogue
description: Turn a Steam library snapshot into an illustrated visual essay. Use when the user asks to create or update a Steam library portrait or visual report, including its analysis, layout, rendering, and quality review.
---

# Steam Visualogue

Steam Visualogue turns one Steam library snapshot into a deterministic,
reader-facing visual essay. Source access and private identity remain local;
published artifacts contain resolved reader copy, visuals, and public file
inventory.

## Standard run

Use the [workflow](references/workflow.md) for environment setup, command order,
freshness checks, gates, and development verification. Resolve bundled scripts,
references, and [JSON schemas](references/schemas/) relative to this `SKILL.md`.
Run commands from the project working directory. Every run directory and its output
are created under its `output/<run-name>`. The current artifact boundary is
`compiled-deck.json`: it is the only editorial artifact consumed by layout,
rendering, export, and quality. The layout artifact is `publish-layout.json`,
and final quality is written to `quality-state.json`.

## Subagent execution

Packetized analysis stages (`achievement-analysis`, `editorial-curation`, `editorial-synthesis`, `artwork-inspection`) and quality gate reviews (`reader`, `visual`, `factual`) produce isolated data packets. Agents must delegate packet inspection, finding generation, and rubric evaluations to parallel subagents following the fresh-context matrix in [agent context](references/agent-context.md).

## Reference loading

Load only the reference needed for the work:

- [data contracts](references/data-contracts.md) for artifact ownership,
  schema fields, and fingerprints;
- [agent context](references/agent-context.md) for privacy and bounded packets;
- [editorial method](references/editorial-method.md) for selection, narrative,
  and reader copy;
- [page roles](references/page-roles.md) for page structures and semantics;
- [localization](references/localization.md) for locale, labels, catalogs, and
  fonts;
- [visual system](references/visual-system.md) for art direction, layout, and
  rendering;
- [quality contract](references/quality.md) for reviewer packets, budgets,
  gates, and finalization.

## Hard boundaries

- During a skill run, every script and test that already exists in the working
  directory is read-only and must not be modified.
- Packetized stages and quality reviews must invoke parallel subagents for independent packet processing.
- `compile-deck` reads evidence, semantic findings, the authored deck plan, and
  current localized labels. It resolves the editorial contract and does not
  consume visual brief data.
- The reader gate passes before asset materialization, artwork download, or
  rendering. Asset work uses the current passed reader gate.
- `build-visual-brief` runs after current assets exist and may include only
  accepted artwork inspections from the current run. With no accepted
  inspection, `accepted_inspections` is empty.
- Layout and rendering require the current visual brief and all current source
  fingerprints. Publishing reads `compiled-deck.json`, not `deck-plan.json`.
- Captions are opt-in only: `deck-plan.json` may include `reader_copy.caption`
  only with `caption_required: true` and a specific `caption_reason`; the
  compiler rejects generic or redundant captions and strips the control fields
  before publishing.
- Quality work is bounded by `quality-start`, `quality-submit`,
  `quality-finish`, `quality-status`, and `finalize-quality`.
