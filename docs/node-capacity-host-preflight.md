# Node capacity host preflight

Before running the measured Node-capacity scenarios from Issue #13, capture a small read-only prerequisite snapshot on the intended load-test host:

```bash
python3 scripts/check-node-capacity-host-preflight.py > node-capacity-host-preflight.json
```

The helper succeeds only when the current machine is Linux, exposes a positive logical CPU count and `/proc/meminfo` `MemTotal`, and has a reachable local Docker server plus Docker Compose v2. It emits deterministic schema-v1 JSON containing only:

- OS family, machine architecture, and kernel release;
- logical CPU count and total host memory;
- Docker server and Docker Compose versions.

It intentionally does **not** collect hostname, username, environment variables, Docker labels, container configuration, credentials, or provider metadata. Docker is queried read-only with `docker version` and `docker compose version`; no container is started, stopped, or modified.

A successful preflight means only that the basic local prerequisites are present. It does not prove that the host matches an intended production Node class, does not establish a `node_profile` identity by itself, and does not establish capacity, safety margin, pass/fail thresholds, media mix, or a production `max_sessions` value. Those remain bound to the existing canonical load-plan, run-manifest, measured-report, coverage, and review-bundle evidence chain.

If the preflight fails, its exit status is `2` and it emits a bounded reason without forwarding raw Docker stderr. Treat that as a local QA setup problem and do not reinterpret it as measured capacity evidence.
