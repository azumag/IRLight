# cgroup v2 memory pressure monitoring

Issue #11 の host-level pressure 監視を補完するため、service/container 単位の cgroup v2 memory control files を read-only で確認する。

Host の `/proc/meminfo` が正常でも、個別 cgroup の `memory.max` に近づくと対象 workload だけが reclaim/OOM の影響を受ける。また `memory.high` を設定している環境では、hard limit 到達前から throttle と強い reclaim pressure が発生するため、対象 cgroup が明確な環境では companion check として利用する。

## Hard-limit check (`memory.max`)

```bash
bash scripts/check-cgroup-memory-pressure.sh \
  /sys/fs/cgroup/<target>/memory.current \
  /sys/fs/cgroup/<target>/memory.max
```

path は `IRLIGHT_CGROUP_MEMORY_CURRENT_PATH` / `IRLIGHT_CGROUP_MEMORY_MAX_PATH` でも指定できる。既定閾値は 80% で warning、90% で critical とし、`IRLIGHT_CGROUP_MEMORY_WARNING_PERCENT` / `IRLIGHT_CGROUP_MEMORY_CRITICAL_PERCENT` で変更できる。

有限の `memory.max` では `memory.current / memory.max` を評価する。有限上限 `0` は有効な設定であり、新たな allocation headroom がないため `CRITICAL` (`usage_percent=NO_HEADROOM`) とする。上限変更や reclaim/OOM 処理の境界では `memory.current > memory.max` が一時的に観測され得るため、壊れた telemetry と推測せず `CRITICAL` (`usage_percent=OVER_LIMIT`) とする。

`memory.max=max` は指定した cgroup 自身に有限の local hard limit がないことを表すため、この check は `OK` (`usage_percent=NA`) を返す。ただし cgroup v2 の memory controller は階層的であり、parent cgroup の有限上限、`memory.high`、swap、host 全体の memory pressure が安全だという意味ではない。

## Throttle-boundary check (`memory.high`)

`memory.high` を明示的に設定している workload では、hard limit より前の reclaim/throttle pressure を別 check で評価できる。

```bash
bash scripts/check-cgroup-memory-high-pressure.sh \
  /sys/fs/cgroup/<target>/memory.current \
  /sys/fs/cgroup/<target>/memory.high
```

path は `IRLIGHT_CGROUP_MEMORY_HIGH_CURRENT_PATH` / `IRLIGHT_CGROUP_MEMORY_HIGH_PATH` でも指定できる。既定閾値は 80% / 90% で、`IRLIGHT_CGROUP_MEMORY_HIGH_WARNING_PERCENT` / `IRLIGHT_CGROUP_MEMORY_HIGH_CRITICAL_PERCENT` で変更できる。

Linux cgroup v2 の `memory.high` は hard cap ではなく memory usage throttle limit である。usage が境界を超えると対象 cgroup の process は throttle され強い reclaim pressure を受ける一方、OOM killer は `memory.high` 超過だけでは呼ばれず、極端な条件では usage が high boundary を超えたままになることがある。そのため `memory.current > memory.high` は壊れた telemetry ではなく実際の pressure として `CRITICAL` (`usage_percent=OVER_HIGH`) にする。

有限の `memory.high=0` は unthrottled allocation headroom がないため `CRITICAL` (`usage_percent=NO_HEADROOM`) とする。`memory.high=max` は指定した cgroup 自身に local high boundary がないため `OK` (`usage_percent=NA`) とするが、`memory.max`、parent cgroup、swap、host memory が安全という意味ではない。

両 check とも欠落、複数行、非数値、signed 64-bit 範囲外は `UNKNOWN` に fail-closed する。control file は変更せず、reclaim、OOM kill、process/container restart、cache drop、`memory.high` / `memory.max` の書換えを行わない。

## Output / exit code

hard-limit check の正常時の例:

```text
IRLIGHT_CGROUP_MEMORY_PRESSURE status=OK usage_percent=42 current_bytes=440401920 maximum_bytes=1048576000 warning_percent=80 critical_percent=90
```

`memory.high` check の正常時の例:

```text
IRLIGHT_CGROUP_MEMORY_HIGH_PRESSURE status=OK usage_percent=42 current_bytes=440401920 high_bytes=1048576000 warning_percent=80 critical_percent=90
```

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | finite boundary に対する usage が warning 未満、または local boundary が `max` |
| 1 | `WARNING` | usage が warning 以上 critical 未満 |
| 2 | `CRITICAL` | usage が critical 以上、finite boundary 0、または current が finite boundary を超過 |
| 3 | `UNKNOWN` | 対象を安全に読み取り・評価できない |

## Scope

これらの check は指定した cgroup の local `memory.current` と、明示した `memory.max` または `memory.high` だけを評価する。次の signal は別途確認する。

- host 全体の `MemAvailable`: `scripts/check-memory-pressure.sh`
- host の memory/IO stall: `scripts/check-psi-pressure.sh`
- service/container の PID limit: `scripts/check-cgroup-pid-pressure.sh`
- parent cgroup の effective memory constraints
- `memory.events` の OOM / OOM kill / high counter の時間差分
- swap limit / usage

root/current cgroup を機械的に選ぶと、監視対象 workload と異なる階層を見たり local boundary が `max` だけなのを見て安全と誤認する可能性がある。このため既定の `check-host-pressure.sh` へ自動追加せず、監視対象 service/container の cgroup を運用側で明示して opt-in する。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_memory_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_cgroup_memory_high_pressure_check.py' -v
bash -n scripts/check-cgroup-memory-pressure.sh
bash -n scripts/check-cgroup-memory-high-pressure.sh
```

監視結果を破壊的な自動復旧へ直結させず、まず alert と診断へ接続する。復旧操作は対象 Session / process / state ownership を確認した runbook に従う。
