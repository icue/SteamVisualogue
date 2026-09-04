# Agent context contract

Source access and private identity stay inside the local run. Agents and
reviewers receive only the bounded current artifact assigned to their work.
Packet builders record the minimum safe evidence closure and current input
fingerprints.

## Privacy boundary

Credentials, complete libraries, raw cache data, source paths, Steam IDs, image
bytes, and base64 do not enter agent packets. Generic handoffs expose only
selected candidates, evidence cards, accepted findings, focused evidence, or
inspected artwork needed for the assignment. Results are schema-checked,
privacy-checked, budget-checked, and merged only when coverage and fingerprints
match.

The editorial handoff is current semantic findings plus the authored deck plan.
The compiler resolves evidence, labels, measures, identity ownership, and
reader-audit data into `compiled-deck.json`, the only editorial contract
consumed by publishing.

## Fresh-context matrix

| work | context | bounded view |
| --- | --- | --- |
| achievement analysis | one fresh context per non-empty packet | selected candidates |
| editorial curation | one fresh context per shard | evidence cards and accepted findings |
| editorial synthesis | fresh context when curation has multiple shards | bounded curation findings |
| artwork inspection | fresh contexts when the shortlist exceeds one packet | 4–8 images |
| reader quality | one fresh context | complete current compiled deck |
| visual quality | one fresh context per visual packet | contact sheet and 1–6 rendered pages |
| factual quality | one fresh context per packet | 1–6 pages and exact evidence closure |

Only a changed current input starts a new substantive quality cycle. Current
work does not import data from another run directory.
