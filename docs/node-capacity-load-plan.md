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

The resulting manifest is a plan, not evidence. A real harness must execute the planned load against the intended Node profile, record each measured trial with `record-node-capacity-trial.py`, assemble it with `assemble-node-capacity-report.py`, and validate the final capacity report. The release checklist must remain pending until measured evidence and an explicitly approved safety margin exist.
