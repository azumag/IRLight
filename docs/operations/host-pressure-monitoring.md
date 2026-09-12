# Host pressure monitoring

Issue #11 の運用監視として、既存の disk/inode pressure と memory pressure を一つの read-only check にまとめる。

## Check

```bash
bash scripts/check-host-pressure.sh
```

既定では disk check は `IRLIGHT_DISK_PATH`、`STATE_DIR`、`/state` の順で対象を選び、memory check は `/proc/meminfo` を読む。限定された診断やテストでは第1引数にdisk path、第2引数にmeminfo pathを指定できる。

```bash
bash scripts/check-host-pressure.sh /state /proc/meminfo
```

各componentの閾値は既存checkの環境変数をそのまま使う。

- `IRLIGHT_DISK_WARNING_PERCENT` / `IRLIGHT_DISK_CRITICAL_PERCENT`
- `IRLIGHT_DISK_INODE_WARNING_PERCENT` / `IRLIGHT_DISK_INODE_CRITICAL_PERCENT`
- `IRLIGHT_MEMORY_WARNING_PERCENT` / `IRLIGHT_MEMORY_CRITICAL_PERCENT`

出力は1行の固定形式とする。

```text
IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK
```

exit code は次の意味を持つ。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | disk/inode と memory が warning 未満 |
| 1 | `WARNING` | 少なくとも1 componentが warning |
| 2 | `CRITICAL` | 少なくとも1 componentが critical |
| 3 | `UNKNOWN` | criticalは確認されていないが、少なくとも1 componentを安全に評価できない |

集約時の優先順位は `CRITICAL > UNKNOWN > WARNING > OK` とする。既知のcriticalを別componentの診断失敗で隠さない一方、criticalがない場合のUNKNOWNは正常・warningへ推測補完しない。

## Diagnosis

summaryが `WARNING` / `CRITICAL` / `UNKNOWN` の場合はcomponent checkを個別に実行して詳細値とreasonを確認する。

```bash
bash scripts/check-disk-pressure.sh /state
bash scripts/check-memory-pressure.sh /proc/meminfo
```

`disk_status` はblock容量とinodeの深刻な方、`memory_status` は`MemAvailable`を基準にしたhost memory pressureを表す。hostの`OK`をcontainer/cgroup、swap、PSI、GPU memoryの余裕と読み替えない。

## Safety

このwrapperとcomponent checkは診断専用で、ファイル削除、Docker prune、volume削除、process kill/restart、cache drop、swap変更、authority state変更を行わない。復旧は対象Sessionとstate ownershipを確認した別の明示操作として実施する。

監視systemから呼び出す場合も、exit codeを契機に破壊的な自動復旧を直結させない。まずalertと診断へ接続し、復旧操作は個別runbookの条件を満たした場合に限る。

## Verification

```bash
python -m unittest discover -s tests -p 'test_host_pressure_check.py' -v
bash -n scripts/check-host-pressure.sh
```
