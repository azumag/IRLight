# cgroup runtime pressure aggregate

Issue #11 の service/container 単位の read-only 診断では、`check-cgroup-runtime-pressure.sh` で cgroup v2 の主要 pressure signal を一度に評価できる。

既定で評価するのは次の4系統である。

- `memory.current` / `memory.max`
- `memory.current` / `memory.high`
- `pids.current` / `pids.max`
- `cpu.pressure` / `memory.pressure` / `io.pressure`

```bash
bash scripts/check-cgroup-runtime-pressure.sh /sys/fs/cgroup/<target>
```

対象 cgroup は第1引数または `IRLIGHT_CGROUP_RUNTIME_DIR` で必ず明示する。root/current cgroup は自動選択しない。

## Optional components

追加 signal は deployment ごとの差異や監視対象 generation を誤認しないよう、すべて明示 opt-in にする。

### memory.events delta

同一 cgroup generation の baseline がある場合だけ第2引数または `IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH` を指定する。

```bash
bash scripts/check-cgroup-runtime-pressure.sh \
  /sys/fs/cgroup/<target> \
  /run/irlight-monitor/<target>.memory.events.baseline
```

baseline 未指定時は `memory_events_status=NOT_CONFIGURED` となる。aggregate 自身は baseline を作成・更新しない。

### Process file descriptor pressure

代表 process を運用側で決められる場合だけ、その `/proc/<pid>` directory を第3引数または `IRLIGHT_CGROUP_RUNTIME_PROCESS_DIR` で指定する。

```bash
PID=<target-pid>
bash scripts/check-cgroup-runtime-pressure.sh \
  /sys/fs/cgroup/<target> \
  "" \
  "/proc/${PID}"
```

未指定時は `process_fd_status=NOT_CONFIGURED` となる。cgroup 内の代表 PID を aggregate が自動選択することはない。

### CPU throttling delta

cgroup v2 `cpu.stat` の `nr_throttled` / `throttled_usec` は累積 counter なので、生の値だけでは「現在も throttling が増えているか」を判断できない。同一 cgroup generation の過去 snapshot を baseline として operator / monitoring 側で保持し、`IRLIGHT_CGROUP_CPU_STAT_BASELINE_PATH` へ明示する。

```bash
IRLIGHT_CGROUP_CPU_STAT_BASELINE_PATH=/run/irlight-monitor/<target>.cpu.stat.baseline \
  bash scripts/check-cgroup-runtime-pressure.sh /sys/fs/cgroup/<target>
```

baseline を指定した場合だけ、aggregate は第1引数と **同じ cgroup** の `cpu.stat` を `check-cgroup-cpu-throttling.sh` で比較し、`cpu_throttling_status=<status>` を出力へ追加する。baseline 未指定時はこの component 自体を実行せず、既存 stdout / exit code 形式を変更しない。

standalone 診断も利用できる。

```bash
bash scripts/check-cgroup-cpu-throttling.sh \
  /sys/fs/cgroup/<target>/cpu.stat \
  /run/irlight-monitor/<target>.cpu.stat.baseline
```

`nr_throttled` または `throttled_usec` に新しい増加があれば `WARNING`、増加がなければ `OK` とする。`cpu.stat` counter だけでは sampling interval、CPU quota に対する実効割合、映像・音声処理への影響を確定できないため、この checker 単独では `CRITICAL` にしない。重大度は cgroup PSI、media pipeline の frame/packet 症状、CPU quota 設定、同時 Session の状態と合わせて判断する。

current / baseline の欠損・読取不能、不正 record、解釈対象 counter の重複、必須 counter 欠損、counter reset は `UNKNOWN` に fail-closed する。新しい kernel が未知 counter を追加した場合は record shape と整数値を検証したうえで、その未知 counter 自体は無視する。aggregate/checker は baseline を作成・更新・削除しない。

### Swap pressure

`memory.swap.current` / `memory.swap.max` がその deployment で利用可能であり、swap accounting を監視対象に含めると明示的に決めた場合だけ、第4引数へ `enabled` を渡すか `IRLIGHT_CGROUP_RUNTIME_SWAP_MODE=enabled` を設定する。

swap は第1引数で指定した **同じ cgroup** の control fileだけを読む。別 cgroup の path を個別指定できる形にはせず、memory / pids / PSI と swap の監視対象が意図せず混在することを防ぐ。

```bash
CGROUP=/sys/fs/cgroup/<target>
bash scripts/check-cgroup-runtime-pressure.sh \
  "$CGROUP" \
  "" \
  "" \
  enabled
```

または次のように環境変数で明示できる。

```bash
IRLIGHT_CGROUP_RUNTIME_SWAP_MODE=enabled \
  bash scripts/check-cgroup-runtime-pressure.sh /sys/fs/cgroup/<target>
```

既定値は `disabled` であり、既存 monitoring parser との互換性を保つため、swap を `enabled` にしたときだけ `swap_status=<status>` を末尾へ追加し、既存の `check-cgroup-swap-pressure.sh` による判定を aggregate へ含める。CPU throttling baseline も未指定であれば、既定出力は従来どおり `process_fd_status` で終わる。

swap control file は kernel / container runtime / delegation policy によって存在しない場合があるため、既定で自動追加しない。明示 opt-in 後に control file が欠落・読取不能・不正であれば `UNKNOWN` に fail-closed する。mode は `enabled` / `disabled` だけを受理し、それ以外は `UNKNOWN` (`invalid_swap_mode`) として、曖昧な設定を黙って無効化しない。

swap checker は有限 limit の warning/critical、`0/0` の意図的 swap-disabled、`max` の unlimited、over-limit、不正 telemetry を既存契約どおり評価する。

## Aggregate precedence

component の exit code は次の優先順位で集約する。

```text
CRITICAL > UNKNOWN > WARNING > OK
```

確定した `CRITICAL` を、別 component の `UNKNOWN` で隠さない。一方、critical がない場合に安全に評価できない component があれば aggregate は `UNKNOWN` とする。

CPU throttling opt-in 時の出力例:

```text
IRLIGHT_CGROUP_RUNTIME_PRESSURE status=WARNING memory_max_status=OK memory_high_status=OK pids_status=OK psi_status=OK memory_events_status=NOT_CONFIGURED process_fd_status=NOT_CONFIGURED cpu_throttling_status=WARNING
```

swap と CPU throttling を両方 opt-in した場合は、両方の component status を出力する。

各 component は既定10秒で bounded execution とし、`IRLIGHT_CGROUP_COMPONENT_TIMEOUT_SECONDS` で1〜300秒へ変更できる。timeout、不正 timeout、`timeout` utility 不在、契約外 exit codeは `UNKNOWN` に fail-closed し、無期限実行へフォールバックしない。

## Safety

この aggregate と CPU throttling checker は診断専用である。cgroup control file、baseline、process、file descriptor、resource limit、swap設定を変更しない。reclaim、kill/restart、`swapon` / `swapoff`、CPU quota変更、baseline更新も行わない。出力へ cgroup path や内部識別子を追加しない。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_runtime_pressure.py' -v
python -m unittest discover -s tests -p 'test_cgroup_cpu_throttling_check.py' -v
python -m unittest discover -s tests -p 'test_cgroup_cpu_throttling_aggregate.py' -v
python -m unittest discover -s tests -p 'test_cgroup_swap_pressure_check.py' -v
bash -n scripts/check-cgroup-runtime-pressure.sh
bash -n scripts/check-cgroup-cpu-throttling.sh
bash -n scripts/check-cgroup-swap-pressure.sh
```
