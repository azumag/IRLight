# cgroup v2 swap pressure monitoring

Issue #11 の service/container 単位の resource pressure 診断を補完するため、cgroup v2 の `memory.swap.current` と `memory.swap.max` を read-only で評価する。

Host 全体の memory pressure や対象 cgroup の `memory.max` が正常でも、有限の swap 上限へ近づいている workload は reclaim / OOM の余裕が小さくなる。逆に swap を意図的に無効化した workload もあるため、`memory.swap.max=0` そのものを障害とは扱わず、現在使用量が 0 なら `OK` とする。

## Usage

```bash
bash scripts/check-cgroup-swap-pressure.sh \
  /sys/fs/cgroup/<target>/memory.swap.current \
  /sys/fs/cgroup/<target>/memory.swap.max
```

path は `IRLIGHT_CGROUP_SWAP_CURRENT_PATH` / `IRLIGHT_CGROUP_SWAP_MAX_PATH` でも指定できる。既定閾値は 80% で `WARNING`、90% で `CRITICAL` とし、`IRLIGHT_CGROUP_SWAP_WARNING_PERCENT` / `IRLIGHT_CGROUP_SWAP_CRITICAL_PERCENT` で変更できる。

有限の正の `memory.swap.max` では `memory.swap.current / memory.swap.max` を評価する。

- warning 未満: `OK`
- warning 以上 critical 未満: `WARNING`
- critical 以上: `CRITICAL`
- `memory.swap.current > memory.swap.max`: `CRITICAL` (`usage_percent=OVER_LIMIT`)
- `memory.swap.max=0` かつ current も 0: `OK` (`usage_percent=NA`)。swap 禁止 policy 自体を pressure と誤判定しない
- `memory.swap.max=max`: local finite swap limit がないため `OK` (`usage_percent=NA`)
- 欠落、読取不能、複数行、非数値、signed 64-bit 範囲外、不正な閾値: `UNKNOWN`

`memory.swap.max=max` は swap が十分に利用可能であることを意味しない。parent cgroup の制約、host の swap availability、通常 memory の `memory.max` / `memory.high`、PSI は別 signal で確認する。

## Host aggregate opt-in

同じ明示対象を host pressure aggregate に含める場合は、2つの control file を指定したうえで opt-in する。

```bash
IRLIGHT_HOST_CGROUP_SWAP_MODE=enabled \
IRLIGHT_CGROUP_SWAP_CURRENT_PATH=/sys/fs/cgroup/<target>/memory.swap.current \
IRLIGHT_CGROUP_SWAP_MAX_PATH=/sys/fs/cgroup/<target>/memory.swap.max \
  bash scripts/check-host-pressure.sh
```

`IRLIGHT_HOST_CGROUP_SWAP_MODE` の既定値は `disabled`。有効化した場合だけ `cgroup_swap_status=<status>` を host summary に追加する。host aggregate は監視対象 cgroup を自動選択せず、2 path の片方でも未指定なら root/current cgroup へ fallback せず `cgroup_swap_status=UNKNOWN` とする。standalone checker の 80%/90%、`max`、`0/0`、`OVER_LIMIT` semantics をそのまま再利用し、aggregate 独自の threshold は追加しない。

host 全体の swap usage (`IRLIGHT_HOST_SWAP_PRESSURE_MODE`) とは別 signal であり、両方を有効化しても一方を他方の代替とは扱わない。

## Safety boundary

この check と host aggregate adapter は cgroup control file を読み取るだけで、`memory.swap.max` の変更、`swapon` / `swapoff`、reclaim、OOM kill、process/container restart は行わない。

また `check-cgroup-runtime-pressure.sh` / `check-host-pressure.sh` へ既定では組み込まない。kernel / container runtime / deployment policy によって swap accounting/control file の利用可否が異なるため、対象 workload で `memory.swap.current` / `memory.swap.max` を監視対象として明示できる環境だけで opt-in する。control file が存在しない環境を aggregate 全体の `UNKNOWN` に変えることを避けるためである。

## Output / exit code

例:

```text
IRLIGHT_CGROUP_SWAP_PRESSURE status=WARNING usage_percent=80 current_bytes=838860800 maximum_bytes=1048576000 warning_percent=80 critical_percent=90
```

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | finite limit に対する usage が warning 未満、swap limit が `max`、または swap-disabled (`0/0`) |
| 1 | `WARNING` | usage が warning 以上 critical 未満 |
| 2 | `CRITICAL` | usage が critical 以上、または current が finite maximum を超過 |
| 3 | `UNKNOWN` | 対象を安全に読み取り・評価できない |

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_swap_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_cgroup_swap_aggregate.py' -v
bash -n scripts/check-cgroup-swap-pressure.sh
bash -n scripts/check-host-cgroup-swap-pressure.sh
bash -n scripts/check-host-pressure.sh
```

監視結果を破壊的な自動復旧へ直結させず、まず alert / runbook の診断 signal として利用する。
