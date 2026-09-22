# Reaper systemd contract

IRLight ships `deploy/systemd/irlight-reaper.service` and `deploy/systemd/irlight-reaper.timer` as the repository-side scheduling contract for the standalone reaper.

The service intentionally uses `docker compose exec -T control-ui python /app/reaper_cli.py` instead of creating a second Control Plane container. This keeps the reaper on the same mounted `STATE_DIR`, provider selection, and injected provider credentials as the running Control Plane. The unit is `Type=oneshot`, so systemd does not start a second copy while the previous invocation is still active.

The timer runs every five minutes. That interval must remain shorter than the reaper CLI's default 600-second provisioning timeout and substantially shorter than the default one-hour no-ingest timeout. `Persistent=true` asks systemd to make up a missed activation after a host downtime instead of silently skipping the sweep.

`tests/test_reaper_systemd_contract.py` protects these repository assumptions: the service must reuse the running `control-ui` container, avoid destructive Compose commands, keep a bounded execution timeout shorter than the timer interval, and keep the runbook commands aligned with the shipped unit names.

This contract test does **not** prove that the timer is installed or enabled on a real host, nor does it exercise a real ConoHa account. Runtime installation and the destructive/provider lifecycle checks remain the explicit steps in `docs/conoha-runtime-verification.md`. Those checks can create and delete paid provider resources, so they must not be run automatically from repository CI or without explicit operational approval.
