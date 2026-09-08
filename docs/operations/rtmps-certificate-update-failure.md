# RTMPS certificate update failure

This runbook covers the operational case where the certificate used by the Media Node RTMPS listener is missing, invalid, not yet valid, expired, or close to expiry. It is intentionally read-only until an operator explicitly replaces certificate files. It does not renew certificates, restart active Sessions, change DNS, or contact a certificate authority.

## Detection

Use the repository checker against the same public certificate file configured by `NODE_RTMPS_CERT_FILE`:

```bash
NODE_RTMPS_CERT_FILE=/etc/irlight/tls/ingest.example.com.fullchain.pem \
IRLIGHT_RTMPS_CERT_MIN_VALID_SECONDS=1209600 \
  ./scripts/check-rtmps-certificate.sh
```

`1209600` is an example 14-day warning window, not a product or SLO policy. The checker defaults to `0` seconds so the repository does not silently choose an alert threshold. Production monitoring should set an explicit value appropriate to the certificate renewal process.

The checker reads only the public certificate and emits a small JSON record containing fixed status/reason values and public validity timestamps. It never reads `NODE_RTMPS_KEY_FILE`, prints the certificate path, or opens a network connection. The production Media Node host is Linux; the checker requires OpenSSL plus GNU `date` so it can fail closed when `notBefore` is still in the future instead of treating `-checkend` alone as full validity verification.

Exit codes and fixed reason codes are:

| Exit | Status | Code | Meaning |
| --- | --- | --- | --- |
| `0` | `OK` | `RTMPS_CERT_VALID` | Certificate parses, is already valid, and remains valid beyond the configured window. |
| `1` | `WARNING` | `RTMPS_CERT_EXPIRING` | Certificate expires within the configured window or is already expired. |
| `2` | `ERROR` | `RTMPS_CERT_PATH_REQUIRED` | No certificate path was supplied. |
| `2` | `ERROR` | `RTMPS_CERT_THRESHOLD_INVALID` | The warning window is not a non-negative integer number of seconds. |
| `2` | `ERROR` | `RTMPS_CERT_UNAVAILABLE` | The configured certificate file is missing or unreadable. |
| `2` | `ERROR` | `RTMPS_CERT_INVALID` | OpenSSL cannot parse the certificate safely. |
| `2` | `ERROR` | `RTMPS_CERT_NOT_YET_VALID` | The certificate `notBefore` timestamp is still in the future. |
| `2` | `ERROR` | `RTMPS_CERT_TIME_UNREADABLE` | The public validity timestamp cannot be converted or the local clock cannot be read safely. |
| `2` | `ERROR` | `OPENSSL_UNAVAILABLE` | The host does not provide the required OpenSSL CLI. |
| `2` | `ERROR` | `DATE_UNAVAILABLE` | The host does not provide the required `date` CLI. |

Treat exit `2` as "certificate state cannot be trusted", not as evidence that no certificate is needed.

## Impact assessment

A certificate-file problem does not by itself prove that an already-running Session has failed. MediaMTX loads the certificate used by its listener when that process is started; replacing a host file does not retroactively prove what an existing process has loaded.

Check separately:

1. whether any active Session is using the affected Media Node;
2. whether the node was started before or after the suspect certificate update;
3. whether new RTMPS clients are failing TLS while plain RTMP/SRT remain healthy;
4. whether the stable hostname returned as `IRLIGHT_INGEST_PUBLIC_HOST` still resolves to the intended prepared node;
5. whether the failure is certificate validity time, hostname/SAN mismatch, key mismatch, incomplete chain, or a client trust-store problem.

Do not classify an RTMPS client failure as an authentication failure until the TLS layer has succeeded.

## Read-only triage

First run the checker. Then inspect only public certificate metadata:

```bash
openssl x509 \
  -in "$NODE_RTMPS_CERT_FILE" \
  -noout \
  -subject \
  -issuer \
  -serial \
  -dates \
  -ext subjectAltName
```

Verify that the certificate is valid for the stable ingest hostname without disabling hostname verification:

```bash
openssl x509 \
  -in "$NODE_RTMPS_CERT_FILE" \
  -noout \
  -checkhost "$IRLIGHT_INGEST_PUBLIC_HOST"
```

If a replacement certificate/key pair has been staged, compare only their derived public-key digests. Do not print or copy the private key itself:

```bash
cert_pub_sha="$({
  openssl x509 -in "$NODE_RTMPS_CERT_FILE" -pubkey -noout |
    openssl pkey -pubin -outform DER 2>/dev/null
} | sha256sum | awk '{print $1}')"

key_pub_sha="$({
  openssl pkey -in "$NODE_RTMPS_KEY_FILE" -pubout -outform DER 2>/dev/null
} | sha256sum | awk '{print $1}')"

test "$cert_pub_sha" = "$key_pub_sha"
unset cert_pub_sha key_pub_sha
```

A mismatch is a deployment input failure. Do not work around it by disabling TLS verification or by putting the private key in a command line, Issue, CI artifact, or log.

## Recovery

For routine renewal, follow the rotation boundary in `docs/rtmps-ingest.md`:

1. obtain/renew the certificate outside the active Media Node;
2. stage the new private key and full chain in new files with restrictive permissions;
3. validate validity time, hostname/SAN, and key/certificate match;
4. atomically replace the host paths referenced by `NODE_RTMPS_KEY_FILE` and `NODE_RTMPS_CERT_FILE`;
5. run `check-rtmps-certificate.sh` again against the final certificate path;
6. use the production Compose preflight before a new Media Node is created;
7. validate RTMPS on a newly prepared node with normal certificate verification enabled.

Do not restart a MediaMTX process carrying an active Session merely to pick up a routine renewal. Existing short-lived nodes may finish normally. Emergency revocation or confirmed key compromise is a separate disruptive security decision: identify affected Sessions, preserve the minimum required audit metadata, and use the security/incident path rather than silently restarting every node.

## Recovery confirmation

Before closing the incident or renewal task, record only non-secret evidence:

- checker status/code, `not_before`, and `not_after`;
- certificate fingerprint and issuer/serial if operationally useful;
- hostname/SAN verification result;
- whether the staged certificate and key public digests matched;
- production Compose preflight result;
- a new-node RTMPS TLS/publish result with credentials redacted;
- which active Sessions, if any, were intentionally left on their previously loaded certificate.

Do not attach private keys, stream credentials, credential-bearing URLs, raw environment dumps, or unredacted MediaMTX logs.

## Follow-up boundary

This runbook deliberately does not choose the production warning threshold, notification channel, renewal provider, or automated renewal mechanism. Those choices depend on the deployment and operational ownership. Automated certificate provisioning/rotation can be implemented separately once that boundary is decided; the read-only checker and fixed reason codes can be reused as its pre/post condition.
