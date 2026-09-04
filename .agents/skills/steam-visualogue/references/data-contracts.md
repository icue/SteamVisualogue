# Data and artifact contracts

Deterministic scripts own source access. Canonical writers emit UTF-8 JSON with
stable keys and a trailing newline. Other writers preserve valid schema data.

## Artifact ownership

| artifact | owner | purpose |
| --- | --- | --- |
| `run-config.json` | run setup | report locale and run identity |
| `profile.json` | collect/enrich | normalized private library snapshot |
| `signals.json` | derive | language-neutral derived signals |
| `evidence.json` | derive | evidence ledger and source facts |
| `semantic-findings.json` | semantic analysis | bounded evidence-closed findings |
| `deck-plan.json` | editorial authoring | authored editorial contract |
| `localized-labels.json` | label materialization | current locale/catalog labels |
| `compiled-deck.json` | `compile-deck` | resolved editorial contract for publishing |
| `art-direction.json` | visual authoring | palette, density, rhythm, and closure |
| `visual-signals.json` | palette/visual analysis | sampled visual input |
| `visual-brief.json` | brief builder | bounded visual candidates and policy |
| asset directory's `manifest.json` | asset materialization | checked local asset inventory |
| `publish-layout.json` | layout composer | page geometry and machine metadata |
| `output/` | renderer/exporter | pages, render inventory, story, and public manifest |
| `.agent-work/quality/` | quality workflow | packets, results, receipts, merges, registry |
| `quality-state.json` | finalizer | current finalized quality state |

Every run directory and its intermediate artifacts are anchored inside `output/<run-name>/`.
`compiled-deck.json` is the editorial/publishing boundary. Layout, rendering,
export, and quality receive its resolved copy, evidence closure, localized
labels, measures, identity ownership, and page bindings; they do not reread
the authored deck plan.

## Required current fields

`compiled-deck.json` carries `locale`, `catalog_version`, `label_fingerprint`,
`deck_schema_fingerprint`, and `compiled_deck_fingerprint`. Each page has a
narrative move, reader question, structured claim, reader-visible copy, and
presentation; authoring-only caption controls do not cross this boundary. The
page count and role semantics are defined by
the [page roles](page-roles.md).

`localized-labels.json` carries `report_locale`, `catalog_version`,
`label_fingerprint`, `games`, `achievements`, and `failures`. A label document is
current only when locale, catalog version, exact referenced game and achievement
keys, schema, and its label fingerprint all match the current plan.

`visual-brief.json` carries `evidence_fingerprint`, `visual_fingerprint`,
`compiled_deck_fingerprint`, `asset_manifest_fingerprint`, and its own
`visual_brief_fingerprint`, plus palette, sampling, `candidate_assets`,
`accepted_inspections`, `deck_policy`, and `role_contracts`. It is a visual
input, not an editorial compilation artifact.

`publish-layout.json` carries `locale`, `catalog_version`,
`label_fingerprint`, `deck_schema_fingerprint`, `compiled_deck_fingerprint`,
`visual_brief_fingerprint`, `layout_input_fingerprint`, `font_families`,
`pages`, and machine-only metadata. Each page contains reader-facing elements,
asset references, evidence hash metadata, and actual layout metrics.

The output manifest carries the same locale/catalog/label/deck/compiled/brief/
layout fingerprints and treats `pages` as the rendered file inventory.
`page_semantics` is machine-only and contains page role, claim/evidence hashes,
narrative movement, visible game IDs, and asset IDs. Reader Markdown contains
the report title plus each page's resolved reader copy.

## Fingerprint chain

The current fingerprints are:

- evidence fingerprint from report-relevant profile data;
- visual fingerprint from sampled visual signals and evidence linkage;
- label fingerprint from locale, catalog version, and materialized labels;
- compiled-deck fingerprint from the canonical compiled deck content;
- asset-manifest fingerprint from the canonical asset manifest;
- visual-brief fingerprint from the visual brief with only its self field
  excluded;
- layout-input fingerprint from exactly four inputs: compiled-deck fingerprint,
  canonical `art-direction.json`, visual-brief fingerprint, and the canonical
  asset manifest.

Render, validation, output, and quality artifacts inherit the current fields.
No font bytes or unrelated run metadata enter `layout_input_fingerprint`.

## Atlas evidence closure

Series and pattern atlas pages contain three or four unique game subjects, one
non-empty reader `statement` per item, portrait assets named
`game:<appid>:portrait`, and evidence closure for the group and every member.
Comparable item measures use the same dimension and canonical unit. The
page-role rules in [page-roles](page-roles.md) own the reader-facing structure;
evidence IDs and hashes remain machine-only.
