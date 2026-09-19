# Filesystem read-only monitoring

Issue #11 の障害切り分け用として、IRLight が状態・キャッシュ・出力を書き込む filesystem が kernel から read-only と見えていないかを **書込みを発生させず**確認する。

`check-disk-pressure.sh` は block / inode の容量枯渇を検出するが、空き容量が残っていても filesystem が障害対応や運用操作で read-only になれば write は失敗する。この check はその別 signal を補完する。

## 実行

既定では `IRLIGHT_FILESYSTEM_PATH`、`STATE_DIR`、`/state` の順で対象を選ぶ。

```bash
python3 scripts/check-filesystem-readonly.py
```

対象を明示する場合:

```bash
python3 scripts/check-filesystem-readonly.py /state
```

または:

```bash
IRLIGHT_FILESYSTEM_PATH=/state \
python3 scripts/check-filesystem-readonly.py
```

## 判定

Python の `os.statvfs()` が返す filesystem flag の `ST_RDONLY` だけを read-only 判定に使う。probe file の create/delete、mount、remount、fsck、permission 変更は行わない。

```text
IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=OK read_only=false
```

filesystem が read-only の場合は `CRITICAL`:

```text
IRLIGHT_FILESYSTEM_MOUNT_HEALTH status=CRITICAL reason=filesystem_read_only read_only=true
```

対象を安全に `statvfs` できない場合や platform が `ST_RDONLY` を提供しない場合は、正常と推測せず `UNKNOWN` にする。出力には対象 path や内部例外文字列を反射しない。

exit code は既存診断と同じ契約を使う。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | 対象 filesystem に `ST_RDONLY` が立っていない |
| 2 | `CRITICAL` | 対象 filesystem に `ST_RDONLY` が立っている |
| 3 | `UNKNOWN` | target / platform flag を安全に評価できない |

`WARNING` は使用しない。

## 限界

この check は mount flag の一点観測であり、実際の application write 成功を保証しない。quota、ACL/permission、SELinux/AppArmor、容量・inode枯渇、I/O error、network filesystem の server-side policy、個別 file/directory の immutability などは別 signal である。

write probe を行わないため、read-write mount 上で特定 directory だけが書けない状態は `OK` になり得る。容量・inodeは `check-disk-pressure.sh`、authority/readiness は `/readyz` と state inspection、個別 permission は deployment preflight で別途確認する。

## Safety

診断は `statvfs(2)` 相当の metadata 読取りのみを行う。mount/remount、file create/delete、`fsck`、provider操作、Docker volume操作、authority state更新、process restartを自動実行しない。`CRITICAL` を検知しても、対象 mount と state ownership を確認せず volume 再作成や remount を行わない。

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_filesystem_readonly_check.py' -v
python3 -m py_compile \
  scripts/check-filesystem-readonly.py \
  tests/test_filesystem_readonly_check.py
```
