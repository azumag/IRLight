# Node capacity host preflight

Before running the measured Node-capacity scenarios from Issue #13, capture a small read-only prerequisite snapshot on the intended load-test host:

```bash
python3 scripts/check-node-capacity-host-preflight.py > node-capacity-host-preflight.json
```

The helper succeeds only when the current machine is Linux, exposes a positive logical CPU count and `/proc/meminfo` `MemTotal`, and has a reachable **local Unix-socket Docker endpoint** plus Docker Compose v2. A remote `DOCKER_HOST` or remote Docker context is rejected before runtime version probes, so local CPU/memory facts cannot be accidentally combined with a different Docker host. It emits deterministic schema-v1 JSON containing only:

- OS family, machine architecture, and kernel release;
- logical CPU count and total host memory;
- Docker server and Docker Compose versions.

It intentionally does **not** collect hostname, username, environment variables, Docker endpoint paths, Docker labels, container configuration, credentials, or provider metadata. Docker is queried read-only with `docker context inspect`, `docker version`, and `docker compose version`; no container is started, stopped, or modified.

When a snapshot is persisted as evidence, validate the persisted bytes before using or reviewing them:

```bash
python3 scripts/validate-node-capacity-host-preflight.py node-capacity-host-preflight.json
```

The validator is read-only and fail-closed. It rejects symlinks, oversized or malformed JSON, duplicate keys, non-standard numeric constants, unknown top-level or nested fields, non-Linux snapshots, bool-as-int or non-positive resource values, and unsafe platform/Docker version strings. This keeps the persisted evidence contract aligned with the schema-v1 snapshot accepted before the load harness starts and prevents accidental hostname, Docker endpoint, or other unreviewed metadata from being treated as trusted capacity evidence.

A successful preflight means only that the basic local prerequisites are present. It does not prove that the host matches an intended production Node class, does not establish a `node_profile` identity by itself, and does not establish capacity, safety margin, pass/fail thresholds, media mix, or a production `max_sessions` value. Those remain bound to the existing canonical load-plan, run-manifest, measured-report, coverage, and review-bundle evidence chain.

If the preflight or persisted-evidence validation fails, its exit status is `2` and it emits a bounded reason. Treat that as a local QA/evidence problem and do not reinterpret it as measured capacity evidence.
