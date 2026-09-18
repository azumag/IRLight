# Egress Gateway runtime secret-file boundary

The production Egress Gateway reads credential-bearing RTSP input and RTMP/RTMPS
destination URLs from files. Those paths are operator-controlled configuration,
not public request parameters, but malformed mounts must not turn secret loading
into an unbounded read, a FIFO/device block, or a diagnostic leak.

## Boundary

`apps/egress-gateway/runtime_secret_file.py` is the service-local reader used by
`secret_inputs.py`. It enforces the following rules before a secret value reaches
the media runtime:

- only a regular-file target is accepted;
- symlink -> regular file remains supported for Docker/Kubernetes projected
  secret volumes;
- the reader opens with `O_NONBLOCK` and `O_CLOEXEC` where the platform exposes
  them;
- reads are bounded to 64 KiB plus one detection byte;
- invalid UTF-8 is rejected as a controlled secret-file error;
- the resolved target identity is compared before open, after open, after read,
  and after final pathname resolution, so pathname replacement and in-place
  mutation fail closed;
- reader errors never contain the secret value, configured pathname, or a raw
  `OSError` chained cause.

`egress_entrypoint.py` binds both credential-bearing readers before constructing
`EgressGateway`. The production image starts through that entrypoint, and its
Dockerfile packages both boundary modules explicitly.

## Existing semantics preserved

This hardening does not change destination allow/deny rules, DNS drift checks,
RTMP sink selection, retry policy, or provider/session behavior.

For `EGRESS_INPUT_URI_FILE`, an unset file setting still uses `EGRESS_INPUT_URI`
(or the existing internal default). Once a file path is configured, unavailable
or malformed file contents continue through the existing startup wait window and
then fail instead of silently falling back to the environment URI. An empty file
continues to be an invalid input URL rather than an environment fallback.

The destination reader still distinguishes a validly read but invalid RTMP/RTMPS
URL from a secret-file availability failure. The URL itself is never included in
those errors.

## Regression coverage

`tests/test_egress_runtime_secret_files.py` covers regular files, projected
symlinks, FIFO/directory rejection, the 64 KiB size boundary, invalid UTF-8,
inspection-to-open replacement, in-read mutation, input fallback semantics,
destination URL validation, diagnostic redaction, and production image wiring.

This change does not add secret rotation, KMS/envelope encryption, external
provider probes, or any billable integration test.
