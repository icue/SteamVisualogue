# Current workflow

The normal run order is:

```text
collect -> enrich -> derive -> palette
-> semantic-findings.json + deck-plan.json + art-direction.json
-> compile-deck
-> reader gate
-> assets
-> optional artwork inspection
-> build-visual-brief
-> render
-> visual gate + factual gate
-> finalize-quality
-> optional commit-reuse
```

The reader gate is completed before `assets`; this prevents artwork download
and rendering for a deck that still needs editorial repair. Artwork inspection
is optional. If it is not run or has no accepted current receipt,
`build-visual-brief` records an empty `accepted_inspections` list.

## Working directory and dependencies

Run commands from the project working directory. In this repository, that is
the directory containing `tests/` and `.agents/`. Keep the same working directory
throughout a run, including delegated script calls.

Set `$skillDir` to the absolute directory containing the loaded `SKILL.md`. For this
repository, use the following PowerShell commands; when the skill is installed
elsewhere, set `$skillDir` to that installation instead:

```powershell
$skillDir = (Resolve-Path -LiteralPath '.agents/skills/steam-visualogue').Path
python -m pip install -r "$skillDir/scripts/requirements.txt"
```

Install dependencies into the Python environment used for the run.

## Commands

Every run directory is created under `output/<run-name>` (or specified as `--run-dir <run-name>`, which automatically normalizes to `output/<run-name>`).

Run the source stages and author the current `semantic-findings.json`,
`deck-plan.json`, and `art-direction.json`:

```text
python -B "$skillDir/scripts/run.py" collect --identity <steam-id-or-profile-url> --run-dir output/<run-name>
python -B "$skillDir/scripts/run.py" enrich --run-dir output/<run-name>
python -B "$skillDir/scripts/run.py" derive --run-dir output/<run-name>
python -B "$skillDir/scripts/run.py" palette --run-dir output/<run-name>
```

Compile the editorial contract, then complete the reader gate before assets:

```text
python -B "$skillDir/scripts/run.py" compile-deck --run-dir output/<run-name>
python -B "$skillDir/scripts/run.py" quality-start --run-dir output/<run-name> --gate reader
python -B "$skillDir/scripts/run.py" quality-submit --run-dir output/<run-name> --attempt <attempt> --packet-id <packet>
python -B "$skillDir/scripts/run.py" quality-finish --run-dir output/<run-name> --attempt <attempt>
python -B "$skillDir/scripts/run.py" assets --run-dir output/<run-name>
```

To use a custom artwork directory, pass `--assets-dir <directory>` to `assets`;
subsequent commands for that run use the selected directory automatically.

For an artwork inspection, create the current bounded packets and accept every
assigned result before building the brief:

```text
python -B "$skillDir/scripts/run.py" packetize --run-dir output/<run-name> --stage artwork-inspection
python -B "$skillDir/scripts/run.py" accept-agent-result --run-dir output/<run-name> --packet-set <packet-set> --packet-id <packet> --result <result>
python -B "$skillDir/scripts/run.py" merge-agent-results --run-dir output/<run-name> --stage artwork-inspection
```

Build the visual input and render the current publish artifacts:

```text
python -B "$skillDir/scripts/run.py" build-visual-brief --run-dir output/<run-name>
python -B "$skillDir/scripts/run.py" render --run-dir output/<run-name>
```

Run both post-render gates, submit every assignment returned by
`quality-start`, and finish each matching attempt:

```text
python -B "$skillDir/scripts/run.py" quality-start --run-dir output/<run-name> --gate visual
python -B "$skillDir/scripts/run.py" quality-start --run-dir output/<run-name> --gate factual
python -B "$skillDir/scripts/run.py" quality-submit --run-dir output/<run-name> --attempt <attempt> --packet-id <packet>
python -B "$skillDir/scripts/run.py" quality-finish --run-dir output/<run-name> --attempt <attempt>
python -B "$skillDir/scripts/run.py" quality-status --run-dir output/<run-name>
python -B "$skillDir/scripts/run.py" finalize-quality --run-dir output/<run-name>
```

`render` writes deterministic validation, page renders, the contact sheet,
reader Markdown, and the output manifest. `validate` rechecks those current
artifacts when a separate validation run is needed. `commit-reuse` is available
only after final quality is current and passed; reuse never restores quality
state.

## Freshness

The compiler must see current evidence, semantic findings, the deck plan, and
localized labels. The asset manifest must be current before the visual brief is
built. Layout, render, validation, and output all carry the current compiled,
label, visual-brief, and layout-input fingerprints. A stale artifact is
regenerated at its owning boundary.

## Development verification

The source repository's `tests/` directory is development tooling and is not
part of the installed skill. When developing this skill, run the explicit suite
from the repository root; do not use unrestricted discovery:

```text
python -B -m unittest tests.test_visual_signals tests.test_skill_context_contract tests.test_semantic_candidates tests.test_reuse tests.test_rendering tests.test_quality_gate tests.test_progress tests.test_measurements tests.test_localization tests.test_editorial_deck tests.test_data_layer tests.test_credentials tests.test_contracts tests.test_context_budget tests.test_api_coordination tests.test_analytics tests.test_cli_boundaries tests.test_ribbon_composer tests.test_skill_packaging tests.test_asset_directory
```
