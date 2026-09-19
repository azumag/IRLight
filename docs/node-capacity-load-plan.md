# Node capacity load-plan manifest

Issue #13 requires Node capacity to be measured rather than guessed. Before the measured trial recorder and canonical report can be used, the load harness needs a reproducible concurrency/scenario plan. `scripts/render-node-capacity-load-plan.py` provides that planning boundary without executing traffic or making an operational capacity decision.

The renderer always includes the Issue #13 baseline concurrency ladder `1, 2, 4, 8` and these required load categories: normal input, all Sessions simultaneously in HOLDING, reconnect storm, concentrated asset pre-fetch, and concurrent API/dashboard activity. Extra concurrency levels are explicit operator inputs and extend, rather than replace, the baseline ladder.

The renderer deliberately does **not** invent the exact 720p/1080p mix, acceptance thresholds, or safety margin. Those remain operator/test-policy inputs because Issue #13 requires 720p/1080p mixed testing but does not define a ratio or final pass/fail thresholds. Use a concrete one-line `--profile-label` that records what the harness will actually generate, and render separate plans when comparing different media mixes.

```bash
python3 scripts/render-node-capacity-load-plan.py \
  --profile-label '720p30/1080p30 mix: explicit harness profile qa-mix-v1' \
  --session-count 16 \
  --session-count 32 \
  > capacity-load-plan.json
```

The command is read-only and only writes JSON to stdout. It does not start publishers, call Docker, open network connections, provision infrastructure, contact a provider, use credentials, update scheduler inventory, choose `max_sessions`, or classify a trial as pass/fail.

## Validate a saved plan before a harness consumes it

`scripts/validate-node-capacity-load-plan.py` validates that a saved JSON plan still matches the exact canonical renderer contract before another tool or load harness consumes it:

```bash
python3 scripts/validate-node-capacity-load-plan.py capacity-load-plan.json --json
```

The validator rejects missing baseline levels, reordered or duplicated levels, scenario changes, unexpected fields, duplicate JSON keys, non-finite JSON constants, invalid UTF-8, and non-object roots. The input is limited to 256 KiB and must be a stable regular file. A final symlink or special file is refused, the file is opened without following a final symlink, and the device/inode/size/mtime/ctime identity is compared across open, bounded read, and the final pathname so path replacement or an in-place mutation fails closed instead of silently validating a mixed snapshot.

Validation does not turn a plan into evidence and does not execute any load. A valid result only means the manifest is one that the current repository renderer could have produced. It does not approve the media profile, acceptance thresholds, safety margin, provider cost, or production capacity.

The resulting manifest is a plan, not evidence. A real harness must execute the planned load against the intended Node profile, record each measured trial with `record-node-capacity-trial.py`, assemble it with `assemble-node-capacity-report.py`, and validate the final capacity report. The release checklist must remain pending until measured evidence and an explicitly approved safety margin exist.
