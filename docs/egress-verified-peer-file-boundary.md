# Egress verified-peer metadata file boundary

The Egress Gateway re-resolves a custom RTMP/RTMPS destination before every publish attempt and, when available, requires the resolved answer set to contain the peer IP recorded during destination verification. The peer-IP file is therefore security-sensitive metadata: treating a FIFO, device, oversized file, or torn replacement as a normal optional value can weaken or stall the DNS-drift guard.

`apps/egress-gateway/destination_guard.py` now treats this input as a small file boundary rather than calling `Path.read_text()` directly.

- A missing file remains optional and returns no expected peer IP, preserving the existing startup/runtime contract.
- Symlinks to regular files remain supported so projected/configured secret-volume layouts continue to work.
- FIFO, device, directory, and other non-regular inputs fail closed with `DESTINATION_GUARD_INVALID` before a blocking read.
- The file is capped at 4 KiB and is also read with a 4 KiB + 1 byte bound, so growth after the initial inspection cannot cause an unbounded allocation.
- `O_NONBLOCK` and `O_CLOEXEC` are used where the platform exposes them.
- The inspected/opened file identity and metadata are compared, and metadata is checked again after the read. Replacement or in-place mutation observed across those boundaries fails closed.
- Invalid UTF-8 and operating-system read failures are converted to the fixed destination-guard error without including the operator-local pathname or a chained raw `OSError`.

This change does not alter destination URL policy, private-target overrides, DNS resolution rules, provider actions, Session state, billing, or the peer-IP matching rule. It only hardens how the already-existing verified-peer metadata is obtained before runtime destination validation.

Refs #12
