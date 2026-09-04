# Quality contract

Quality is a current-state workflow separate from authorship. There are exactly
three gates: `reader`, `visual`, and `factual`. Each gate has its own input
fingerprint and attempt namespace under `<run>/.agent-work/quality/`.

## State and attempts

The gate lifecycle is:

```text
active -> passed
active -> revision-required -> active -> passed or stopped
```

`quality-start` creates one current attempt. A protocol repair stays in the same
attempt and fingerprint. A new substantive cycle requires a changed current
fingerprint; the second cycle ends only in `passed` or `stopped`.

`<run>/quality-state.json` is the finalized current state. Attempts, packets,
result templates, receipts, merges, and the registry live below
`<run>/.agent-work/quality/`.

## Fixed budgets

| artifact or measure | limit |
| --- | ---: |
| generic packet UTF-8 bytes | 73,728 |
| generic packet estimated tokens | 24,000 |
| quality result UTF-8 bytes | 24,576 |
| merge artifact UTF-8 bytes | 24,576 |
| images in a visual packet | 8 |
| source pixels in a visual packet | 10,000,000 |
| cards in one curation shard | 30 |
| findings in one curation result | 8 |
| final semantic findings | 20 |
| full-resolution pages in one visual packet | 6 |
| pages in one factual packet | 6 |
| complete reader packet UTF-8 bytes | 163,840 |

The reader gate receives one full-deck packet. Its dedicated envelope is
checked after the result template and legal reader content are present; content
is never silently truncated. Visual and factual packets are sharded
deterministically by page order.

## Packet contents

The reader packet includes locale, mode, title, every page's `reader_copy`,
structured claim, presentation kind, visible identity owner, and deck
progression. Its result template covers every page once and contains complete
deck verdict fields.

The reader rubric treats an omitted caption as not applicable. If a caption is
present, the review must confirm that it has an explicit non-obvious-visual
reason and adds interpretation beyond the headline, support, claim, and visible
encoding; a generic or redundant caption is a `must-fix` finding.

Each visual packet contains a contact sheet, one to six full-resolution rendered
pages, layout rows, asset bindings, composition metadata, stable required page
IDs, and a result template. It contains at most eight image descriptors and at
most 10,000,000 source pixels.

Each factual packet contains reader-visible copy, claims, measure bindings, item
bindings, labels, and exact evidence closure for its pages. `allowed_evidence_ids`
is exactly the set of evidence records in that closure. Credentials, Steam IDs,
absolute paths, cache paths, unrelated evidence, and full private libraries are
prohibited.

The reader packet ID is `reader-deck`; visual packets are `visual-01`,
`visual-02`, and so on; factual packets are `factual-01`, `factual-02`, and so
on. Quality result files use the assignment packet ID.

## Submission and completion

`quality-submit` checks the final result byte budget before JSON schema, privacy,
or binding checks. Every accepted receipt binds gate, attempt, packet, packet
hash, result schema, input fingerprint, result byte count, and result hash.

Every assignment has one accepted result. Its page IDs match the packet exactly
once. `quality-finish` verifies that all packet page sets form the exact ordered
partition of the compiled deck, then merges findings and recomputes severity
using only `must-fix` and `polish`.

A failed verdict requires a locating `must-fix` finding. Fixed categories cannot
be downgraded. A first-cycle `must-fix` result or an over-limit `polish` result
becomes `revision-required`; the second substantive cycle becomes `stopped`. A
clean result with no more than two `polish` findings becomes `passed`.

`quality-status` reports freshness. `finalize-quality` requires current
deterministic validation, current evidence, current rendered output, and
current `passed` results for all three gates before writing
`quality-state.json`.
