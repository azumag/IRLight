# Host swap pressure monitoring

Issue #11 の host memory 診断を補完するため、Linux の `/proc/meminfo` にある `SwapTotal` / `SwapFree` を副作用なく確認する targeted check を定義する。

`MemAvailable` が十分でも swap pool が高使用率になっていることはあり、逆に swap を意図的に無効化している host もある。そのため、この check は既存の `check-host-pressure.sh` aggregate へ自動追加せず、swap を運用対象にする deployment で opt-in する。

## Check

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

## Semantics

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

## Diagnosis

`WARNING` / `CRITICAL` は swap 使用率だけの signal で、host memory exhaustion や OOM の発生を単独で証明しない。同じ時間帯の以下を照合する。

```bash
bash scripts/check-memory-pressure.sh /proc/meminfo
bash scripts/check-psi-pressure.sh /proc/pressure
bash scripts/check-oom-kill-delta.sh /proc/vmstat <operator-managed-baseline>
```

対象 service/container が明確なら cgroup v2 の `memory.current` / `memory.max` / `memory.high`、`memory.swap.current` / `memory.swap.max`、`memory.events` も確認する。swap thrashing の頻度や I/O stall はこの check の使用率だけでは分からないため、PSI、`vmstat`、service latency 等と組み合わせる。

## Safety

この check は `/proc/meminfo` を読むだけで、`swapon` / `swapoff`、swapfile 作成・削除、sysctl、cgroup limit、cache drop、process kill/restart、Node drain、Session stop を実行しない。

`CRITICAL` を検出しても swap 設定や service を自動変更しない。まず memory / PSI / OOM / cgroup / process の証跡を確認し、進行中 Session への影響を特定してから別の明示的な復旧判断を行う。

## Verification

```bash
python -m unittest discover -s tests -p 'test_host_swap_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_swap_pressure_runbook_inventory.py' -v
bash -n scripts/check-host-swap-pressure.sh
```
