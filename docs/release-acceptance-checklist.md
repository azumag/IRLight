# Issue #13 pre-beta QA release acceptance checklist

This checklist is the fail-closed release-acceptance artifact for the QA scope in Issue #13. It does **not** replace the product Go/No-Go decision in Issue #14, the security/legal work in Issue #12, or the operations work in Issue #11.

The canonical machine-readable state is `docs/release-acceptance-checklist.json`. Run:

```sh
python scripts/validate-release-acceptance-checklist.py
python -m unittest tests.test_release_acceptance_checklist -v
```

## Status semantics

- `pending`: required evidence has not yet been produced.
- `partial`: useful evidence exists, but it does not yet satisfy the whole acceptance item.
- `blocked`: the acceptance item cannot currently be completed; the blocking reason belongs in `notes` and should be tracked in an Issue.
- `satisfied`: the item has repository evidence that is sufficient for the Issue #13 acceptance condition.

`qa_acceptance_ready` is deliberately fail-closed. The validator accepts `true` only when every required item is `satisfied`. A satisfied item must reference at least one existing repository file. Evidence paths are repository-relative, cannot escape the repository, and must resolve to regular files.

Repository evidence proves only what the referenced artifact actually demonstrates. In particular, FFmpeg CI must not be promoted into an OBS/mobile/hardware claim, local MediaMTX must not be promoted into Twitch/YouTube/Kick compatibility, and a short PR smoke run must not be promoted into a six-hour soak result.

## Current acceptance state

The initial checklist records existing RTMP/RTMPS/SRT and disconnect/degradation automation as `partial`, because those artifacts are real but do not by themselves close every Issue #13 scenario. Device compatibility, six-hour soak evidence, and measured Node capacity remain `pending`. Creating this machine-validated checklist itself satisfies the Issue #13 deliverable that a pre-beta acceptance checklist exist, but it does not make the QA scope ready.

## Updating an item

Before changing an item to `satisfied`:

1. Commit sanitized, reproducible evidence to the repository. Manual compatibility evidence should follow `docs/compatibility-testing.md` and `docs/compatibility-reports/README.md`.
2. Reference only the repository files that directly support the claim. Do not commit stream keys, SRT passphrases, tokens, private keys, credential-bearing URLs, or raw logs containing secrets.
3. Keep long-running acceptance evidence separate from normal PR smoke tests. `AGENTS.md` keeps ordinary Docker validation bounded; a six-hour soak should be an explicit acceptance run with a recorded result.
4. Run the validator and focused unit test above. Normal pull-request CI remains the merge gate.

If an external device, account, hardware encoder, or paid platform is unavailable, leave the item `pending` or `blocked` and record the decision in the relevant Issue instead of manufacturing evidence.
