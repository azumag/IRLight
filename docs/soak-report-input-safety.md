# Soak report validator input safety

`scripts/validate-soak-report.py` treats a soak report as release evidence, so the input path is validated before JSON parsing instead of being read as an arbitrary filesystem object.

## File requirements

The canonical schema-v1 report must be a regular file. The validator rejects symlinks, FIFOs, devices, directories, sockets, and other non-regular inputs. It performs `lstat`, opens the file read-only with `O_NOFOLLOW` and `O_NONBLOCK` when the host exposes those flags, then compares the opened file's device/inode from `fstat` with the object inspected before open. A path that is replaced between inspection and open therefore fails closed.

Once opened, validation reads from the pinned file descriptor rather than reopening the pathname. Replacing the pathname after the descriptor is pinned cannot redirect the bytes being parsed.

## Size bound

A canonical soak report is limited to **2 MiB (2,097,152 bytes)**. The validator checks the size before and after open, then reads at most `MAX_REPORT_BYTES + 1` bytes so growth after `fstat` is still detected. Oversized input is rejected before JSON parsing.

The limit is a parser/storage safety bound, not a duration or acceptance-policy limit. Normal 2h/6h/12h schema-v1 reports sampled at operationally reasonable intervals remain far below it. If a future schema genuinely needs larger reports, change the schema/tooling deliberately rather than bypassing this guard.

## Scope

These checks harden local evidence consumption only. They do not prove that a soak run was genuine, choose CPU/memory/A/V acceptance thresholds, authorize external streaming, or change production state. Release acceptance continues to rely on the canonical schema validator plus separately approved policy and human/operator evidence review.
