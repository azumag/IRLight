# Filesystem mountpoint monitoring

Issue #11 / #90 の障害切り分け用として、IRLight が state・cache・output 用に **mount されていることを前提とする path が、現在の Linux mount namespace で実際に mountpoint か**を read-only で確認する。

容量・inode・read-only flag が正常でも、volume mount が外れて underlying root filesystem の同名 directory へ書き込みが継続すると、authority の世代や保存先を取り違える可能性がある。この check はその「expected mountpoint が消えている」状態を独立 signal として検出し、必要な deployment では current mount namespace の source / root も operator が指定した期待値と照合できる。

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

checker は対象 path を symlink として追跡せず、Linux `/proc/self/mountinfo` の mountpoint field と **exact match** するかを見る。親 filesystem が mount 済みでも、対象 path 自身が mountpoint でなければ成功扱いしない。bind mount も mountinfo に独立 record があるため対象になる。

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

出力には target path、mount source、mount root、device、期待値、内部例外文字列を反射しない。

exit code は既存診断と同じ契約を使う。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | 対象 path が current mount namespace の exact mountpoint で、指定された identity expectation も一致 |
| 2 | `CRITICAL` | expected mountpoint が存在しない、または opt-in identity expectation と不一致 |
| 3 | `UNKNOWN` | target / mountinfo / identity expectation を安全に評価できない |

`WARNING` は使用しない。

## Optional mount identity verification

既定動作は従来どおり **presence-only** であり、source / root は判定しない。deployment 側で current mount namespace に期待する mount identity を静的に明示できる場合だけ、次の環境変数を任意に設定する。

- `IRLIGHT_EXPECTED_MOUNT_SOURCE`: `/proc/self/mountinfo` の `mount source` と exact match する期待値。block device path、`tmpfs`、`overlay` など mountinfo がその namespace で公開する文字列をそのまま指定する。
- `IRLIGHT_EXPECTED_MOUNT_ROOT`: mountinfo の `root` field と exact match する absolute path。bind mount や同一 backing filesystem 内の subpath を区別したい場合に使う。

片方だけでも両方でも利用できる。たとえば block-device backed state mount を source まで固定する場合:

```bash
IRLIGHT_EXPECTED_MOUNTPOINT_PATH=/state \
IRLIGHT_EXPECTED_MOUNT_SOURCE=/dev/mapper/irlight-state \
python3 scripts/check-filesystem-mountpoint.py
```

bind mount の backing subpath も固定する場合:

```bash
IRLIGHT_EXPECTED_MOUNTPOINT_PATH=/state \
IRLIGHT_EXPECTED_MOUNT_SOURCE=/dev/mapper/irlight-data \
IRLIGHT_EXPECTED_MOUNT_ROOT=/volumes/irlight-state \
python3 scripts/check-filesystem-mountpoint.py
```

identity expectation を設定した状態で source または root が一致しなければ、値そのものは表示せず `CRITICAL` にする。

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=CRITICAL reason=mount_identity_mismatch
```

同じ exact mountpoint の record が複数あり、どの record を identity 比較すべきか安全に一意化できない場合は、推測せず `UNKNOWN` にする。presence-only の既定動作では従来互換のため exact mountpoint が存在すれば `OK` のままとする。

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=mountpoint_ambiguous
```

identity 環境変数を「設定済みだが空文字」にした場合、または `IRLIGHT_EXPECTED_MOUNT_ROOT` が absolute path でない場合も設定ミスを無視せず fail-closed する。

```text
IRLIGHT_FILESYSTEM_MOUNTPOINT_HEALTH status=UNKNOWN reason=invalid_expected_mount_identity
```

この照合が保証するのは **現在の mount namespace に見えている mountinfo の source / root が、operator が与えた静的期待値と一致することだけ**である。source 名が provider API 上の特定 volume ID と同一であること、volume generation / backup generation / restore epoch / authority generation が正しいことは検証しない。それらを証明する provider metadata や authority state との照合を、この checker が推測で代替してはいけない。

## Host-pressure aggregate opt-in

既定の `check-host-pressure.sh` 出力は変更しない。deployment が「この path は独立した mountpoint でなければならない」と明示できる場合だけ、次の opt-in で `filesystem_mountpoint_status` を aggregate に追加する。

```bash
IRLIGHT_HOST_FILESYSTEM_MOUNTPOINT_MODE=enabled \
IRLIGHT_EXPECTED_MOUNTPOINT_PATH=/state \
bash scripts/check-host-pressure.sh
```

`IRLIGHT_EXPECTED_MOUNTPOINT_PATH` を省略した場合は standalone checker と同じく `STATE_DIR`、さらに未指定なら `/state` を対象にする。aggregate の disk path 引数は mountpoint target の fallback には使わない。容量・inode を確認したい filesystem と「独立 mount が必須な path」は別の契約だからである。

source / root まで deployment が静的に固定できる場合は、standalone checker と同じ `IRLIGHT_EXPECTED_MOUNT_SOURCE` / `IRLIGHT_EXPECTED_MOUNT_ROOT` を aggregate 実行環境へ設定できる。host adapter はそれらを変更せず checker へ継承する。未設定なら aggregate も presence-only のままである。

opt-in は `enabled` / `disabled` のみを受け付ける。未知値は component を無効化して続行せず、aggregate を `UNKNOWN reason=invalid_filesystem_mountpoint_mode` に fail-closed する。有効化後は他 component と同じ timeout 境界と `CRITICAL > UNKNOWN > WARNING > OK` の優先順位に参加する。

standalone checker の `CRITICAL reason=mountpoint_missing` / `mount_identity_mismatch` は aggregate では `filesystem_mountpoint_status=CRITICAL`、評価不能は `filesystem_mountpoint_status=UNKNOWN` として伝播する。aggregate 自体も mount/remount、volume attach、directory 作成などの復旧処理は行わない。

## mountinfo の扱い

`/proc/self/mountinfo` の mandatory fields と `-` separator を確認し、mountpoint field の kernel escape `\\011` / `\\012` / `\\040` / `\\134` を decode する。identity verification が有効な場合は、**対象 mountpoint の record に限って** root / mount source にも同じ decode を適用する。短い record、不正な mountpoint escape、不正な separator などがあれば、部分的な table を根拠に `OK` とせず `UNKNOWN` に fail-closed する。

presence-only では root / source を新たに解釈しない。identity opt-in を追加したことで、対象外 record の root / source 表現だけを理由に従来の presence 判定を壊さないためである。

## 限界

この check は、既定では **「この path が mountpoint か」だけ**、identity opt-in 時でも **「current mountinfo の source / root が operator 指定値と一致するか」まで**を確認する。次は保証しない。

- mount source が provider API 上の意図した volume ID / resource ownership と対応するか
- volume generation / backup generation / restore epoch が正しいか
- filesystem が read-write か
- block / inode に余裕があるか
- authority JSON / initialization marker が正しいか
- provider resource ownership と local state が一致するか

read-only flag は [`filesystem-readonly-monitoring.md`](filesystem-readonly-monitoring.md)、capacity は `check-disk-pressure.sh`、authority は `/readyz` と [`state-readiness.md`](state-readiness.md)、state/provider 所有権は [`state-provider-reconciliation.md`](state-provider-reconciliation.md) で別々に確認する。

特に Issue #90 の「正しい mount / generation」のうち、この checker が埋めるのは expected mountpoint の **presence** と、明示設定した場合の **namespace-local source/root identity** までである。`OK` を「正しい provider volume / volume generation を確認済み」と解釈しない。

## Safety

診断は target の metadata と `/proc/self/mountinfo` の read のみを行う。mount/remount/unmount、file create/delete、volume attach/detach、Docker volume操作、provider操作、authority state更新、marker生成、process restartを自動実行しない。

`CRITICAL` を検知しても、underlying directory の内容を消したり、空 volume を作ったり、provider volume を推測 attach したりしない。書込み系サービスを quiesce すべきかを判断し、state generation / provider ownership を read-only に照合してから復旧判断へ進む。

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_filesystem_mountpoint_check.py' -v
python3 -m unittest discover -s tests -p 'test_filesystem_mountpoint_runbook_contract.py' -v
python3 -m unittest discover -s tests -p 'test_host_filesystem_mountpoint_aggregate.py' -v
python3 -m unittest discover -s tests -p 'test_host_pressure_opt_in_matrix.py' -v
python3 -m unittest discover -s tests -p 'test_operations_runbook_inventory.py' -v
python3 -m py_compile \
  scripts/check-filesystem-mountpoint.py \
  tests/test_filesystem_mountpoint_check.py \
  tests/test_filesystem_mountpoint_runbook_contract.py \
  tests/test_host_filesystem_mountpoint_aggregate.py
bash -n \
  scripts/check-host-filesystem-mountpoint.sh \
  scripts/check-host-pressure.sh
```
