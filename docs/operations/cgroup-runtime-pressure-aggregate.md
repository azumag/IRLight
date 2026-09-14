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

既定値は `disabled` で、`swap_status=NOT_CONFIGURED` となり aggregate の status を悪化させない。`enabled` の場合は既存の `check-cgroup-swap-pressure.sh` を用い、有限 limit の warning/critical、`0/0` の意図的 swap-disabled、`max` の unlimited、over-limit、不正 telemetry を同じ契約で評価する。

swap control file は kernel / container runtime / delegation policy によって存在しない場合があるため、既定で自動追加しない。明示 opt-in 後に control file が欠落・読取不能・不正であれば `UNKNOWN` に fail-closed する。mode は `enabled` / `disabled` だけを受理し、それ以外は `UNKNOWN` (`invalid_swap_mode`) として、曖昧な設定を黙って無効化しない。

## Aggregate precedence

component の exit code は次の優先順位で集約する。

```text
CRITICAL > UNKNOWN > WARNING > OK
```

確定した `CRITICAL` を、別 component の `UNKNOWN` で隠さない。一方、critical がない場合に安全に評価できない component があれば aggregate は `UNKNOWN` とする。

出力例:

```text
IRLIGHT_CGROUP_RUNTIME_PRESSURE status=WARNING memory_max_status=OK memory_high_status=OK pids_status=OK psi_status=OK memory_events_status=NOT_CONFIGURED process_fd_status=NOT_CONFIGURED swap_status=WARNING
```

各 component は既定10秒で bounded execution とし、`IRLIGHT_CGROUP_COMPONENT_TIMEOUT_SECONDS` で1〜300秒へ変更できる。timeout、不正 timeout、`timeout` utility 不在、契約外 exit codeは `UNKNOWN` に fail-closed し、無期限実行へフォールバックしない。

## Safety

この aggregate は診断専用である。cgroup control file、baseline、process、file descriptor、resource limit、swap設定を変更しない。reclaim、kill/restart、`swapon` / `swapoff`、limit変更、baseline更新も行わない。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_runtime_pressure.py' -v
python -m unittest discover -s tests -p 'test_cgroup_swap_pressure_check.py' -v
bash -n scripts/check-cgroup-runtime-pressure.sh
bash -n scripts/check-cgroup-swap-pressure.sh
```
