# Filesystem mountpoint monitoring

Issue #11 / #90 の障害切り分け用として、IRLight が state・cache・output 用に **mount されていることを前提とする path が、現在の Linux mount namespace で実際に mountpoint か**を read-only で確認する。

容量・inode・read-only flag が正常でも、volume mount が外れて underlying root filesystem の同名 directory へ書き込みが継続すると、authority の世代や保存先を取り違える可能性がある。この check はその「expected mountpoint が消えている」状態だけを独立 signal として検出する。

## 実行

既定では `IRLIGHT_EXPECTED_MOUNTPOINT_PATH`、`STATE_DIR`、`/state` の順で対象を選ぶ。target は曖昧な working-directory 解釈を避けるため absolute path のみ受け付ける。

```bash
python3 scripts/check-filesystem-mountpoint.py
```

対象を明示する場合:

```bash
python3 scripts/check-filesystem-mountpoint.py /state
```

または:

```bash
IRLIGHT_EXPECTED_MOUNTPOINT_PATH=/state \
python3 scripts/check-filesystem-mountpoint.py
```

Linux 以外の fixture / テストでは `IRLIGHT_MOUNTINFO_PATH` で mountinfo 入力を差し替えられる。本番では既定の `/proc/self/mountinfo` を使い、mount table や namespace を変更しない。

## 判定

checker は対象 path を symlink として追跡せず、Linux `/proc/self/mountinfo` の mountpoint field と **exact match** するかだけを見る。親 filesystem が mount 済みでも、対象 path 自身が mountpoint でなければ成功扱いしない。bind mount も mountinfo に独立 record があるため対象になる。

対象が exact mountpoint の場合:

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=OK mounted=true
```

対象 path が存在しない、または存在しても exact mountpoint ではない場合は `CRITICAL`:

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mountpoint_missing
```

mountinfo を安全に読めない・parse できない場合は正常と推測せず `UNKNOWN`:

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountinfo_unavailable
```

target の metadata 自体を安全に読めない場合や symlink target は、それぞれ固定 reason の `UNKNOWN` にする。

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=target_unavailable
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=target_symlink
```

出力には target path、mount source、device、内部例外文字列を反射しない。

exit code は既存診断と同じ契約を使う。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | 対象 path が current mount namespace の exact mountpoint |
| 2 | `CRITICAL` | expected mountpoint が存在しない |
| 3 | `UNKNOWN` | target / mountinfo を安全に評価できない |

`WARNING` は使用しない。

## mountinfo の扱い

`/proc/self/mountinfo` の mandatory fields と `-` separator を確認し、mountpoint field の kernel escape `\\011` / `\\012` / `\\040` / `\\134` のみを decode する。短い record、不正 escape、不正な separator などがあれば、部分的な table を根拠に `OK` とせず `UNKNOWN` に fail-closed する。

## 限界

この check は **「この path が mountpoint か」だけ**を確認する。次は保証しない。

- mount source / provider volume が意図したものか
- volume generation / backup generation が正しいか
- filesystem が read-write か
- block / inode に余裕があるか
- authority JSON / initialization marker が正しいか
- provider resource ownership と local state が一致するか

read-only flag は [`filesystem-readonly-monitoring.md`](filesystem-readonly-monitoring.md)、capacity は `check-disk-pressure.sh`、authority は `/readyz` と [`state-readiness.md`](state-readiness.md)、state/provider 所有権は [`state-provider-reconciliation.md`](state-provider-reconciliation.md) で別々に確認する。

特に Issue #90 の「正しい mount / generation」のうち、この checker が埋めるのは expected mountpoint の **presence** だけである。`OK` を「正しい volume generation を確認済み」と解釈しない。

## Safety

診断は target の metadata と `/proc/self/mountinfo` の read のみを行う。mount/remount/unmount、file create/delete、volume attach/detach、Docker volume操作、provider操作、authority state更新、marker生成、process restartを自動実行しない。

`CRITICAL` を検知しても、underlying directory の内容を消したり、空 volume を作ったり、provider volume を推測 attach したりしない。書込み系サービスを quiesce すべきかを判断し、state generation / provider ownership を read-only に照合してから復旧判断へ進む。

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_filesystem_mountpoint_check.py' -v
python3 -m unittest discover -s tests -p 'test_filesystem_mountpoint_runbook_contract.py' -v
python3 -m unittest discover -s tests -p 'test_operations_runbook_inventory.py' -v
python3 -m py_compile \
  scripts/check-filesystem-mountpoint.py \
  tests/test_filesystem_mountpoint_check.py \
  tests/test_filesystem_mountpoint_runbook_contract.py
```
