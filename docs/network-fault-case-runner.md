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

Issue #13 tracks burst-loss testing but does not define a canonical correlation. The matrix therefore keeps its 24-case baseline unchanged and adds correlated-random-loss cases only when an operator explicitly supplies a bounded `LOSS_PERCENT:CORRELATION_PERCENT` pair. Loss must reuse one of the existing 1/3/5/10% values and correlation is bounded to 1..99%:

```bash
python scripts/network-fault-case-runner.py plan \
  --case rtmp-burst-loss-5pct-corr-75pct-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --burst-loss-profile 5:75 \
  --pretty
```

The generated command uses `tc netem loss random 5% 75%`. This is a correlated-random-loss model for burstiness testing, not a fixed burst-length or Gilbert-Elliott model. `--burst-loss-profile` is repeatable and duplicate pairs are deduplicated deterministically.

Issue #13 requires jitter testing but does not define canonical jitter values. The matrix likewise adds jitter cases only when the operator explicitly supplies a bounded `LATENCY_MS:JITTER_MS` pair. The base latency must be one of the injector's 50/100/300/1000 ms values and jitter must be positive and no greater than that latency:

```bash
python scripts/network-fault-case-runner.py plan \
  --case rtmp-jitter-100ms-20ms-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --jitter-profile 100:20 \
  --pretty
```

`--jitter-profile` is repeatable when a harness wants several explicit profiles in one manifest context. Duplicate pairs are deduplicated deterministically. The selected matrix duration applies to explicit burst-loss and jitter cases, so a 120-second burst profile uses an ID such as `rtmp-burst-loss-5pct-corr-75pct-120s`.

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

Execution requires the same disposable namespace plus an explicit acknowledgement. For example, an explicit burst-loss case can be applied as follows:

```bash
sudo python scripts/network-fault-case-runner.py apply \
  --case srt-burst-loss-5pct-corr-75pct-30s \
  --namespace irlight-qa \
  --interface eth0 \
  --burst-loss-profile 5:75 \
  --confirm-disposable-namespace
```

A jitter case uses the same execution boundary:

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

Before execution, a supplied case is regenerated from its closed fault metadata. For burst-loss cases, both `burst_loss_percent` and `burst_correlation_percent` rebuild the matrix context; for jitter cases, both `latency_ms` and `jitter_ms` are used; for bandwidth cases, `bandwidth_kbit` is used. The regenerated case must reproduce the exact case ID, fault mapping, namespace/interface, apply argv, cleanup argv, and closed field set. Mutating a burst correlation, jitter value, bandwidth value, or generated command cannot convert one case into another executable command.

The matrix manifest schema is version 4 because explicit burst-loss support adds the top-level `burst_loss_profiles` field and per-case `burst_loss_percent` / `burst_correlation_percent` fields. Consumers that validate the manifest shape can distinguish it from schema 3.

The protocol component of a case ID is a workload label for QA evidence; `tc netem` affects traffic on the selected namespace interface and does not filter packets by RTMP or SRT protocol.

## Safety boundary

Use only a disposable network namespace prepared for fault testing. The runner deliberately does not create the namespace because namespace/veth/routing setup has a separate blast radius and ownership lifecycle. It does not implement DNS faults, route withdrawal, or firewall/TCP-reset injection; those remain separate #13 slices with their own cleanup contracts. Burst-loss, jitter, and bandwidth shaping reuse only the already-bounded `netem` support from the injector and do not add a host-level shaper.

This runner records no credentials, destination URLs, packet contents, environment variables, or container logs. It does not contact external streaming providers and adds no external-cost behavior.
