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

Issue #13 requires bandwidth-limited testing but does not define canonical bandwidth values. The matrix therefore keeps its 24-case baseline unchanged and adds bandwidth cases only when the operator explicitly supplies one or more bounded values. The same value must be supplied when selecting the case:

```bash
python scripts/network-fault-case-runner.py plan \
  --case rtmp-bandwidth-2500kbit-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --bandwidth-kbit 2500 \
  --pretty
```

`--bandwidth-kbit` is repeatable when a harness wants one manifest context containing several explicit rates. Duplicate values are deduplicated deterministically. Values outside the injector's 64..100000 kbit/s safety bound are rejected before any command execution.

## Bounded execution

Execution requires the same disposable namespace plus an explicit acknowledgement:

```bash
sudo python scripts/network-fault-case-runner.py apply \
  --case srt-bandwidth-2500kbit-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --bandwidth-kbit 2500 \
  --confirm-disposable-namespace
```

The runner executes only the matrix-generated namespaced argv. Each qdisc command has a 10-second process timeout and the injected fault duration is restricted to the injector's bounded QA durations. Once an apply command is attempted, cleanup is attempted even if apply itself errors, times out, or is interrupted, as well as after the normal fault wait. A cleanup failure is reported as failure rather than silently claiming the case completed safely.

Before execution, a supplied case is regenerated from its closed fault metadata. For a bandwidth case, the declared `bandwidth_kbit` is used to rebuild the matrix context and must reproduce the exact case ID, fault mapping, namespace/interface, apply argv, and cleanup argv. Mutating the bandwidth metadata or command cannot convert one generated case into another executable command.

The protocol component of a case ID is a workload label for QA evidence; `tc netem` affects traffic on the selected namespace interface and does not filter packets by RTMP or SRT protocol.

## Safety boundary

Use only a disposable network namespace prepared for fault testing. The runner deliberately does not create the namespace because namespace/veth/routing setup has a separate blast radius and ownership lifecycle. It does not implement DNS faults, route withdrawal, firewall/TCP-reset injection, or burst loss; those remain separate #13 slices with their own cleanup contracts. Bandwidth shaping uses only the already-bounded `netem rate` support from the injector and does not add a host-level shaper.

This runner records no credentials, destination URLs, packet contents, environment variables, or container logs. It does not contact external streaming providers and adds no external-cost behavior.
