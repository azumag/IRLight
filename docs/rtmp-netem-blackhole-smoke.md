# RTMP netem blackhole smoke

The public CI entry point is `scripts/smoke-rtmp-netem-blackhole.sh`. It is a quarantine wrapper around `scripts/smoke-rtmp-netem-blackhole-core.sh`.

The core exercise handles generated ingest credentials, a credential-bearing publisher URL, raw Session/event/internal-node responses, and service logs. The wrapper therefore runs the core with stdout and stderr redirected to a run-local file created under a `umask 077` temporary directory and never replays that captured output to CI. It emits only a fixed success marker or the fixed `rtmp-netem-blackhole-quarantined` failure marker, forwards `INT`/`TERM` to the core process, and removes the temporary directory on exit.

CI workflows must invoke only the public wrapper. The dedicated RTMP netem blackhole workflow watches both wrapper and core paths so changes to the behavior implementation still exercise the quarantine boundary.

The behavioral contract remains in the core: establish a `LIVE` / relay-online baseline, inject 100% RTMP publisher packet loss, require `HOLDING` with an expected continuity reason while the publisher and relay remain alive, clear the fault, return to `LIVE`, and verify the recovery event sequence.
