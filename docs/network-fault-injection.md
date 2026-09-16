# Bounded network fault injection

Issue #13 requires repeatable network-failure testing for RTMP/RTMPS/SRT recovery. `scripts/network-fault-injector.py` provides the first reusable Linux `tc netem` slice for packet loss, latency/jitter, and complete packet blackholes.

## Safety boundary

The helper does **not** create network namespaces and does not discover or choose an interface. A test harness or operator must provide an already-created, disposable named Linux network namespace and the interface inside it.

`plan` is read-only. `apply` and `clear` require both an explicit `--namespace` and `--confirm-disposable-namespace`. The executing modes therefore use `ip netns exec <namespace> tc ...`; they never construct a host-namespace `tc` command.

`tc qdisc replace ... root netem` replaces the selected interface's root qdisc. Cleanup deletes that root qdisc; it cannot reconstruct an arbitrary qdisc that existed before the test. For that reason, use executing modes only in a disposable QA namespace whose qdisc state may be replaced. Do not point the tool at a production host interface. Rebuild/delete the namespace after a test if its prior qdisc configuration matters.

The tool executes argv directly without a shell. Interface and namespace names are syntax-checked, option-like names are rejected, and loopback needs the additional `--allow-loopback` acknowledgement.

## Supported matrix

The initial bounded matrix intentionally matches the discrete values already listed in #13:

- packet loss: 1%, 3%, 5%, 10%
- one-way netem delay setting: 50 ms, 100 ms, 300 ms, 1000 ms
- jitter: positive milliseconds no greater than the selected latency
- complete packet blackhole: netem loss 100%
- executing duration: 10 s, 30 s, 120 s, 600 s

Bandwidth shaping, burst-loss models, TCP reset, DNS failure, and route mutation are deliberately not mixed into this helper yet. They have different state/cleanup semantics and remain tracked by #13.

## Read-only planning

Print the exact loss/latency/jitter commands without executing them:

```bash
python scripts/network-fault-injector.py plan \
  --namespace irlight-qa \
  --interface eth0 \
  --loss 3 \
  --latency 100 \
  --jitter 20 \
  --duration 30
```

For machine-readable orchestration, add `--json`. The JSON contains `apply_argv`, `cleanup_argv`, the namespace/interface, and the optional duration; callers do not need to parse shell text.

A complete-disconnect plan is:

```bash
python scripts/network-fault-injector.py plan \
  --namespace irlight-qa \
  --interface eth0 \
  --disconnect \
  --duration 120
```

## Bounded execution

After creating and verifying a **disposable** test namespace, apply the fault for an allowed duration:

```bash
sudo python scripts/network-fault-injector.py apply \
  --namespace irlight-qa \
  --interface eth0 \
  --loss 5 \
  --duration 30 \
  --confirm-disposable-namespace
```

`apply` requires a duration. After the sleep, and also after `KeyboardInterrupt`, the helper attempts `tc qdisc del ... root` in `finally`. A cleanup failure is returned as an error instead of being hidden. SIGKILL, host crash, or kernel failure cannot run Python cleanup, so the namespace must remain disposable rather than relying on cleanup as restoration.

If an interrupted external runner needs explicit cleanup, use:

```bash
sudo python scripts/network-fault-injector.py clear \
  --namespace irlight-qa \
  --interface eth0 \
  --confirm-disposable-namespace
```

`clear` deletes the root qdisc; it does not restore previous qdisc configuration.

## Test coverage

The unit regression covers command construction, the #13 loss/latency/duration matrix boundary, jitter validation, complete disconnects, unsafe interface/namespace syntax, loopback acknowledgement, namespaced execution, normal cleanup, and cleanup after `KeyboardInterrupt`.

Run it with:

```bash
python -m unittest discover -s tests -p 'test_network_fault_injector.py' -v
```

No unit test applies a real qdisc. End-to-end use of `apply` belongs in an isolated Linux namespace runner where `iproute2` and `CAP_NET_ADMIN` are intentionally available.
