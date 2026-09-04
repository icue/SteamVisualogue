# Editorial presentation contracts

`deck-plan.json` contains 12–18 pages. Every page has `page`,
`narrative_move`, `reader_question`, `claim`, `reader_copy`, and one
`presentation` object. The presentation kind determines deterministic visual
binding. The authored plan does not provide display values, dimensions, units,
or arbitrary renderer metadata.

## Shared rules

- A page earns its place through one worthwhile relation, contrast, magnitude,
  pattern, anomaly, consequence, or synthesis.
- A comparison has one shared question, one shared dimension, one relationship
  claim ID, and exactly two parallel items.
- A game identity is exposed once per page and is owned by reader headline copy
  or by the renderer.
- A game may appear on at most two pages, with different presentation kinds and
  narrative purposes.
- Every nested evidence reference is repeated in the page evidence closure.
- Every quantitative visual value is a measure reference resolved from
  evidence.
- Qualitative comparison statements do not repeat the subject name and do not
  encode a number.
- The opening appears once; the closing appears once and introduces a new
  synthesis.

## Presentation kinds

| kind | binding | reader purpose |
| --- | --- | --- |
| opening | optional subject, statement, or reviewed raw visual | establish the report's lens |
| hero | one subject | give a work a consequential role |
| archive-density | denominator and count bins | quantify collection shape |
| evidence-ledger | 2–5 facts | connect compact evidence to a claim |
| quantitative-comparison | two subject/measure items | explain a meaningful difference |
| qualitative-comparison | two subject/statement items | contrast two experiences without chart encoding |
| series-atlas | group evidence and 3–4 members | reveal a within-series pattern or growth arc |
| pattern-atlas | group evidence and 3–4 members | connect related works |
| temporal-strata | 2–8 ordered measures | show a time structure, career milestone, or era strata |
| achievement-anomaly | one subject/achievement/statistic | make an unusual achievement pattern or sequence break legible |
| abstract-portrait | subject or reviewed raw visual | synthesize visually without a chart |
| closing | optional statement or reviewed raw visual | return with a new conclusion |

### Common pattern-to-role mappings

- **Behavioral and rarity anomalies** (`sequence_breaker_anomaly`, `near_complete_plateau`, `anti_mainstream_divergence`) $\rightarrow$ `achievement-anomaly` or `evidence-ledger`;
- **Dual-state contrasts** (`flow_friction_contrast`, `coop_vs_solo_polarization`, `genre_specialist_vs_tourist`) $\rightarrow$ `qualitative-comparison` or `quantitative-comparison`;
- **Chronological structures** (`peak_daily_burst`, `era_evolution_strata`) $\rightarrow$ `temporal-strata`;
- **Franchise progressions** (`same_series_group` with `growth_arc`) $\rightarrow$ `series-atlas`.

## Measure binding

A measure contains `evidence_id`, `fact`, and `format`. The compiler resolves
the authoritative value and records dimension, canonical unit, display value,
and numeric value in `measure_bindings`. Authors cannot declare a new dimension
or supply a competing value.

Series and pattern items keep subject and measure together. Group evidence
identifies the member set, every item belongs to that set, and comparable
measures share dimension and canonical unit.

## Atlas contracts

A `series-atlas` page is a reader-facing pattern, not a catalog. It uses one
series evidence record and three or four member items. Each item binds a current
game identity, one evidence-derived measure or fact, and a non-empty reader
`statement` explaining the shared pattern.

A `pattern-atlas` page connects three or four games through one declared pattern
evidence record. The claim explains the relationship across works, while each
item supplies one evidence-bound fact and a concise reader `statement`.

Both atlas kinds require unique subjects, explicit group membership, evidence
closure, comparable measures when present, and portrait assets named
`game:<appid>:portrait`. The renderer displays `statement`; it never reads a
`note` field for atlas copy. IDs, hashes, and role metadata remain machine-only.

## Comparison contracts

A `quantitative-comparison` page renders a corridor 3D perspective comparison between two games. Both subjects require vertical Steam portrait game assets named `game:<appid>:portrait` (aspect ratio 2:3, height > width) mapped losslessly into the 3D perspective walls ($220 \times 440$).

A `qualitative-comparison` page contrasts two game subjects without chart bars, requiring landscape Steam header artwork named `game:<appid>:header`.

## Anomaly contracts

An `achievement-anomaly` page renders a two-column anomaly breakdown. The game subject requires a vertical Steam portrait game asset named `game:<appid>:portrait` (aspect ratio 2:3, height > width) for the left card ($320 \times 480$), while the right column displays the achievement identity, key statistic measure, and contextual note.
