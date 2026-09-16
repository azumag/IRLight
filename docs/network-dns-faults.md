# DNS fault injection for QA

`scripts/network-dns-fault-injector.py` provides a bounded DNS-failure primitive
for Issue #13 network recovery testing. It is deliberately narrower than a
general firewall helper.

## Safety boundary

The helper:

- only operates inside an explicitly named Linux network namespace;
- requires `--confirm-disposable-namespace` for `apply` and `clear`;
- accepts a literal IPv4 or IPv6 resolver address only and performs no DNS lookup;
- changes only `OUTPUT` rules matching that resolver on destination port 53;
- blocks both UDP and TCP DNS, preventing ordinary TCP fallback from bypassing the fault;
- does not create namespaces, edit routes, rewrite `/etc/resolv.conf`, touch the host
  firewall, or contact any provider API;
- uses shell-free argv execution with a 10 second command timeout;
- limits bounded runs to 10, 30, 120, or 600 seconds;
- records a case-specific firewall comment so cleanup targets the exact generated rules;
- refuses `apply` when an exact rule already exists, avoiding a repeated case silently stacking duplicate rules.

Use a disposable QA namespace. Do not point this helper at a production namespace.

## Read-only plan

```bash
python scripts/network-dns-fault-injector.py plan \
  --namespace irlight-qa \
  --resolver 192.0.2.53 \
  --rule-id dns-outage-30s \
  --duration 30 \
  --json
```

`plan` never invokes `iptables`/`ip6tables`.

## Apply a bounded outage

```bash
sudo python scripts/network-dns-fault-injector.py apply \
  --namespace irlight-qa \
  --resolver 192.0.2.53 \
  --rule-id dns-outage-30s \
  --duration 30 \
  --confirm-disposable-namespace
```

Before mutating firewall state, the script checks that neither exact rule already
exists. Reusing a stale `rule-id` therefore fails before insertion instead of
stacking duplicate rules. The script then inserts one UDP/53 and one TCP/53 reject
rule. Cleanup is registered before each insert attempt, so an apply failure or
`KeyboardInterrupt` cannot skip cleanup merely because the command outcome is uncertain. Exact cleanup is
attempted in reverse order on normal completion and failure. Cleanup failure is
reported as failure rather than as a successful QA run.

## Manual cleanup

```bash
sudo python scripts/network-dns-fault-injector.py clear \
  --namespace irlight-qa \
  --resolver 192.0.2.53 \
  --rule-id dns-outage-30s \
  --confirm-disposable-namespace
```

`clear` attempts both exact rules even if one deletion fails, then returns a
failure so an operator knows firewall state could not be fully confirmed.

## What this does and does not prove

This primitive verifies behavior when the configured resolver becomes
unreachable from the disposable namespace. It does not change resolver
configuration, simulate poisoned answers, DNSSEC failures, split-horizon
behavior, or provider-specific DNS outages. Those require separate explicit
fixtures.

For an end-to-end recovery case, capture the resolver address from the isolated
QA namespace before applying the fault, start the relevant RTMP/RTMPS/SRT
workload, inject the bounded fault, and verify the expected state/event
sequence. Do not treat this helper alone as evidence that an external
platform's DNS behavior has been tested.
