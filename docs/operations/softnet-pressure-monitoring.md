# Linux softnet drop / time_squeeze delta monitoring

Linux の NIC link / route が正常でも、受信パケットの softnet 処理が追いつかないと host / network namespace 全体で packet drop や処理遅延が起こり得る。`scripts/check-softnet-pressure-delta.sh` は `/proc/net/softnet_stat` の累積 counter を operator-managed baseline と比較し、再起動や設定変更をせずにこの pressure signal を確認するための targeted read-only 診断である。

## 対象 signal

各 CPU 行の第2 field `dropped` と第3 field `time_squeeze` を比較・合計する。`dropped` の増加は softnet backlog 側で packet を処理できず捨てた活動を、`time_squeeze` の増加は softnet processing budget / time を使い切った活動を示す。どちらも host / network namespace 全体の signal であり、単独で特定 Session、destination、NIC、アプリケーションを原因と断定しない。

- 差分なし: `OK`
- `dropped` または `time_squeeze` が増加: `WARNING`
- current / baseline の欠落・読取不能、不正 record、per-CPU counter reset、CPU record 数の変化: `UNKNOWN`

`UNKNOWN` の generation-safety reason は、per-CPU counter の減少を `counter_reset`、CPU record 数の変化を `cpu_topology_changed` として固定する。これらは pressure が無かったことを意味せず、baseline と current を安全に比較できないことを示す。

この checker 単独では `CRITICAL` を返さない。重大度を上げる場合は NIC error/drop、CPU / PSI、route、ingest / egress、実際の Session 影響など別 signal と組み合わせて判断する。

## Baseline

baseline は checker が作成・更新・削除しない。operator が診断対象と同じ host、network namespace、boot generation、CPU topology で snapshot を取得して明示的に渡す。

```bash
install -d -m 0755 /run/irlight-monitor
cp /proc/net/softnet_stat /run/irlight-monitor/softnet_stat.baseline
bash scripts/check-softnet-pressure-delta.sh \
  /proc/net/softnet_stat \
  /run/irlight-monitor/softnet_stat.baseline
```

boot / network namespace / CPU topology / baseline generation が不明な場合は古い snapshot を推測で再利用しない。各 CPU 行の `processed` / `dropped` / `time_squeeze` のいずれかが baseline より小さくなった場合は counter reset / generation mismatch として `UNKNOWN`、CPU record 数が変化した場合も `UNKNOWN` に fail-closed する。これにより、一部 CPU の reset が別 CPU の増加で aggregate 上は隠れるケースを正常扱いしない。

環境変数でも current / baseline を指定できる。

```bash
IRLIGHT_SOFTNET_STAT_PATH=/proc/net/softnet_stat \
IRLIGHT_SOFTNET_STAT_BASELINE_PATH=/run/irlight-monitor/softnet_stat.baseline \
  bash scripts/check-softnet-pressure-delta.sh
```

## Safety boundary

checker は `/proc/net/softnet_stat` と baseline を read-only で読むだけで、sysctl、qdisc、route、interface、socket、service、process、provider resource を変更しない。baseline の自動更新もしない。出力には pathname、CPU identity、interface、destination、Session ID、credential を含めない。

`WARNING` が継続する場合は、既存の NIC error/drop delta、host CPU/load/PSI、cgroup pressure、route / resolver、ingest / egress status を read-only で併せて確認する。qdisc / RPS / RFS / backlog / IRQ affinity 等の tuning は deployment と workload に依存する重大な仕様判断なので、この checker が自動適用しない。

## 検証

```bash
python -m unittest discover -s tests -p 'test_softnet_pressure_delta_check.py' -v
bash -n scripts/check-softnet-pressure-delta.sh
```
