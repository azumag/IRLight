# Relay client observation clock validation

The Node Agent's relay-client observer stamps the MediaMTX reader snapshot that is sent to the Control Plane. That timestamp is part of the relay connection observation boundary, so an invalid wall clock must not be published as if it were a trustworthy observation.

`RelayClientObserver.observe()` validates the effective observation clock before contacting the MediaMTX API. The observer accepts only finite, non-negative numeric values. It rejects booleans, negative values, NaN, positive or negative infinity, non-numeric values, and integers that cannot be represented as a float.

Invalid clocks raise a fixed `RuntimeError` before `_path_snapshot()` is called. This deliberately avoids generating a `CONNECTED`, `DISCONNECTED`, or `UNKNOWN` observation with an untrustworthy `observed_at` value and avoids performing a MediaMTX request for an observation that cannot be safely timestamped.

The same validation applies to an explicitly supplied `now` value and to the default `time.time()` sample. Existing relay reader-count semantics, MediaMTX request behavior, relay status vocabulary, and heartbeat persistence remain unchanged.
