# Standby asset checksum verification

Issue #7 の Asset processing / Node prefetch 境界で使う、read-only の checksum primitive を定義する。

`STANDBY_IMAGE_PATH` は Continuity 側でも regular-file / size / header / snapshot の防御を持つが、それだけでは「Node に届いた byte 列が、Control Plane / processing worker が承認した version と同じか」を表せない。`scripts/verify-standby-asset-checksum.py` は、その将来の handoff に使える canonical SHA-256 と byte size を安全に採取・照合する。

## Usage

Inspection:

```sh
python3 scripts/verify-standby-asset-checksum.py inspect /node-cache/asset.bin
```

Verification against metadata already bound to an owner/object/version authority:

```sh
python3 scripts/verify-standby-asset-checksum.py verify /node-cache/asset.bin \
  --expected-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --expected-size-bytes 123456
```

Successful output is compact JSON:

```json
{"algorithm":"sha256","schema_version":1,"sha256":"...","size_bytes":123456}
```

`verify` also adds `"verified":true`.

The output deliberately contains no local pathname, user filename, object key, URL, credential, or owner identifier. The caller is responsible for binding the digest to its own authenticated asset/version record.

## File-safety boundary

The helper is read-only and accepts only one stable local regular file.

- symlinks and non-regular files such as FIFO/device are rejected
- the existing Continuity maximum of 32 MiB is enforced before hashing
- the source is opened with `O_NOFOLLOW`, `O_NONBLOCK`, and `O_CLOEXEC` where supported
- device/inode are checked across pathname open and after hashing
- size/mtime/ctime and descriptor identity must remain stable while bytes are hashed
- path replacement or in-place mutation during inspection fails closed
- expected SHA-256 uses exactly 64 lowercase hexadecimal characters
- expected size, when supplied, must be within the same 32 MiB bound

This does not create, rename, delete, upload, fetch, or repair an asset.

## What this does not claim

A matching checksum is necessary for immutable version handoff, but is not sufficient to mark an asset `READY`.

The following remain Issue #7 work:

- owner-bound upload intent and server-generated object keys
- full decode / decompression-budget validation in a processing worker
- `PENDING -> PROCESSING -> READY/FAILED` job/lease/retry semantics
- persistent storage of the approved hash/version and Node-side assignment
- authenticated object-storage fetch
- cache/LRU and reference-safe deletion
- profile compatibility and actual application acknowledgement

Until those pieces are wired together, this helper is a local verification primitive only; it must not be treated as release or processing completion evidence by itself.
