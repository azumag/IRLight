# Host swap pressure monitoring

Issue #11 の host memory 診断を補完するため、Linux の `/proc/meminfo` にある `SwapTotal` / `SwapFree` と、`/proc/vmstat` にある累積 swap I/O counter を副作用なく確認する targeted checks を定義する。

`MemAvailable` が十分でも swap pool が高使用率になっていることはあり、逆に swap を意図的に無効化している host もある。そのため usage check は既存の `check-host-pressure.sh` aggregate へ自動追加しない。swap I/O delta も既定では aggregate 無効のままとし、同一 boot generation の baseline を管理できる deployment だけ明示的に opt-in する。

## Usage check

```bash
bash scripts/check-host-swap-pressure.sh /proc/meminfo
```

path は第1引数または `IRLIGHT_SWAP_MEMINFO_PATH` で指定できる。既定は `/proc/meminfo`。

初期閾値は既存の cgroup swap pressure check と揃え、使用率 80% 以上を `WARNING`、90% 以上を `CRITICAL` とする。deployment の実測に合わせて次で変更できる。

```text
IRLIGHT_SWAP_WARNING_PERCENT
IRLIGHT_SWAP_CRITICAL_PERCENT
```

閾値は 0〜100 の10進整数で `warning < critical` を必須とする。不正値は推測補完せず `UNKNOWN` にする。

### Usage semantics

`SwapTotal` と `SwapFree` はそれぞれ exactly one record、単位 `kB`、signed 64-bit 範囲内の非負整数であることを要求する。`SwapFree > SwapTotal`、欠落、重複、単位不正、非数値は `UNKNOWN` とする。

swap が無効な host で kernel が次を返す場合は、それ自体を pressure とみなさない。

```text
SwapTotal: 0 kB
SwapFree:  0 kB
```

この場合は `status=OK usage_percent=NA`。swap 無効が deployment policy として適切かどうかは別の構成判断であり、この read-only checker は swap を有効化しない。

有限 swap pool がある場合は `used = SwapTotal - SwapFree` の整数使用率を評価する。例えば swap が完全に使い切られ `SwapFree=0` なら `usage_percent=100` となり `CRITICAL` になる。

出力例:

```text
IRLIGHT_HOST_SWAP_PRESSURE status=WARNING usage_percent=82 free_kb=180000 total_kb=1000000 warning_percent=80 critical_percent=90
```

exit code:

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | swap 無効、または有限 swap pool が warning 未満 |
| 1 | `WARNING` | 使用率が warning 以上 critical 未満 |
| 2 | `CRITICAL` | 使用率が critical 以上 |
| 3 | `UNKNOWN` | meminfo / threshold を安全に評価できない |

## Swap I/O delta companion

swap pool の使用率が低くても短時間に swap-in / swap-out が繰り返されている場合があるため、`/proc/vmstat` の `pswpin` / `pswpout` を operator-managed baseline と比較する companion check を用意する。

```bash
bash scripts/check-swap-io-delta.sh /proc/vmstat <operator-managed-baseline>
```

第1引数は current `/proc/vmstat`、第2引数は同じ host / boot generation から保存した baseline。引数を省略する場合は `IRLIGHT_VMSTAT_PATH` と `IRLIGHT_VMSTAT_BASELINE_PATH` を使用できる。checker 自身は baseline を作成・更新しない。

`pswpin` / `pswpout` は exactly one record、signed 64-bit 範囲内の非負10進 counter として検証する。どちらかが baseline より減少した場合は reboot / generation mismatch 等を推測せず `UNKNOWN reason=counter_reset` とする。累積 counter の世代確認には `check-host-boot-generation.sh` も組み合わせる。

- 増加なし: `OK` / exit `0`。
- `pswpin` または `pswpout` が1以上増加: `WARNING` / exit `1`, reason `swap_io_activity`。
- baseline/current 欠損、counter 欠落・重複・不正値、counter reset: `UNKNOWN` / exit `3`。

例:

```text
IRLIGHT_SWAP_IO status=WARNING reason=swap_io_activity pswpin_delta=8 pswpout_delta=16
```

この WARNING は swap thrashing や user-visible latency を単独で証明しない。小さな background activity でも増加し得るため、PSI、memory pressure、swap 使用率、OOM、service latency と相関する targeted signal として扱う。deployment 固有の閾値や自動 remediation はこの checker では導入しない。

### Host aggregate opt-in

同一 boot generation の baseline を運用側で管理できる場合に限り、swap I/O companion を既存 host-pressure aggregate へ明示的に追加できる。

```bash
IRLIGHT_HOST_SWAP_IO_MODE=enabled \
IRLIGHT_VMSTAT_PATH=/proc/vmstat \
IRLIGHT_VMSTAT_BASELINE_PATH=/var/lib/irlight-monitoring/vmstat.baseline \
  bash scripts/check-host-pressure.sh
```

`IRLIGHT_HOST_SWAP_IO_MODE` は `enabled` / `disabled` のみを受け付け、既定は `disabled`。既定時は従来の `IRLIGHT_HOST_PRESSURE` 出力を変更しない。`enabled` 時だけ `swap_io_status=OK|WARNING|UNKNOWN` を追加し、同じ severity aggregation と component timeout の境界に参加させる。

baseline が未設定・読取不能、counter が不正、世代が一致しない場合は `swap_io_status=UNKNOWN` として fail-closed になる。無効な mode 値も aggregate 自体を `UNKNOWN reason=invalid_swap_io_mode` にする。aggregate は baseline を作成・更新せず、swap usage checker を暗黙に有効化もしない。

## Diagnosis

`WARNING` / `CRITICAL` は swap 関連 signal で、host memory exhaustion や OOM の発生を単独で証明しない。同じ時間帯の以下を照合する。

```bash
bash scripts/check-memory-pressure.sh /proc/meminfo
bash scripts/check-psi-pressure.sh /proc/pressure
bash scripts/check-host-swap-pressure.sh /proc/meminfo
bash scripts/check-swap-io-delta.sh /proc/vmstat <operator-managed-baseline>
bash scripts/check-oom-kill-delta.sh /proc/vmstat <operator-managed-baseline>
```

対象 service/container が明確なら cgroup v2 の `memory.current` / `memory.max` / `memory.high`、`memory.swap.current` / `memory.swap.max`、`memory.events` も確認する。swap I/O delta は churn の有無を補助するが I/O stall の深刻度までは示さないため、PSI と service latency も合わせて確認する。

## Safety

これらの check は `/proc/meminfo` / `/proc/vmstat` と operator-managed baseline を読むだけで、`swapon` / `swapoff`、swapfile 作成・削除、baseline 書換え、sysctl、cgroup limit、cache drop、process kill/restart、Node drain、Session stop を実行しない。

`CRITICAL` / `WARNING` を検出しても swap 設定や service を自動変更しない。まず memory / PSI / swap I/O / OOM / cgroup / process の証跡を確認し、進行中 Session への影響を特定してから別の明示的な復旧判断を行う。

## Verification

```bash
python -m unittest discover -s tests -p 'test_host_swap_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_swap_pressure_runbook_inventory.py' -v
python -m unittest discover -s tests -p 'test_swap_io_delta_check.py' -v
python -m unittest discover -s tests -p 'test_host_swap_io_runbook_inventory.py' -v
python -m unittest discover -s tests -p 'test_host_swap_io_aggregate.py' -v
bash -n scripts/check-host-swap-pressure.sh
bash -n scripts/check-swap-io-delta.sh
bash -n scripts/check-host-pressure.sh
```
