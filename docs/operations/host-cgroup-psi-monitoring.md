# Host-side targeted cgroup PSI monitoring

Issue #11 の host monitoring から service/container 単位の cgroup v2 Pressure Stall Information (PSI) を安全に参照するため、`check-host-cgroup-psi-pressure.sh` は監視対象 cgroup directory を明示必須にする薄い adapter として提供する。

## Why an explicit adapter exists

`cpu.pressure` / `memory.pressure` / `io.pressure` は cgroup hierarchy ごとに意味が異なる。host 側から root cgroup、現在 process の cgroup、代表 PID、container path を自動選択すると、意図した production workload と別の scope を正常扱いする危険がある。

この adapter は target を推測しない。引数が空なら `UNKNOWN reason=target_not_configured` とし、既存の `check-cgroup-psi-pressure.sh` へ進まない。

```bash
bash scripts/check-host-cgroup-psi-pressure.sh \
  /sys/fs/cgroup/<target>
```

## Semantics

実際の PSI parse と threshold 判定は既存 checker に委譲する。既定値は次のとおり。

- `some avg10`: 25% 以上で `WARNING`、50% 以上で `CRITICAL`
- `memory.full avg10` / `io.full avg10`: 5% 以上で `WARNING`、20% 以上で `CRITICAL`
- control file 欠落、不正 PSI record、不正 threshold は `UNKNOWN`

threshold を変える場合も既存の `IRLIGHT_CGROUP_PSI_*` 環境変数を利用し、この adapter 独自の threshold は持たない。

## Safety boundary

この check は read-only であり、cgroup control file の書込み、CPU/memory/I/O limit の変更、reclaim、process/container restart・kill、Node drain、provider operation を行わない。target path の discovery や fallback も行わない。

checker または shared PSI parser が配置されていない場合は `UNKNOWN reason=checker_unavailable` とし、依存関係欠落を `WARNING`/`OK` として扱わない。

## Tests

```bash
python -m unittest discover -s tests -p 'test_cgroup_psi_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_cgroup_psi_adapter.py' -v
bash -n scripts/check-cgroup-psi-pressure.sh
bash -n scripts/check-host-cgroup-psi-pressure.sh
bash -n scripts/lib/psi-pressure-common.sh
```

この adapter 自体は host aggregate の既定出力を変更しない。aggregate へ接続する場合は別変更として default-off opt-in にし、明示 target と既存 severity ordering を維持する。
