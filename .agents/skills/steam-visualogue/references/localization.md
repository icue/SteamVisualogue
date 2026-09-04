# Report localization contract

Each run chooses exactly one report locale: `en-US` or `zh-CN`. The canonical
locale is written to `run-config.json` before collection and carried through
compilation, layout, rendering, validation, export, and quality fingerprints.

## Data boundary

Acquisition, enrichment, analytics, evidence, palette, and source fingerprints
remain language-neutral. Core Steam enrichment uses English source data. Report
copy is authored in the selected language; the pipeline does not translate an
English report after the fact.

Visible entity names are evidence tokens:

```text
{{game:<appid>#name|text}}
{{achievement:<appid>:<api_name>#name|text}}
{{achievement:<appid>:<api_name>#description|text}}
```

`compile-deck` scans the current plan and materializes `localized-labels.json`.
`en-US` resolves canonical evidence without network calls. `zh-CN` requests
only referenced Store game names and achievement schemas, then falls back per
entity to canonical English when a localized value is missing. Localized values
never overwrite profile, evidence, or cache source tables.

The label artifact contains canonical IDs, final values, provenance, failure
records, locale, `catalog_version`, and a stable `label_fingerprint`. It
contains no SteamID, API key, URL, request detail, or source path. A current
label artifact must match the plan's exact game and achievement references;
otherwise it is rematerialized before compilation.

## Reader language and fonts

The locale catalog owns renderer chrome, fallback labels, Markdown headings,
contact-sheet labels, typography metadata, and publishable-copy patterns.
Layout, render manifests, output manifests, and validation reports agree on
locale, catalog version, and label fingerprint.

`zh-CN` uses CJK-aware wrapping: closing punctuation cannot start a line,
opening punctuation cannot end one, and long Latin game titles remain intact
when possible. `en-US` wraps on words and splits only an overlong token. Date
presentation is locale-aware while evidence semantics, precision, and units
remain unchanged.

The final layout records the selected regular and bold font families. A
compatible CJK font is required for `zh-CN`; English may use the recorded
Pillow default when no preferred font is available. Validation rejects renderer
chrome in the wrong language and never treats localized reader labels as
private identity data.
