# Host pressure monitoring

Issue #11 の運用監視として、disk/inode pressure、memory pressure、host load pressure、Linux Pressure Stall Information (PSI) を一つの read-only check にまとめる。

## Check

```bash
bash scripts/check-host-pressure.sh
```

既定では disk check は `IRLIGHT_DISK_PATH`、`STATE_DIR`、`/state` の順で対象を選び、memory check は `/proc/meminfo`、load check は `/proc/loadavg`、PSI check は `/proc/pressure/{cpu,memory,io}` を読む。限定された診断やテストでは第1引数にdisk path、第2引数にmeminfo path、第3引数にloadavg path、第4引数にonline CPU数、第5引数にPSI directoryを指定できる。

```bash
bash scripts/check-host-pressure.sh /state /proc/meminfo /proc/loadavg 4 /proc/pressure
```

CPU数を省略した場合、load check は `getconf _NPROCESSORS_ONLN` をread-onlyで参照する。

各componentの閾値は環境変数で変更できる。

- `IRLIGHT_DISK_WARNING_PERCENT` / `IRLIGHT_DISK_CRITICAL_PERCENT`
- `IRLIGHT_DISK_INODE_WARNING_PERCENT` / `IRLIGHT_DISK_INODE_CRITICAL_PERCENT`
- `IRLIGHT_MEMORY_WARNING_PERCENT` / `IRLIGHT_MEMORY_CRITICAL_PERCENT`
- `IRLIGHT_LOAD_WARNING_PERCENT` / `IRLIGHT_LOAD_CRITICAL_PERCENT`
- `IRLIGHT_PSI_SOME_WARNING_PERCENT` / `IRLIGHT_PSI_SOME_CRITICAL_PERCENT`
- `IRLIGHT_PSI_FULL_WARNING_PERCENT` / `IRLIGHT_PSI_FULL_CRITICAL_PERCENT`

load pressure は5分load averageをonline CPU数で正規化した値をpercentとして扱う。既定は100%でwarning、200%でcriticalとする。これはCPU utilizationそのものではなく、実行待ち・uninterruptible I/O waitを含むLinux load averageの継続的な混雑指標である。

PSI pressure はLinux kernelの10秒平均 `avg10` を使う。`some` は少なくとも1 taskが対象resourceでstallしていた時間、`full` は全non-idle taskが同時にstallしていた時間を表す。CPUは`some`、memory/ioは`some`と`full`を評価する。初期閾値は `some >= 25%` でwarning、`some >= 50%` でcritical、`full >= 5%` でwarning、`full >= 20%` でcriticalとし、β運用の実測で調整する。PSIの欠落・壊れた値・NaN/Infinity・100%超過値は正常扱いせずUNKNOWNにする。

出力は1行の固定形式とする。

```text
IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK
```

exit code は次の意味を持つ。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | disk/inode、memory、load、PSI が warning 未満 |
| 1 | `WARNING` | 少なくとも1 componentが warning |
| 2 | `CRITICAL` | 少なくとも1 componentが critical |
| 3 | `UNKNOWN` | criticalは確認されていないが、少なくとも1 componentを安全に評価できない |

集約時の優先順位は `CRITICAL > UNKNOWN > WARNING > OK` とする。既知のcriticalを別componentの診断失敗で隠さない一方、criticalがない場合のUNKNOWNは正常・warningへ推測補完しない。

## Diagnosis

summaryが `WARNING` / `CRITICAL` / `UNKNOWN` の場合はcomponent checkを個別に実行して詳細値とreasonを確認する。

```bash
bash scripts/check-disk-pressure.sh /state
bash scripts/check-memory-pressure.sh /proc/meminfo
bash scripts/check-load-pressure.sh /proc/loadavg
bash scripts/check-psi-pressure.sh /proc/pressure
```

`disk_status` はblock容量とinodeの深刻な方、`memory_status` は`MemAvailable`を基準にしたhost memory pressure、`load_status` は5分load averageをCPU数で正規化した継続負荷、`psi_status` はCPU/memory/I/Oでtaskが実際にstallした割合を表す。hostの`OK`をcontainer/cgroup、swap、GPU memory、network帯域の余裕と読み替えない。

load pressureが高い場合も、このcheckだけではCPU saturationとI/O waitを区別しない。PSIを併用するとtask stallの有無を確認できるが、原因processやdeviceまでは特定しない。`vmstat`、`iostat`、process/container metrics等の追加診断で原因を確認し、単純なprocess killや再起動を自動実行しない。

PSIが利用できないkernelやcontainer環境ではhost summaryはUNKNOWNになる。PSIを無効扱いしてOKへ丸めず、監視対象OSの要件を明示するか、PSI対応kernelへ揃える。個別componentの結果が必要な場合はdisk/memory/load checkを直接実行する。

## Safety

このwrapperとcomponent checkは診断専用で、ファイル削除、Docker prune、volume削除、process kill/restart、cache drop、swap変更、authority state変更を行わない。復旧は対象Sessionとstate ownershipを確認した別の明示操作として実施する。

監視systemから呼び出す場合も、exit codeを契機に破壊的な自動復旧を直結させない。まずalertと診断へ接続し、復旧操作は個別runbookの条件を満たした場合に限る。

## Verification

```bash
python -m unittest discover -s tests -p 'test_load_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_psi_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_pressure_check.py' -v
bash -n scripts/check-load-pressure.sh scripts/check-psi-pressure.sh scripts/check-host-pressure.sh
```
