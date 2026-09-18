# Soak media-metrics snapshot input boundary

`scripts/collect-soak-resource-sample.py` can merge an externally produced media-metrics snapshot into one measured-soak sample through `--media-metrics-file`. Because that snapshot becomes QA evidence, the collector treats the file as an input trust boundary rather than as an arbitrary local text file.

The accepted snapshot remains the existing strict four-field JSON object (`bitrate_bps`, `av_sync_drift_ms`, `timestamp_errors`, and `unexpected_reconnects`). The file itself is limited to 64 KiB and must be a stable regular file. Final symlinks, FIFOs, devices, oversized files, invalid UTF-8, and recursive/invalid JSON are rejected. Where the platform exposes them, the collector opens with `O_NOFOLLOW`, `O_NONBLOCK`, and `O_CLOEXEC`.

For a measured read, the collector compares device, inode, size, mtime, and ctime before and after opening, before and after the bounded read, and against the final pathname. A pathname replacement or same-inode mutation during the read therefore fails closed rather than contributing ambiguous evidence. File-open/read failures use controlled error categories instead of echoing the local pathname or raw OS error into normal command output.

`--allow-unmeasured-media` is unchanged: it is still an explicit opt-in for resource-only samples and does not inspect a media-metrics file. This hardening does not alter soak acceptance thresholds, Docker state, production media behavior, provider integrations, credentials, or billing.
