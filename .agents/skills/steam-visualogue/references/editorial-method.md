# Editorial method

The main Agent receives compact semantic findings, not the source ledger. This
method turns evidence-closed findings into a concrete reader contract. The
[page roles](page-roles.md) define the structure used by the resulting plan.

## Selection and lenses

Balance curation across four narrative lenses to avoid single-dimension concentration:

- **Macro landscape**: collection shape, playtime concentration, and era spans;
- **Playstyle and flow**: behavioral anomalies (sequence breakers, 99% plateau, rarity divergence), and flow friction contrasts;
- **Temporal archaeology**: single-day bursts, multi-year return spans, and career milestones;
- **Series and work atlas**: same-series growth arcs, coop vs. solo polarization, and genre specialist vs. tourist contrasts.

Keep one observation per beat. A candidate is not a player diagnosis. A rarity
percentage measures frequency, a missing common achievement is not proof of
intent, and a timestamp gap is not proof of continuous play. State only observable
structural and behavioral facts:
- Describe achievement anomalies through observable unlock and rarity thresholds (e.g. "Base progression achievements with >85% global completion remain unlogged while advanced milestone achievements <5% are completed");
- Describe flow friction contrasts through recorded playtime distributions across high-penalty and low-penalty genres;
- Describe series growth through release-ordered playtime and completion ratios.

## Story modes

Use thesis-led when one dominant tension (e.g. friction spectrum or breadth vs. depth) unites the deck.
Use constellation-led when multiple distinct lenses (e.g. era evolution, burst rhythm, sequence breaking) provide independent observations. Write
11–17 beats plus a closing note, arrange an intentional progression, and make
the closing a synthesis or final concrete observation. The authored plan uses
the current role contracts.

Every factual beat carries evidence IDs from the compact merge or focused
evidence result. Numeric copy uses evidence tokens; names use game or
achievement tokens so the page validator can require matching artwork. Numbers
that drive bars, bins, or other visual encodings use structured measures, never
prose values. Do not invent numbers, causal explanations, or coverage claims.

## Reader-copy jobs

Every visible copy field has one distinct editorial job:

- `headline` gives the page conclusion or concrete observation first;
- `support` adds evidence, context, or contrast not already in the headline or
  visual;
- `caption` interprets a non-obvious image or encoding and is omitted when the
  visual is self-explanatory. It is opt-in only: an authored caption must carry
  `caption_required: true` and a non-empty `caption_reason` explaining what the
  reader cannot infer from the visual alone; the compiler removes these control
  fields from the published reader copy;
- a role-specific `note` or `statement` adds a new fact, contrast, or
  synthesis and never describes assembly;
- a game's localized name is the sole identity label for that subject;
- omission is preferable to a field that only fills a slot.

Delete or rewrite copy that repeats a claim, repeats a visible label or measure,
narrates construction or provenance, explains reviewer or model behavior,
states a defensive non-inference, or merely says that an item is an example.
A cover opens with a finding, tension, or concrete invitation. A closing
develops the intervening evidence into a new synthesis or final observation.

The complete reader-copy review happens after localization and measure
resolution. It checks information gain within each page and across the deck
before artwork is downloaded or rendering begins. That review is the reader
gate required by the [current workflow](workflow.md).
