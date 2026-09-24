# cgroup v2 PID pressure monitoring

Issue #11 の resource-pressure 診断として、明示した cgroup v2 の `pids.current` と `pids.max` を read-only で評価する。

system-wide の `threads-max` だけでは、特定 service / container に設定された cgroup PID 上限の枯渇を検知できない。逆に cgroup の値だけから host 全体の task 枯渇を推測もしないため、用途を分けて扱う。

## Standalone check

監視対象 cgroup の control file を明示して実行する。

```bash
bash scripts/check-cgroup-pid-pressure.sh \
  /sys/fs/cgroup/<target>/pids.current \
  /sys/fs/cgroup/<target>/pids.max
```

環境変数でも指定できる。

```bash
IRLIGHT_CGROUP_PIDS_CURRENT_PATH=/sys/fs/cgroup/<target>/pids.current \
IRLIGHT_CGROUP_PIDS_MAX_PATH=/sys/fs/cgroup/<target>/pids.max \
  bash scripts/check-cgroup-pid-pressure.sh
```

既定閾値は 80% で `WARNING`、90% で `CRITICAL`。必要な場合だけ `IRLIGHT_CGROUP_PIDS_WARNING_PERCENT` / `IRLIGHT_CGROUP_PIDS_CRITICAL_PERCENT` で変更する。

## Host aggregate opt-in

既定の `scripts/check-host-pressure.sh` 出力は変更しない。監視すべき cgroup を deployment が明示できる場合だけ、次の opt-in で `cgroup_pids_status` を host aggregate に追加する。

```bash
IRLIGHT_HOST_CGROUP_PIDS_MODE=enabled \
IRLIGHT_CGROUP_PIDS_CURRENT_PATH=/sys/fs/cgroup/<target>/pids.current \
IRLIGHT_CGROUP_PIDS_MAX_PATH=/sys/fs/cgroup/<target>/pids.max \
  bash scripts/check-host-pressure.sh
```

aggregate では **両方の path が必須**。片方でも未指定なら root cgroup や別 cgroup を推測せず、`cgroup_pids_status=UNKNOWN` として fail-closed にする。`IRLIGHT_HOST_CGROUP_PIDS_MODE` は `enabled` / `disabled` のみを受け付け、未知値は aggregate 自体を `UNKNOWN` にする。

有効化した component は他の host pressure component と同じ timeout 境界、および `CRITICAL > UNKNOWN > WARNING > OK` の集約規則へ参加する。

## Semantics

`pids.max` が有限値なら `pids.current / pids.max` を評価する。

- warning 閾値未満: `OK`
- warning 以上・critical 未満: `WARNING`
- critical 以上: `CRITICAL`
- `pids.max=0`: 新しい task を作れないため `CRITICAL` (`NO_HEADROOM`)
- `pids.current > pids.max`: cgroup の organisational operation では起こり得るが、新規 task の余裕がないため `CRITICAL` (`OVER_LIMIT`)
- `pids.max=max`: この cgroup 自身に有限 local limit がないため `OK` (`usage_percent=NA`)
- 欠落、読取不能、複数行、非数値、signed 64-bit 範囲外: `UNKNOWN`

PID limit は階層的である。child の `pids.max=max` でも parent cgroup の有限上限に制約される場合がある。この check は指定した2ファイルの local 状態だけを評価し、parent の effective limit、system-wide `threads-max`、PID namespace 枯渇を推測しない。必要な parent も監視する場合は、その cgroup を別の明示対象として扱う。

## Safety

この診断は cgroup control file を読み取るだけで、値の変更、process kill / restart、service restart、Node drain、container recreate、provider migration、instance resize を行わない。

`WARNING` / `CRITICAL` を検知しても、原因 process と Session ownership を確認せず自動で task を終了しない。`UNKNOWN` を正常扱いしたり、path を自動探索して別 cgroup を代替対象にしたりしない。

## Diagnosis

異常時は対象 cgroup が意図した service / container であることを先に確認し、その後 `pids.current` / `pids.max` と process 数・thread 数を read-only で照合する。container / service の再作成や limit 変更は別の明示的な運用判断として扱う。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_pid_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_host_cgroup_pid_aggregate.py' -v
python -m unittest discover -s tests -p 'test_host_pressure_opt_in_matrix.py' -v
bash -n \
  scripts/check-cgroup-pid-pressure.sh \
  scripts/check-host-cgroup-pid-pressure.sh \
  scripts/check-host-pressure.sh
```
