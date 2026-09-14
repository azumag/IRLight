# cgroup v2 memory pressure monitoring

Issue #11 の host-level pressure 監視を補完するため、service/container 単位の cgroup v2 memory control files を read-only で確認する。

Host の `/proc/meminfo` が正常でも、個別 cgroup の `memory.max` に近づくと対象 workload だけが reclaim/OOM の影響を受ける。また `memory.high` を設定している環境では、hard limit 到達前から throttle と強い reclaim pressure が発生するため、対象 cgroup が明確な環境では companion check として利用する。現在値だけでは過去の OOM / reclaim 発生を確認できないため、`memory.events` の累積 counter も別 check で前回 snapshot と比較できる。

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

これら2つの boundary check は欠落、複数行、非数値、signed 64-bit 範囲外を `UNKNOWN` に fail-closed する。control file は変更せず、reclaim、OOM kill、process/container restart、cache drop、`memory.high` / `memory.max` の書換えを行わない。

## Event-delta check (`memory.events`)

`memory.events` の counter は累積値なので、生の `oom_kill > 0` 等をそのまま alert 条件にすると一度の過去障害で永続的に alert し続ける。`check-cgroup-memory-events.sh` は同一 cgroup generation の過去 snapshot を baseline として明示的に渡し、その後に増えた event だけを評価する。

```bash
bash scripts/check-cgroup-memory-events.sh \
  /sys/fs/cgroup/<target>/memory.events \
  /run/irlight-monitor/<target>.memory.events.baseline
```

第1引数は `IRLIGHT_CGROUP_MEMORY_EVENTS_PATH`、第2引数は `IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH` でも指定できる。baseline は監視側が事前に read-only snapshot として保存・更新するもので、この script 自身は cgroup control file も baseline file も作成・更新しない。

判定は baseline から current への増分に限定する。

- `oom` / `oom_kill` / `oom_group_kill` が1以上増加: `CRITICAL` (`oom_activity`)
- `low` / `high` / `max` が1以上増加し、OOM系増分なし: `WARNING` (`memory_pressure_activity`)
- 監視対象 counter の増分がすべて0: `OK`
- current が baseline より小さい counter がある: `UNKNOWN` (`counter_reset`)。cgroup の再作成や baseline の世代不一致を正常扱いしない
- snapshot 欠落、必須 counter 欠落、重複・非数値・signed 64-bit 範囲外など: `UNKNOWN`

`low` / `high` / `max` は reclaim / throttle / hard-boundary activity の発生証跡として warning にし、実 OOM / kill が観測されたときだけ critical に格上げする。kernel version 差を考慮して `oom_group_kill` は任意 counter とし、存在しない場合は0として扱う。一方、将来 kernel が未知の counter を追加しても、その record が単一の非負整数であることを検証した上で、この check が意味を定義していない counter は判定から除外する。

出力例:

```text
IRLIGHT_CGROUP_MEMORY_EVENTS status=CRITICAL reason=oom_activity low_delta=0 high_delta=2 max_delta=1 oom_delta=1 oom_kill_delta=1 oom_group_kill_delta=0
```

この check は「baseline 取得後に event が発生したか」を示すだけで、現在の memory headroom や pressure が継続中かは証明しない。`memory.current` / `memory.max` / `memory.high` と cgroup PSI を併用し、baseline は必ず同じ監視対象 cgroup generation に対応させる。監視側が新しい baseline を採用するタイミングは alert の確認・記録方針と合わせて定義し、script が自動で履歴を消費したり reset したりしないようにする。

## Targeted runtime aggregate

同じ service/container cgroup について hard limit、throttle boundary、PID limit、PSI を個別に配線する代わりに、read-only aggregate で一度に評価できる。

```bash
bash scripts/check-cgroup-runtime-pressure.sh /sys/fs/cgroup/<target>
```

第1引数は `IRLIGHT_CGROUP_RUNTIME_DIR` でも指定できる。対象 cgroup は必須であり、root/current cgroup を自動選択しない。aggregate は `memory.current` / `memory.max`、`memory.current` / `memory.high`、`pids.current` / `pids.max`、`cpu.pressure` / `memory.pressure` / `io.pressure` を既存 checker へ渡し、`CRITICAL > UNKNOWN > WARNING > OK` の優先順位で集約する。

`memory.events` は世代が一致する baseline が必要な stateful delta なので既定では実行しない。同じ cgroup generation の baseline を明示できる場合だけ第2引数（または `IRLIGHT_CGROUP_MEMORY_EVENTS_BASELINE_PATH`）で opt-in する。

```bash
bash scripts/check-cgroup-runtime-pressure.sh \
  /sys/fs/cgroup/<target> \
  /run/irlight-monitor/<target>.memory.events.baseline
```

baseline 未指定時は `memory_events_status=NOT_CONFIGURED` と表示し、それ自体では aggregate を悪化させない。baseline を指定した場合は `memory.events` delta を同じ優先順位へ含める。aggregate 自身は baseline を作成・更新しない。

各 component は既定10秒で bounded execution とし、`IRLIGHT_CGROUP_COMPONENT_TIMEOUT_SECONDS` で 1〜300 秒へ変更できる。timeout、不正な timeout 設定、`timeout` utility 不在、契約外 exit code は `UNKNOWN` に fail-closed し、無期限実行へフォールバックしない。ある component が `UNKNOWN` でも別 component の確定した `CRITICAL` は隠さない。

出力例:

```text
IRLIGHT_CGROUP_RUNTIME_PRESSURE status=OK memory_max_status=OK memory_high_status=OK pids_status=OK psi_status=OK memory_events_status=NOT_CONFIGURED
```

この aggregate は診断のみであり、cgroup control file、baseline、process/container、resource limit を変更しない。対象 workload の cgroup path と baseline generation の選択は deployment/operator 側の責任範囲に残す。

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
| 0 | `OK` | finite boundary に対する usage が warning 未満、local boundary が `max`、または監視対象 event の新規増分がない |
| 1 | `WARNING` | usage が warning 以上 critical 未満、または OOM を伴わない `low` / `high` / `max` event が増加 |
| 2 | `CRITICAL` | usage が critical 以上、finite boundary 0、current が finite boundary を超過、または OOM 系 event が増加 |
| 3 | `UNKNOWN` | 対象を安全に読み取り・評価できない、または baseline と current の counter 世代が整合しない |

## Scope

これらの check は指定した cgroup の local `memory.current`、明示した `memory.max` / `memory.high`、および明示した2時点の `memory.events` だけを評価する。次の signal は別途確認する。

- host 全体の `MemAvailable`: `scripts/check-memory-pressure.sh`
- host の memory/IO stall: `scripts/check-psi-pressure.sh`
- service/container の PID limit: `scripts/check-cgroup-pid-pressure.sh`
- service/container の stall: `scripts/check-cgroup-psi-pressure.sh`
- parent cgroup の effective memory constraints
- swap limit / usage

root/current cgroup を機械的に選ぶと、監視対象 workload と異なる階層を見たり local boundary が `max` だけなのを見て安全と誤認する可能性がある。このため既定の `check-host-pressure.sh` へ自動追加せず、監視対象 service/container の cgroup を運用側で明示して opt-in する。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_memory_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_cgroup_memory_high_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_cgroup_memory_events_check.py' -v
python -m unittest discover -s tests -p 'test_cgroup_runtime_pressure.py' -v
bash -n scripts/check-cgroup-memory-pressure.sh
bash -n scripts/check-cgroup-memory-high-pressure.sh
bash -n scripts/check-cgroup-memory-events.sh
bash -n scripts/check-cgroup-runtime-pressure.sh
```

監視結果を破壊的な自動復旧へ直結させず、まず alert と診断へ接続する。復旧操作は対象 Session / process / state ownership を確認した runbook に従う。
