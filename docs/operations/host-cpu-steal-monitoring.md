# Host CPU steal delta monitoring

Issue #11 の host resource monitoring を補完するため、Linux の `/proc/stat` にある aggregate CPU の `steal` counter を operator-managed baseline と比較する targeted read-only diagnostic を定義する。

仮想化された Media Node では、process CPU や load average が単独では説明しにくい frame drop や egress instability の相関要因として、hypervisor 側の CPU scheduling contention が現れることがある。`steal` は virtual CPU が runnable だったにもかかわらず hypervisor が別 workload を実行していた時間の累積 counter であり、この check はその増加を検出する。

## Check

```bash
IRLIGHT_PROC_STAT_BASELINE_PATH=/var/lib/irlight-monitor/proc-stat.baseline \
  bash scripts/check-cpu-steal-delta.sh
```

第1引数または `IRLIGHT_PROC_STAT_PATH` で current snapshot を差し替えられる。既定は `/proc/stat`。第2引数または `IRLIGHT_PROC_STAT_BASELINE_PATH` で baseline を指定する。

出力例:

```text
IRLIGHT_CPU_STEAL status=OK reason=none steal_ticks_delta=0
IRLIGHT_CPU_STEAL status=WARNING reason=cpu_steal_activity steal_ticks_delta=7
IRLIGHT_CPU_STEAL status=UNKNOWN reason=counter_reset
```

exit code は他の resource-pressure checks と同じく `0=OK`, `1=WARNING`, `3=UNKNOWN` とする。この check は `steal` の増加を outage や provider fault と断定せず、配信品質劣化を切り分けるための correlation signal として扱う。

## 判定

- aggregate `cpu` record の `steal` が baseline から増えていなければ `OK`。
- 1 tick でも増えていれば `WARNING / cpu_steal_activity`。この閾値は targeted sampling の「活動あり」を表すためのもので、継続時間や provider 切替を自動判断する閾値ではない。
- current / baseline が読めない、aggregate record がない、対象counterが不正、同じ aggregate record が重複している場合は `UNKNOWN`。
- `steal` 自体が baseline より減少した場合は、reboot・counter reset・別hostのbaseline等を安全に区別できないため `UNKNOWN / counter_reset` とする。他の CPU counter は record の形式検証には使うが generation 判定には使わない。Linux では `iowait` が条件によって減少し得るため、全 CPU counter の単調増加を要求すると正常な host を誤って `UNKNOWN` にできてしまうためである。
- `cpu0` など per-CPU record はこの check の判定には使用しない。host全体の aggregate counterだけを比較する。

## Baseline lifecycle

baseline は checker が作成・更新しない。監視側が同じ host / boot generation の snapshot を明示的に管理する。

初回例:

```bash
install -d -m 0750 /var/lib/irlight-monitor
cp -- /proc/stat /var/lib/irlight-monitor/proc-stat.baseline
```

判定結果を監視系へ保存した後、次の観測区間へ進む際に監視側が baseline を置き換える。baseline の採取間隔は deployment の監視周期に合わせ、checker 内へ固定しない。

reboot / reprovision をまたぐ累積counter比較は無効なので、`scripts/check-host-boot-generation.sh` と組み合わせる。`boot_generation_changed` または `counter_reset` が出た場合は、原因を記録してから新しい boot generation の baseline を明示的に採取する。古いbaselineを自動的に正常化しない。

## 運用境界

この診断は read-only であり、CPU affinity、scheduler、sysctl、process priority、service restart、instance resize、provider migration、failover 等を実行しない。CPU steal が継続する場合も、Media Node の profile、frame drop、A/V sync、egress stability、同時Session数と合わせて影響を確認する。

既定の `check-host-pressure.sh` aggregate へ自動追加しない。bare metal、専有CPU、共有VPSでは `steal` の意味と許容値が異なり、baseline の更新周期も deployment policy に依存するためである。provider変更やinstance resizeは課金・配信影響を伴うので、この check の結果だけで自動実行しない。

## 検証

```bash
python -m unittest discover -s tests -p 'test_cpu_steal_delta_check.py' -v
python -m unittest discover -s tests -p 'test_host_cpu_steal_runbook_inventory.py' -v
bash -n scripts/check-cpu-steal-delta.sh
```
