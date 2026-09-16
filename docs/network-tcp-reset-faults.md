# Bounded TCP reset fault injection

Issue #13 includes TCP-disconnect recovery testing in addition to the existing packet-loss, latency, jitter, bandwidth, burst-loss, and complete-blackhole matrix. `scripts/network-tcp-reset-injector.py` provides a separate firewall-scoped helper because TCP reset cleanup and blast radius differ from `tc netem`.

## Safety boundary

The helper never operates in the host network namespace. Every mode requires a named namespace, and `apply` / `clear` additionally require `--confirm-disposable-namespace`. It does not create namespaces, veth pairs, routes, DNS state, or provider resources.

Targets must be literal IPv4 or IPv6 addresses. Hostnames are rejected, so the helper does not introduce an implicit DNS lookup or DNS-rebinding decision. Each rule is limited to one TCP destination address and one destination port in the namespace `OUTPUT` chain. IPv4 uses `iptables`; IPv6 uses `ip6tables`.

The rule carries a caller-supplied QA identifier in an iptables comment and uses `REJECT --reject-with tcp-reset`. Use a unique `--rule-id` for concurrent or repeated cases. The helper does not inspect credentials, destination URLs, packet contents, or process environments.

SRT is UDP, so TCP reset is protocol-specific and does not pretend to provide an equivalent SRT fault. Use the existing netem loss/disconnect cases for cross-protocol RTMP/SRT comparisons.

## Read-only planning

Planning prints the exact argv without invoking `ip`, `iptables`, or `ip6tables`:

```bash
python scripts/network-tcp-reset-injector.py plan \
  --namespace irlight-qa \
  --destination 192.0.2.10 \
  --port 1935 \
  --rule-id rtmp-reset-01 \
  --duration 30 \
  --json
```

`192.0.2.0/24` is a documentation-only address block. Replace it with the literal address of the disposable QA destination reachable from the namespace.

The bounded durations reuse Issue #13's existing interruption matrix: 10, 30, 120, or 600 seconds.

## Applying a reset fault

Only use a disposable namespace prepared separately for the QA workload:

```bash
sudo python scripts/network-tcp-reset-injector.py apply \
  --namespace irlight-qa \
  --destination 192.0.2.10 \
  --port 1935 \
  --rule-id rtmp-reset-01 \
  --duration 30 \
  --confirm-disposable-namespace
```

The helper inserts a single exact OUTPUT rule, waits for the bounded duration, and then deletes that exact rule. Each firewall command has a 10-second process timeout and requests a two-second xtables lock wait.

Once an apply command is attempted, cleanup is attempted even when apply fails, times out, or is interrupted. Cleanup failure is returned as failure rather than claiming the fault completed safely.

## Explicit cleanup

If an earlier process was terminated outside the helper's cleanup path, remove the exact case rule with the same namespace, address, port, and rule ID:

```bash
sudo python scripts/network-tcp-reset-injector.py clear \
  --namespace irlight-qa \
  --destination 192.0.2.10 \
  --port 1935 \
  --rule-id rtmp-reset-01 \
  --confirm-disposable-namespace
```

`clear` deliberately fails if the exact rule cannot be removed. Operators should inspect the disposable namespace rather than treating an unconfirmed cleanup as success.

## Preconditions and limitations

The host running the QA namespace needs `ip`, plus `iptables` for IPv4 and/or `ip6tables` for IPv6, with support for the comment and REJECT targets. The helper does not install packages or alter firewall policy outside the named namespace.

This slice covers an RTMP/RTMPS TCP-reset primitive only. It does not yet wire TCP-reset cases into the deterministic network-fault manifest/case runner, and it does not implement DNS failure or route-change injection. Those remain separate Issue #13 slices so each mutation has an explicit ownership and cleanup contract.
