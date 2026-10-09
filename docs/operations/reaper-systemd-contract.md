# Reaper systemd contract

IRLight ships `deploy/systemd/irlight-reaper.service` and `deploy/systemd/irlight-reaper.timer` as the repository-side scheduling contract for the standalone reaper.

The service intentionally uses `docker compose exec -T control-ui python /app/reaper_cli.py` instead of creating a second Control Plane container. This keeps the reaper on the same mounted `STATE_DIR`, provider selection, and injected provider credentials as the running Control Plane. The unit is `Type=oneshot`, so systemd does not start a second copy while the previous invocation is still active.

The in-process Node authority root is resolved by the same rule as the Control Plane and the alert/inspection tooling: `NODE_STATE_DIR`, else `STATE_DIR`, else `/state`. A reaper invoked with only `STATE_DIR` set therefore reads the same `nodes.json` generation as the Control Plane that owns it; it must not fall back to a literal `/state`, which would let a sweep enforce heartbeats from a foreign Node generation (or silently disable heartbeat enforcement) while cleaning up Sessions from a different state root. `tests/test_reaper_node_heartbeat.py` pins the resolution and asserts parity with `node_internal`.

The timer runs every five minutes. That interval must remain shorter than the reaper CLI's default 600-second provisioning timeout and substantially shorter than the default one-hour no-ingest timeout. `Persistent=true` asks systemd to make up a missed activation after a host downtime instead of silently skipping the sweep.

Each reaper sweep samples its wall clock once, requires that sample to be a finite non-negative number, and reuses it for timeout decisions, orphan grace, and reaper-generated event timestamps. Reaper timeout/grace configuration is likewise required to be finite and non-negative. An invalid clock or duration fails closed before provider cleanup or Session mutation starts. Unix epoch `0.0` remains valid; this contract does not add a future-skew or timestamp-ordering policy.

`tests/test_reaper_systemd_contract.py` protects these repository assumptions: the service must reuse the running `control-ui` container, avoid destructive Compose commands, keep a bounded execution timeout shorter than the timer interval, and keep the runbook commands aligned with the shipped unit names.

This contract test does **not** prove that the timer is installed or enabled on a real host, nor does it exercise a real ConoHa account. Runtime installation and the destructive/provider lifecycle checks remain the explicit steps in `docs/conoha-runtime-verification.md`. Those checks can create and delete paid provider resources, so they must not be run automatically from repository CI or without explicit operational approval.
