# Host pressure monitoring

Issue #11 の運用監視として、disk/inode、memory、host load、Linux Pressure Stall Information (PSI)、system-wide file handle、netfilter conntrack、system-wide task/thread pressure を一つの read-only check にまとめる。

## Check

```bash
bash scripts/check-host-pressure.sh
```

既定では disk check は `IRLIGHT_DISK_PATH`、`STATE_DIR`、`/state` の順で対象を選び、memory check は `/proc/meminfo`、load と task check は `/proc/loadavg`、PSI check は `/proc/pressure/{cpu,memory,io}`、file-handle check は `/proc/sys/fs/file-nr`、conntrack check は `/proc/sys/net/netfilter/nf_conntrack_count` と `nf_conntrack_max`、task check の上限は `/proc/sys/kernel/threads-max` を読む。

限定診断やテストでは、第1引数から順に disk path、meminfo path、loadavg path、online CPU数、PSI directory、file-nr path、conntrack count path、conntrack max path、threads-max path を指定できる。

```bash
bash scripts/check-host-pressure.sh \
  /state \
  /proc/meminfo \
  /proc/loadavg \
  4 \
  /proc/pressure \
  /proc/sys/fs/file-nr \
  /proc/sys/net/netfilter/nf_conntrack_count \
  /proc/sys/net/netfilter/nf_conntrack_max \
  /proc/sys/kernel/threads-max
```

CPU数を省略した場合、load check は `getconf _NPROCESSORS_ONLN` を read-only で参照する。

各componentの閾値は環境変数で変更できる。

- `IRLIGHT_DISK_WARNING_PERCENT` / `IRLIGHT_DISK_CRITICAL_PERCENT`
- `IRLIGHT_DISK_INODE_WARNING_PERCENT` / `IRLIGHT_DISK_INODE_CRITICAL_PERCENT`
- `IRLIGHT_MEMORY_WARNING_PERCENT` / `IRLIGHT_MEMORY_CRITICAL_PERCENT`
- `IRLIGHT_LOAD_WARNING_PERCENT` / `IRLIGHT_LOAD_CRITICAL_PERCENT`
- `IRLIGHT_PSI_SOME_WARNING_PERCENT` / `IRLIGHT_PSI_SOME_CRITICAL_PERCENT`
- `IRLIGHT_PSI_FULL_WARNING_PERCENT` / `IRLIGHT_PSI_FULL_CRITICAL_PERCENT`
- `IRLIGHT_FILE_HANDLE_WARNING_PERCENT` / `IRLIGHT_FILE_HANDLE_CRITICAL_PERCENT`
- `IRLIGHT_CONNTRACK_WARNING_PERCENT` / `IRLIGHT_CONNTRACK_CRITICAL_PERCENT`
- `IRLIGHT_TASK_WARNING_PERCENT` / `IRLIGHT_TASK_CRITICAL_PERCENT`

load pressure は5分load averageをonline CPU数で正規化したpercentで、既定は100%でwarning、200%でcriticalとする。CPU utilizationそのものではなく、実行待ちやuninterruptible I/O waitを含む継続的な混雑指標である。

PSI pressure はLinux kernelの10秒平均 `avg10` を使う。`some` は少なくとも1 taskが対象resourceでstallしていた時間、`full` は全non-idle taskが同時にstallしていた時間を表す。CPUは`some`、memory/ioは`some`と`full`を評価する。初期閾値は `some >= 25%` でwarning、`some >= 50%` でcritical、`full >= 5%` でwarning、`full >= 20%` でcriticalとする。欠落・壊れた値・NaN/Infinity・100%超過値はUNKNOWNにする。

file-handle pressure は `/proc/sys/fs/file-nr` の `allocated unused maximum` から `active = allocated - unused` を求め、system-wide file handle 使用率を評価する。既定は80%/90%。欠落、列数不正、非数値、矛盾、0以下のmaximum、signed 64-bit範囲外はUNKNOWNにする。個別process/containerの `ulimit -n` を代替しない。

conntrack pressure は `nf_conntrack_count / nf_conntrack_max` を評価し、既定は80%/90%。欠落、複数行、非数値、`count > maximum`、0以下のmaximum、signed 64-bit範囲外はUNKNOWNにする。これはnetwork bandwidth、socket backlog、NIC drop、個別processのsocket上限とは別のsignalである。

task pressure は `/proc/loadavg` の4番目の `running/total` から system-wide scheduling entity の `total` を取得し、`/proc/sys/kernel/threads-max` に対する比率を評価する。既定は80%でwarning、90%でcritical。Linuxのloadavgに含まれるtotalはprocessだけでなくthreadも含むため、PID番号空間そのものではなく、kernelのsystem-wide thread/task上限へ近づく兆候を見るためのsignalとして扱う。欠落、複数行、壊れた `running/total`、`running > total`、0以下のmaximum、`total > maximum`、signed 64-bit範囲外は正常扱いせずUNKNOWNにする。cgroup `pids.max` や個別serviceのprocess上限は別途監視が必要である。

出力は1行の固定形式とする。

```text
IRLIGHT_HOST_PRESSURE status=OK disk_status=OK memory_status=OK load_status=OK psi_status=OK file_handle_status=OK conntrack_status=OK task_status=OK
```

exit code は次の意味を持つ。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | 全componentがwarning未満 |
| 1 | `WARNING` | 少なくとも1 componentがwarning |
| 2 | `CRITICAL` | 少なくとも1 componentがcritical |
| 3 | `UNKNOWN` | criticalは確認されていないが、少なくとも1 componentを安全に評価できない |

集約時の優先順位は `CRITICAL > UNKNOWN > WARNING > OK` とする。既知のcriticalを別componentの診断失敗で隠さない一方、criticalがない場合のUNKNOWNは正常・warningへ推測補完しない。

## Optional host signals

累積counter、deployment policy、または明示的な監視対象選択に依存する signal は、既定の aggregate 出力を変えないためすべて `disabled` で開始する。必要な baseline / generation 管理や target 選択を行う deployment だけ明示的に有効化する。

| signal | opt-in mode | aggregate field | current path env | baseline path env | baseline / generation contract | standalone runbook |
| --- | --- | --- | --- | --- | --- | --- |
| host swap usage | `IRLIGHT_HOST_SWAP_PRESSURE_MODE` | `swap_pressure_status` | `IRLIGHT_SWAP_MEMINFO_PATH`（未指定時は aggregate の meminfo path） | — | baseline 不要。swap を使うかどうか自体は deployment policy | [host-swap-pressure-monitoring.md](host-swap-pressure-monitoring.md) |
| swap I/O delta | `IRLIGHT_HOST_SWAP_IO_MODE` | `swap_io_status` | `IRLIGHT_VMSTAT_PATH` | `IRLIGHT_VMSTAT_BASELINE_PATH` | 同一 host / boot generation の operator-managed baseline | [host-swap-pressure-monitoring.md](host-swap-pressure-monitoring.md) |
| OOM kill delta | `IRLIGHT_HOST_OOM_KILL_MODE` | `oom_kill_status` | `IRLIGHT_VMSTAT_PATH` | `IRLIGHT_VMSTAT_BASELINE_PATH` | 同一 host / boot generation の operator-managed baseline | [oom-kill-monitoring.md](oom-kill-monitoring.md) |
| CPU steal delta | `IRLIGHT_HOST_CPU_STEAL_MODE` | `cpu_steal_status` | `IRLIGHT_PROC_STAT_PATH` | `IRLIGHT_PROC_STAT_BASELINE_PATH` | 同一 host / boot generation の operator-managed baseline | [host-cpu-steal-monitoring.md](host-cpu-steal-monitoring.md) |
| boot generation | `IRLIGHT_HOST_BOOT_GENERATION_MODE` | `boot_generation_status` | `IRLIGHT_BOOT_ID_PATH` | `IRLIGHT_BOOT_ID_BASELINE_PATH` | persistent operator-managed baseline。planned reboot でも `WARNING` になる | [host-boot-generation-monitoring.md](host-boot-generation-monitoring.md) |
| softnet drop / time_squeeze delta | `IRLIGHT_HOST_SOFTNET_MODE` | `softnet_status` | `IRLIGHT_SOFTNET_STAT_PATH` | `IRLIGHT_SOFTNET_STAT_BASELINE_PATH` | 同一 host / boot / network namespace / CPU topology の operator-managed baseline | [softnet-pressure-monitoring.md](softnet-pressure-monitoring.md) |
| host clock synchronization | `IRLIGHT_HOST_CLOCK_SYNC_MODE` | `clock_sync_status` | `IRLIGHT_TIMEDATECTL_BIN` | — | baseline 不要。systemd / timedatectl を利用する deployment policy のみ opt-in | [host-clock-sync-monitoring.md](host-clock-sync-monitoring.md) |
| production network link | `IRLIGHT_HOST_NETWORK_LINK_MODE` | `network_link_status` | `IRLIGHT_NETWORK_INTERFACE_DIR` | — | baseline 不要。production egress interface を operator が明示し、自動選択しない | [host-network-link-monitoring.md](host-network-link-monitoring.md) |
| TCP socket memory pressure | `IRLIGHT_HOST_TCP_MEMORY_MODE` | `tcp_memory_status` | `IRLIGHT_TCP_SOCKSTAT_PATH` / `IRLIGHT_TCP_MEM_PATH` | — | baseline 不要。kernel の `tcp_mem` pressure/max watermark を read-only で評価し、repository 独自 threshold は導入しない | [tcp-memory-pressure-monitoring.md](tcp-memory-pressure-monitoring.md) |
| cgroup v2 PID pressure | `IRLIGHT_HOST_CGROUP_PIDS_MODE` | `cgroup_pids_status` | `IRLIGHT_CGROUP_PIDS_CURRENT_PATH` | `IRLIGHT_CGROUP_PIDS_MAX_PATH` | baseline 不要。監視対象 cgroup の2 control fileをoperatorが明示し、未指定時は `UNKNOWN`。parent/effective limitは推測しない | [cgroup-pid-pressure-monitoring.md](cgroup-pid-pressure-monitoring.md) |
| filesystem read-only mount | `IRLIGHT_HOST_FILESYSTEM_READONLY_MODE` | `filesystem_readonly_status` | `IRLIGHT_FILESYSTEM_PATH`（未指定時は aggregate の disk path） | — | baseline 不要。writeability が必要な filesystem path を operator が明示 | [filesystem-readonly-monitoring.md](filesystem-readonly-monitoring.md) |
| filesystem mountpoint presence | `IRLIGHT_HOST_FILESYSTEM_MOUNTPOINT_MODE` | `filesystem_mountpoint_status` | `IRLIGHT_EXPECTED_MOUNTPOINT_PATH`（未指定時は `STATE_DIR`、さらに未指定なら `/state`） | — | baseline 不要。mount が必須な path を operator が明示し、mount source / generation は別途検証 | [filesystem-mountpoint-monitoring.md](filesystem-mountpoint-monitoring.md) |

全 mode は `enabled` / `disabled` のみを受け付け、未知値は component を黙って無効化せず aggregate 自体を `UNKNOWN` に fail-closed する。有効化した component は通常 component と同じ timeout 境界と `CRITICAL > UNKNOWN > WARNING > OK` に参加する。

baseline が必要な component では、aggregate は baseline を作成・更新・削除しない。baseline 欠落、読取不能、counter reset、世代不整合などを安全に評価できない場合は component の `UNKNOWN` をそのまま伝播する。累積counterの baseline を更新する前に [host-boot-generation-monitoring.md](host-boot-generation-monitoring.md) で boot generation を確認し、予期しない reboot の証跡を自動更新で消さない。

opt-in signal の結果だけを根拠に swap 設定変更、baseline refresh、network interface変更、service/process restart、Node drain、provider migration、instance resize、failover 等を自動実行しない。詳細な意味・個別の安全境界は表の standalone runbook を参照する。

## Targeted cgroup v2 PID check

system-wide task pressure だけでは、service/container に設定された cgroup v2 の PID 上限枯渇は検知できない。監視対象の cgroup が明確な場合は、対象 cgroup の `pids.current` と `pids.max` を指定して read-only の companion check を実行する。

```bash
bash scripts/check-cgroup-pid-pressure.sh \
  /sys/fs/cgroup/<target>/pids.current \
  /sys/fs/cgroup/<target>/pids.max
```

環境変数 `IRLIGHT_CGROUP_PIDS_CURRENT_PATH` / `IRLIGHT_CGROUP_PIDS_MAX_PATH` でも path を指定できる。既定閾値は80%でwarning、90%でcriticalで、`IRLIGHT_CGROUP_PIDS_WARNING_PERCENT` / `IRLIGHT_CGROUP_PIDS_CRITICAL_PERCENT` で変更できる。

同じ明示対象を host aggregate に参加させる場合は、両 path を指定したうえで opt-in する。

```bash
IRLIGHT_HOST_CGROUP_PIDS_MODE=enabled \
IRLIGHT_CGROUP_PIDS_CURRENT_PATH=/sys/fs/cgroup/<target>/pids.current \
IRLIGHT_CGROUP_PIDS_MAX_PATH=/sys/fs/cgroup/<target>/pids.max \
  bash scripts/check-host-pressure.sh
```

aggregate は target を自動選択しない。mode を有効にして片方でも path が未指定なら `cgroup_pids_status=UNKNOWN` とし、root cgroup 等へ暗黙fallbackしない。詳細は [cgroup-pid-pressure-monitoring.md](cgroup-pid-pressure-monitoring.md) を参照する。

`pids.max` が有限値なら `pids.current / pids.max` を評価する。cgroup policy では task の移動や上限引き下げなどの organisational operation によって `pids.current > pids.max` が正規に発生し得るため、この状態は telemetry corruption ではなく `CRITICAL` として扱う。有限上限 `0` も正規の設定であり、新規 task を許容しないため `CRITICAL`（`usage_percent=NO_HEADROOM`）とする。`pids.max` が正規の `max` なら、その cgroup 自身には有限の local limit がないため `OK`（`usage_percent=NA`）とする。

PID limit は階層的であり、child の `pids.max=max` でも parent cgroup の有限上限に制約される場合がある。この check は指定した2ファイルの local 状態だけを評価し、parent cgroup の effective limit、system-wide `threads-max`、PID namespace 枯渇を推測しない。そのため既定の host aggregate へ自動追加せず、監視対象 cgroup を明示できる service/container でのみ opt-in する。

欠落・複数行・非数値・signed 64-bit範囲外は `UNKNOWN` にする。check は cgroup control file へ書き込まない。

## Diagnosis

summaryが `WARNING` / `CRITICAL` / `UNKNOWN` の場合はcomponent checkを個別に実行して詳細値とreasonを確認する。

```bash
bash scripts/check-disk-pressure.sh /state
bash scripts/check-memory-pressure.sh /proc/meminfo
bash scripts/check-load-pressure.sh /proc/loadavg
bash scripts/check-psi-pressure.sh /proc/pressure
bash scripts/check-file-handle-pressure.sh /proc/sys/fs/file-nr
bash scripts/check-conntrack-pressure.sh \
  /proc/sys/net/netfilter/nf_conntrack_count \
  /proc/sys/net/netfilter/nf_conntrack_max
bash scripts/check-task-pressure.sh \
  /proc/loadavg \
  /proc/sys/kernel/threads-max
```

`disk_status` はblock容量とinode、`memory_status` は`MemAvailable`、`load_status` は5分load average/CPU、`psi_status` はtask stall、`file_handle_status` はkernel file handle、`conntrack_status` はconnection tracking table、`task_status` はsystem-wide task/thread countの上限接近を表す。hostの`OK`をcontainer/cgroup、swap、GPU memory、network bandwidth、packet loss、processごとのfile descriptorやtask上限の余裕と読み替えない。

load/PSI/file handle/conntrack/task pressureのどれも原因processやSessionを単独では特定しない。`vmstat`、`iostat`、`ps`、`systemd-cgtop`、`lsof`、`/proc/<pid>/fd`、`conntrack -S`、NIC/process/container metricsなどで追加診断し、単純なprocess kill、table flush、再起動を自動実行しない。

PSI、file-nr、conntrack、threads-max等を読めない制限されたcontainerではhost summaryはUNKNOWNになる。対象環境で不要なcomponentを暗黙にOKへ丸めず、監視設計側で要件と除外方針を明示する。

## Safety

このwrapperとcomponent checkは診断専用で、ファイル削除、Docker prune、volume削除、process kill/restart、conntrack table flush、sysctl/cgroup変更、cache drop、swap変更、authority state変更を行わない。復旧は対象Sessionとstate ownershipを確認した別の明示操作として実施する。

監視systemから呼び出す場合も、exit codeを契機に破壊的な自動復旧を直結させない。まずalertと診断へ接続し、復旧操作は個別runbookの条件を満たした場合に限る。

## Verification

```bash
python -m unittest discover -s tests -p 'test_load_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_psi_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_file_handle_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_conntrack_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_task_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_cgroup_pid_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_cgroup_pid_aggregate.py' -v
python -m unittest discover -s tests -p 'test_host_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_network_link_aggregate.py' -v
python -m unittest discover -s tests -p 'test_host_filesystem_readonly_aggregate.py' -v
python -m unittest discover -s tests -p 'test_host_filesystem_mountpoint_aggregate.py' -v
python -m unittest discover -s tests -p 'test_host_pressure_opt_in_matrix.py' -v
bash -n \
  scripts/check-load-pressure.sh \
  scripts/check-psi-pressure.sh \
  scripts/check-file-handle-pressure.sh \
  scripts/check-conntrack-pressure.sh \
  scripts/check-task-pressure.sh \
  scripts/check-cgroup-pid-pressure.sh \
  scripts/check-host-cgroup-pid-pressure.sh \
  scripts/check-network-link-health.sh \
  scripts/check-host-filesystem-readonly.sh \
  scripts/check-host-filesystem-mountpoint.sh \
  scripts/check-host-pressure.sh
```
