# Filesystem capacity exhaustion の read-only 診断

Issue #11 が Critical として要求する `disk full` を、閾値を推測せずに検出するための targeted read-only 診断です。`scripts/check-filesystem-capacity.py` は `statvfs(2)` 相当の情報だけを読み、対象 filesystem の一般ユーザー向け block または inode が **実際に 0** になった場合だけ `CRITICAL` を返します。

この checker は filesystem、mount、process、service、quota、provider resource、Session、課金状態を変更しません。空き容量を確保するための削除や cleanup も自動実行しません。

## 実行

既定では `/` を確認します。IRLight の authority / media data / container storage が別 mount にある場合は、その mount 上の既存 path を明示してください。

```bash
python scripts/check-filesystem-capacity.py /
python scripts/check-filesystem-capacity.py /var/lib/irlight
```

環境変数でも指定できます。

```bash
IRLIGHT_FILESYSTEM_PATH=/var/lib/irlight \
  python scripts/check-filesystem-capacity.py
```

出力例:

```text
IRLIGHT_FILESYSTEM_CAPACITY status=OK reason=none available_bytes=2147483648 available_inodes=120000
IRLIGHT_FILESYSTEM_CAPACITY status=CRITICAL reason=blocks_exhausted available_bytes=0 available_inodes=120000
IRLIGHT_FILESYSTEM_CAPACITY status=CRITICAL reason=inodes_exhausted available_bytes=2147483648 available_inodes=0
IRLIGHT_FILESYSTEM_CAPACITY status=UNKNOWN reason=path_unavailable
```

exit code は `0=OK`, `2=CRITICAL`, `3=UNKNOWN` です。

## 判定契約

- `f_bavail == 0`: service user が新規 block を確保できない状態として `CRITICAL / blocks_exhausted`。
- inode accounting を持つ filesystem で `f_favail == 0`: `CRITICAL / inodes_exhausted`。
- 両方 0: `CRITICAL / blocks_and_inodes_exhausted`。
- inode accounting を公開しない filesystem (`f_files == 0`) は inode 不明を `available_inodes=unsupported` とし、block 情報だけで判定します。
- path を読めない、block capacity が取得できない、矛盾した statvfs 値を返す場合は正常と推測せず `UNKNOWN` に fail closed します。

ここでは warning threshold を決めません。`5%未満` や `10 GiB未満` のような early-warning 値は filesystem の用途、reserved block、write rate、cleanup policy、運用猶予によって意味が変わるため、実測なしに repository 側で固定しません。必要なら deployment 側で明示 threshold を持つ warning collector を別途定義してください。

## 監視対象

最低限、実 deployment では次を個別に確認してください。

- Control Plane authority / state が存在する mount
- Media Node の container / image / writable layer が存在する mount
- temporary media / log / cache を置く mount

`/` がこれらと同じ filesystem とは限りません。checker は指定 path が属する filesystem だけを確認します。

## CRITICAL 時の一次対応

1. 対象 mount と影響中 service / Session を確認する。
2. read-only で容量、inode、増加元を切り分ける。
3. authority / evidence / active Session data を削除対象と推測しない。
4. cleanup、log rotation、image 削除、provider 再作成等の変更操作は、対象と復旧影響を確認した明示的な運用判断として行う。
5. 容量を回復した後、readiness、Session state、ingest / egress、reaper 等の関連 signal が正常化したことを確認する。

`docker compose down -v`、volume prune、state file / marker の削除、証跡の消去を disk-full 復旧の近道として使わないでください。

## Secret / path handling

checker は通常出力へ指定 path や `OSError` 本文を再表示しません。監視通知には status、reason、空き block/inode 数値だけを使い、stream key、token、credential、ユーザー入力、内部 path を label / payload へ追加しないでください。
