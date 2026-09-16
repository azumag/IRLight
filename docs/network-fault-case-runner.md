# Deterministic network fault case runner

Issue #13 defines repeatable RTMP/SRT network-fault testing. `scripts/network-fault-matrix.py` produces stable case IDs and `scripts/network-fault-case-runner.py` selects or executes exactly one of those cases.

The runner is intentionally narrow. It does not create network namespaces, move interfaces, configure routes, start media workloads, or target a host interface. All generated `tc` commands remain inside the explicitly named disposable Linux network namespace enforced by the existing injector.

## Read-only planning

Inspect one baseline case before changing qdisc state:

```bash
python scripts/network-fault-case-runner.py plan \
  --case rtmp-loss-3pct-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --pretty
```

Unknown or mismatched case IDs fail closed. Loss and latency IDs use the selected matrix profile duration, which defaults to 30 seconds. Disconnect IDs remain the fixed 10/30/120/600-second cases from the baseline matrix.

Issue #13 requires jitter testing but does not define canonical jitter values. The matrix therefore keeps its 24-case baseline unchanged and adds jitter cases only when the operator explicitly supplies a bounded `LATENCY_MS:JITTER_MS` pair. The base latency must be one of the injector's 50/100/300/1000 ms values and jitter must be positive and no greater than that latency:

```bash
python scripts/network-fault-case-runner.py plan \
  --case rtmp-jitter-100ms-20ms-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --jitter-profile 100:20 \
  --pretty
```

`--jitter-profile` is repeatable when a harness wants several explicit profiles in one manifest context. Duplicate pairs are deduplicated deterministically. The selected matrix duration applies to explicit jitter cases, so a 120-second profile uses an ID such as `rtmp-jitter-100ms-20ms-120s`.

Issue #13 also requires bandwidth-limited testing but does not define canonical bandwidth values. Bandwidth cases likewise remain opt-in:

```bash
python scripts/network-fault-case-runner.py plan \
  --case rtmp-bandwidth-2500kbit-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --bandwidth-kbit 2500 \
  --pretty
```

`--bandwidth-kbit` is repeatable. Duplicate values are deduplicated deterministically. Values outside the injector's 64..100000 kbit/s safety bound are rejected before any command execution.

## Bounded execution

Execution requires the same disposable namespace plus an explicit acknowledgement. For example, an explicit jitter case can be applied as follows:

```bash
sudo python scripts/network-fault-case-runner.py apply \
  --case srt-jitter-100ms-20ms-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --jitter-profile 100:20 \
  --confirm-disposable-namespace
```

A bandwidth case uses the same execution boundary:

```bash
sudo python scripts/network-fault-case-runner.py apply \
  --case srt-bandwidth-2500kbit-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --bandwidth-kbit 2500 \
  --confirm-disposable-namespace
```

The runner executes only the matrix-generated namespaced argv. Each qdisc command has a 10-second process timeout and the injected fault duration is restricted to the injector's bounded QA durations. Once an apply command is attempted, cleanup is attempted even if apply itself errors, times out, or is interrupted, as well as after the normal fault wait. A cleanup failure is reported as failure rather than silently claiming the case completed safely.

Before execution, a supplied case is regenerated from its closed fault metadata. For jitter cases, both `latency_ms` and `jitter_ms` are used to rebuild the matrix context; for bandwidth cases, `bandwidth_kbit` is used. The regenerated case must reproduce the exact case ID, fault mapping, namespace/interface, apply argv, cleanup argv, and closed field set. Mutating a jitter value, bandwidth value, or generated command cannot convert one case into another executable command.

The matrix manifest schema is version 3 because explicit jitter support adds the top-level `jitter_profiles` field and per-case `jitter_ms` fault field. Consumers that validate the manifest shape can distinguish it from the earlier bandwidth-only schema.

The protocol component of a case ID is a workload label for QA evidence; `tc netem` affects traffic on the selected namespace interface and does not filter packets by RTMP or SRT protocol.

## Safety boundary

Use only a disposable network namespace prepared for fault testing. The runner deliberately does not create the namespace because namespace/veth/routing setup has a separate blast radius and ownership lifecycle. It does not implement DNS faults, route withdrawal, firewall/TCP-reset injection, or burst loss; those remain separate #13 slices with their own cleanup contracts. Jitter and bandwidth shaping reuse only the already-bounded `netem` support from the injector and do not add a host-level shaper.

This runner records no credentials, destination URLs, packet contents, environment variables, or container logs. It does not contact external streaming providers and adds no external-cost behavior.
