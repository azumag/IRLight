# Central Compose smoke failure diagnostics

`scripts/smoke-compose.sh` is the public entrypoint for the central Docker smoke. The smoke creates a short-lived ingest credential and passes it to a publisher, so any component regression can potentially echo that credential while reporting a failed connection.

To keep the CI boundary fail-closed, the public entrypoint runs the existing smoke implementation in `scripts/smoke-compose-core.sh` and captures all of its stdout/stderr in a mode-private temporary directory. The captured output is never replayed to CI, including on failure. If the inner smoke succeeds, the wrapper emits only a fixed success line. If it fails, the wrapper emits only a fixed failure annotation with the allowlisted stage token `central-compose-quarantined`, so the Docker smoke suite can retain machine-searchable failure classification without copying arbitrary diagnostics.

The one exception is the intentional overlapping-run isolation test. That test needs to distinguish the expected fixed-host-port collision from an unrelated failure. The wrapper therefore searches the private captured file for a small allowlist of known Docker port-collision phrases and emits a normalized fixed message. It never prints the matching input line.

The wrapper forwards INT/TERM to the inner smoke so the inner per-project cleanup trap still runs, and then removes its own private diagnostic directory. The inner implementation remains responsible for the existing Compose project isolation, runtime assertions, credential checks, media behavior and cleanup semantics.

This quarantine is intentionally conservative. It trades detailed central-smoke failure logs for a strict guarantee that generated ingest credentials are not replayed by the public smoke entrypoint. Individual smoke scripts that can enumerate their generated secrets should continue to prefer targeted redaction plus fail-closed withholding, as documented in `docs/smoke-test-isolation.md`.
