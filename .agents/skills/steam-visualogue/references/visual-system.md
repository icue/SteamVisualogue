# Publish visual system

The visual system consumes `compiled-deck.json`, current `art-direction.json`,
the current asset manifest, and `visual-brief.json` to produce
`publish-layout.json`. The [page roles](page-roles.md) own page semantics; this
document owns deterministic visual binding and actual geometry.

## Composition contract

Each page records the fixed composition family used by its presentation kind.
Quantitative comparisons use bars; qualitative comparisons use equal cards when
the measured pair is safe and stacked cards otherwise. Atlas pages use a fixed
grid, archive pages use a waffle, ledgers use rows, temporal pages use rows,
and anomalies use a split.

The composer measures the actual source geometry before choosing the qualitative
card family. Linear image enlargement is capped at 1.5×. Equal-card layouts
share geometry; unsafe source proportions use the stacked geometry family.

Page elements are reader-facing text, images, or decorative marks. Machine-only
metadata records role, claim ID, narrative move, and evidence hash outside the
visible elements. No private identity field enters the layout.

## Color and palette contract

The layout applies deterministic color binding per page. Pages featuring a
single game artwork derive their page background and palette directly from that
artwork's dominant colors in the asset manifest, keeping background luminance
comfortably dark for text contrast while reflecting the game's distinctive hue
and accent tones. Pages without a single game artwork use the global palette
from `art-direction.json`. All decorative marks serve distinct layout roles.

## Actual layout metrics

`layout_metrics.card_content` records measured card-level `content_bbox` and
`occupancy_ratio`. The same object records `visible_text_count`,
`visible_image_count`, and `lower_anchor`. Validation uses these actual metrics
to reject sparse cards and pages without a lower-half anchor. There is no
overall content box or overall occupancy field.

Every displayed measure comes from the compiled measure binding and is formatted
by the locale formatter. Atlas cards display the item `statement` and never
render a `note` as a substitute.

Generated artwork is optional. It must pass integrity and review checks, remain
associated with the current asset ID, and keep its source prompt out of reader
content. The renderer emits numeric page filenames and the contact sheet uses
reader titles only.
