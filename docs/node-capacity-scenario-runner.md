# Node capacity scenario runner

Issue #13 の canonical load plan を、実際の負荷ハーネスへ安全に渡すための薄い orchestration layer です。

`scripts/run-node-capacity-scenario.py` 自体は CPU / memory / network の合否閾値、media mix、安全率を決めません。これらは承認済みの試験方針を持つ operator / harness 側の責務です。runner は canonical plan の順序を守って 1 scenario を実行し、既存の strict raw-trial recorder を通して測定値を保存します。

## 実行例

```bash
python3 scripts/run-node-capacity-scenario.py \
  --plan artifacts/node-capacity-plan.json \
  --scenario normal-input \
  --trials-jsonl artifacts/normal-input.trials.jsonl \
  --timeout-seconds 900 \
  --runner /opt/irlight/bin/node-capacity-harness \
  --json
```

固定引数が必要な harness は `--runner-arg` を繰り返して渡します。shell は経由せず argv として直接実行されます。

runner は `--trials-jsonl` が既に存在する場合は拒否します。既存の証跡を上書きしません。途中で harness が失敗した場合、それ以前に正常記録できた raw trial は残ります。再試行では新しい evidence path を使い、途中結果を成功済み run として扱わないでください。

## Harness contract

指定した harness command の末尾へ、runner が次の 2 引数を追加します。

```text
<REQUEST_JSON> <RESULT_JSON>
```

`REQUEST_JSON` は mode `0600` の一時ファイルで、次の schema-v1 object です。

```json
{
  "schema_version": 1,
  "profile_label": "720p30 3Mbps",
  "scenario_id": "normal-input",
  "concurrent_sessions": 4
}
```

harness は計測を完了後、指定された `RESULT_JSON` を新規作成し、次の **exact fields** を JSON object として書き込みます。

```json
{
  "duration_seconds": 600.0,
  "outcome": "pass",
  "cpu_peak_percent": 72.5,
  "memory_rss_peak_bytes": 2147483648,
  "egress_peak_bps": 11800000.0,
  "failed_sessions": 0,
  "unexpected_reconnects": 0
}
```

`outcome` は harness/operator が承認済み threshold に基づいて `pass` / `fail` を分類します。runner は測定値から独自に合否を推測しません。

result は 64 KiB 以下の UTF-8 JSON regular file である必要があります。duplicate key、NaN / Infinity、extra/missing field、symlink result、既存 trial schema に反する型や値は fail-closed です。stdout / stderr は evidence transport に使わず破棄します。

## 負荷レベルの進め方

runner は plan にある concurrency level を昇順に実行します。

- `pass` の間だけ次の planned level へ進む。
- 最初の `fail` を記録した時点で、それより高い level は実行しない。
- plan の全 level が `pass` した場合、plan 外の負荷を勝手に追加しない。`boundary_found=false` として終了し、必要なら operator が新しい canonical plan に追加 level を明示する。

このため、失敗境界を得るために根拠なくさらに高負荷を掛けることはありません。

## 次の evidence chain

raw trial を取得した後は、既存 tooling を使います。

```text
run-node-capacity-scenario.py
  -> *.trials.jsonl
  -> assemble-node-capacity-report.py
  -> measured scenario report
  -> render-node-capacity-coverage-manifest.py
  -> render-node-capacity-max-sessions-proposal.py
  -> write-node-capacity-review-bundle.py
```

report assembly では `run_id`、Node profile、software revision、scenario、operator が選んだ safety margin を明示します。review bundle まで進めることで proposal だけでなく coverage manifest、load plan、全 measured reports の exact bytes を digest で固定できます。

## Safety boundary

この runner は operator が明示したローカル command だけを起動します。provider API、課金リソース作成、production scheduler / `max_sessions` 更新、外部配信 credential の取得は行いません。それらが必要な harness は別途明示的な運用承認・資格情報・安全手順の範囲で実行してください。
