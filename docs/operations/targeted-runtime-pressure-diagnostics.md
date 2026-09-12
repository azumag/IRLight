# Targeted runtime pressure diagnostics

Issue #11 の host-level pressure 監視を補完するため、監視対象 workload を明示できる場合だけ使う read-only companion checks を提供する。

`check-host-pressure.sh` の `OK` は個別 cgroup / process の上限に余裕があることを意味しない。この手順では cgroup v2 の PSI stall と、単一 process の file descriptor soft limit への接近を別々に確認する。

## cgroup v2 PSI

対象 cgroup の `cpu.pressure` / `memory.pressure` / `io.pressure` を読み取る。

```bash
bash scripts/check-cgroup-psi-pressure.sh /sys/fs/cgroup/<target>
```

対象 directory は `IRLIGHT_CGROUP_PSI_DIR` でも指定できる。既定閾値は host PSI と同じく、`some avg10` が 25% / 50%、memory/io の `full avg10` が 5% / 20% で warning / critical。必要なら次で個別調整する。

- `IRLIGHT_CGROUP_PSI_SOME_WARNING_PERCENT`
- `IRLIGHT_CGROUP_PSI_SOME_CRITICAL_PERCENT`
- `IRLIGHT_CGROUP_PSI_FULL_WARNING_PERCENT`
- `IRLIGHT_CGROUP_PSI_FULL_CRITICAL_PERCENT`

出力例:

```text
IRLIGHT_CGROUP_PSI_PRESSURE status=WARNING cpu_some_avg10=27.50 memory_some_avg10=1.00 memory_full_avg10=0.00 io_some_avg10=1.00 io_full_avg10=0.00 some_warning_percent=25 some_critical_percent=50 full_warning_percent=5 full_critical_percent=20
```

CPU は `some`、memory/io は `some` と `full` を評価する。`avg10` だけで判定するが、同じ record の `avg60` / `avg300` も構文・範囲検証し、NaN / Infinity / 100%超過 / 重複 record / 読取不能は `UNKNOWN` に fail-closed する。

この check は指定した cgroup の stall time を観測するだけで、原因 process、host 全体の余裕、parent cgroup の制約、`memory.max` / `pids.max` の headroom を推測しない。対象 cgroup が不明確な環境では root/current cgroup を機械的に選ばず、監視対象を運用側で決める。

## Process file descriptor pressure

system-wide `file-nr` が正常でも、個別 process の `RLIMIT_NOFILE` soft limit に近づくと、その process だけが socket/file open に失敗し得る。対象 PID を明示し、その `/proc/<pid>/fd` と `/proc/<pid>/limits` を指定する。

```bash
PID=<target-pid>
bash scripts/check-process-fd-pressure.sh \
  "/proc/${PID}/fd" \
  "/proc/${PID}/limits"
```

path は `IRLIGHT_PROCESS_FD_DIR` / `IRLIGHT_PROCESS_LIMITS_PATH` でも指定できる。既定閾値は 80% / 90% で、`IRLIGHT_PROCESS_FD_WARNING_PERCENT` / `IRLIGHT_PROCESS_FD_CRITICAL_PERCENT` で変更できる。

有限 soft limit では `open fd count / soft limit` を評価する。soft limit `0` は新規 descriptor headroom がないため `CRITICAL` (`NO_HEADROOM`)。limit を実行中に引き下げた場合などに fd count が soft limit を超えていても telemetry corruption と推測せず `CRITICAL` (`OVER_LIMIT`) とする。soft limit が `unlimited` なら local percentage を発明せず `OK` (`usage_percent=NA`) とするが、system-wide file handle や cgroup / service manager 側の別制約まで安全という意味ではない。

`Max open files` record の欠落・重複・型不正・soft > hard、対象の読取不能、非数値 fd entry は `UNKNOWN` にする。対象 process が列挙中に消えた場合も正常扱いしない。

出力例:

```text
IRLIGHT_PROCESS_FD_PRESSURE status=WARNING usage_percent=82 open_fds=820 soft_limit=1000 hard_limit=4096 warning_percent=80 critical_percent=90
```

## Exit code

両 check とも同じ contract を使う。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | 対象 signal は warning 未満、または明示された local limit が unlimited |
| 1 | `WARNING` | warning 閾値以上 critical 未満 |
| 2 | `CRITICAL` | critical 閾値以上、または headroom がない / over-limit |
| 3 | `UNKNOWN` | 対象を安全に読み取り・評価できない |

## Safety

どちらも診断専用で、cgroup control file、rlimit、process、socket、authority stateを変更しない。kill/restart、fd close、`prlimit`、sysctl、cache drop、cleanupを自動実行しない。結果はまず alert / 診断へ接続し、復旧は対象 Session / process / state ownership を確認した runbook の明示操作として行う。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_psi_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_process_fd_pressure_check.py' -v
bash -n \
  scripts/check-cgroup-psi-pressure.sh \
  scripts/check-process-fd-pressure.sh
```
