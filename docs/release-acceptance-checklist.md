# Issue #13 pre-beta QA release acceptance checklist

This checklist is the fail-closed release-acceptance artifact for the QA scope in Issue #13. It does **not** replace the product Go/No-Go decision in Issue #14, the security/legal work in Issue #12, or the operations work in Issue #11.

The canonical machine-readable state is `docs/release-acceptance-checklist.json`. Run:

```sh
python scripts/validate-release-acceptance-checklist.py
python -m unittest \
  tests.test_release_acceptance_checklist \
  tests.test_release_acceptance_checklist_depth \
  -v
```

The canonical checklist input itself is bounded to 128 KiB and must be a stable regular file. The validator rejects a final symlink, FIFO/device/non-regular input, an oversized file, or a pathname that changes to a different inode while the bounded read is in progress. Invalid UTF-8 and excessively nested JSON are reported as validation failures rather than escaping as tracebacks. These checks protect the release gate's input handling; they do not turn repository evidence into proof of an external or long-running test that was never actually performed.

## Status semantics

- `pending`: required evidence has not yet been produced.
- `partial`: useful evidence exists, but it does not yet satisfy the whole acceptance item.
- `blocked`: the acceptance item cannot currently be completed; the blocking reason belongs in `notes` and should be tracked in an Issue.
- `satisfied`: the item has repository evidence that is sufficient for the Issue #13 acceptance condition.

`qa_acceptance_ready` is deliberately fail-closed. The validator accepts `true` only when every required item is `satisfied`. A satisfied item must reference at least one existing repository file. Evidence paths are repository-relative, cannot escape the repository, and must resolve to regular files.

`six-hour-soak` has an additional semantic gate: when that item is marked `satisfied`, at least one referenced evidence file must pass the canonical schema-v1 validator in `scripts/validate-soak-report.py`, report `outcome == "pass"`, declare `target_duration_seconds >= 21600`, and record `observed_duration_seconds >= 21600`. Documentation, workflow definitions, or a shorter passing soak report may be supplementary evidence, but they cannot satisfy the six-hour item. The machine gate validates report structure, cleanup, pass status, and duration; it does **not** prove that the measurements came from a real six-hour run. Operators must still commit sanitized evidence from an explicit long-running acceptance run instead of fabricating a synthetic report merely to satisfy the gate.

`node-capacity-load` has an additional semantic gate: when that item is marked `satisfied`, at least one referenced evidence file must pass `scripts/validate-node-capacity-coverage-manifest.py`. A single canonical capacity report is no longer sufficient. The durable schema-v1 coverage manifest names the canonical load plan and exactly one report for every required scenario, then re-runs the complete coverage validator across those referenced files. This proves that every planned scenario and concurrency level is represented, that report scenario prefixes bind to the exact plan profile/scenario, that all reports use one Node profile, software revision, and safety margin, and that measured `run_id` values are distinct. Documentation, individual reports, and the plan itself may be supplementary evidence, but none can satisfy the item without a valid complete coverage manifest.

The coverage gate remains intentionally narrow. It verifies the repository evidence graph and the internal consistency of the measured reports; it does **not** prove that measurements are real, choose a media mix, acceptance threshold or safety margin, approve provider cost, update scheduler inventory, or change production `max_sessions`. Operators must still commit sanitized artifacts from the actual intended Node profile and approved workload rather than fabricating synthetic files merely to satisfy the machine gate.

Repository evidence proves only what the referenced artifact actually demonstrates. In particular, FFmpeg CI must not be promoted into an OBS/mobile/hardware claim, local MediaMTX must not be promoted into Twitch/YouTube/Kick compatibility, and a short PR smoke run must not be promoted into a six-hour soak result.

## Current acceptance state

The initial checklist records existing RTMP/RTMPS/SRT and disconnect/degradation automation as `partial`, because those artifacts are real but do not by themselves close every Issue #13 scenario. Device compatibility, six-hour soak evidence, and measured Node capacity remain `pending`. Creating this machine-validated checklist itself satisfies the Issue #13 deliverable that a pre-beta acceptance checklist exist, but it does not make the QA scope ready.

## Updating an item

Before changing an item to `satisfied`:

1. Commit sanitized, reproducible evidence to the repository. Manual compatibility evidence should follow `docs/compatibility-testing.md` and `docs/compatibility-reports/README.md`.
2. Reference only the repository files that directly support the claim. Do not commit stream keys, SRT passphrases, tokens, private keys, credential-bearing URLs, or raw logs containing secrets.
3. Keep long-running acceptance evidence separate from normal PR smoke tests. `AGENTS.md` keeps ordinary Docker validation bounded; `six-hour-soak` requires a canonical passing soak report from an explicit run whose target and observed duration are both at least 21,600 seconds.
4. For `node-capacity-load`, commit the canonical load plan, one sanitized canonical measured report for every required scenario, and a schema-v1 coverage manifest that binds them. Reference the coverage manifest in the checklist; a helper script, documentation file, plan, or single report is not sufficient evidence.
5. Run the validator and focused unit tests above. Normal pull-request CI remains the merge gate.

If an external device, account, hardware encoder, or paid platform is unavailable, leave the item `pending` or `blocked` and record the decision in the relevant Issue instead of manufacturing evidence.
