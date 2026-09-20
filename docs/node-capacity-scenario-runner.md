# Node capacity scenario runner

Issue #13 の canonical load plan を、実際の負荷ハーネスへ安全に渡すための薄い orchestration layer です。

`scripts/run-node-capacity-scenario.py` 自体は CPU / memory / network の合否閾値、media mix、安全率を決めません。これらは承認済みの試験方針を持つ operator / harness 側の責務です。runner は canonical plan の順序を守って 1 scenario を実行し、既存の strict raw-trial recorder を通して測定値を保存します。

## 実行例

安全側で最初の失敗後に止める探索 run:

```bash
python3 scripts/run-node-capacity-scenario.py \
  --plan artifacts/node-capacity-plan.json \
  --scenario normal-input \
  --trials-jsonl artifacts/normal-input.trials.jsonl \
  --run-manifest artifacts/normal-input.run.json \
  --node-profile 'example-node-shape' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --timeout-seconds 900 \
  --failure-policy stop \
  --runner /opt/irlight/bin/node-capacity-harness \
  --json
```

Issue #13 の complete coverage evidence を採るため、承認済み plan の全 level を明示的に実行する run:

```bash
python3 scripts/run-node-capacity-scenario.py \
  --plan artifacts/node-capacity-plan.json \
  --scenario normal-input \
  --trials-jsonl artifacts/normal-input.complete.trials.jsonl \
  --run-manifest artifacts/normal-input.complete.run.json \
  --node-profile 'example-node-shape' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --timeout-seconds 900 \
  --failure-policy continue \
  --runner /opt/irlight/bin/node-capacity-harness \
  --json
```

固定引数が必要な harness は `--runner-arg` を繰り返して渡します。shell は経由せず argv として直接実行されます。

runner は `--trials-jsonl` または `--run-manifest` が既に存在する場合は拒否します。既存の証跡を上書きしません。`--run-manifest` を指定する場合、測定対象の `--node-profile` と lowercase 40-character `--software-revision` も実行前に必須です。これらを report 生成時に後付けせず、負荷をかける前に run provenance として固定します。

run manifest は harness run が正常に runner へ戻った場合だけ publish され、canonical plan と normalized raw trials の SHA-256、media profile、scenario、Node profile、software revision、planned/tested level、failure policy、completion state を保持します。途中で harness が失敗した場合、それ以前に正常記録できた raw trial は残ることがありますが run manifest は publish されません。再試行では新しい evidence path を使い、途中結果を成功済み run として扱わないでください。

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

## 負荷レベルと failure policy

runner は plan にある concurrency level を昇順にだけ実行し、plan 外の負荷 level を勝手に追加しません。`--failure-policy` は必須で、失敗境界より上の planned load を自動実行するかどうかを operator が明示します。

- `stop`: 最初の `fail` を記録した時点で終了する。安全側の探索用で、`completed_plan=false` になります。
- `continue`: 最初の `fail` 後も **canonical plan に既に明示された level だけ**を続ける。Issue #13 の coverage validator は各 scenario で planned level 全件を要求するため、complete coverage evidence を作る場合はこちらを明示します。
- 全 level が `pass` した場合はどちらの policy でも `boundary_found=false` で終了する。runner は未承認のさらに高い load を生成しません。失敗境界が必要なら、operator が追加 level を含む新しい canonical plan を作ります。

`continue` は障害後の追加負荷を意味するため、Node shape、harness、監視、停止手順がその実行を許容すると確認した場合だけ使用してください。どちらの policy でも、既存 raw-trial validator が fail 後の pass、failed session を含む pass、型不正などを拒否します。

## 次の evidence chain

raw trial を取得した後は、complete coverage 用 report では canonical plan と run manifest を再検証する plan-bound assembler を使います。これにより、`stop` policy や harness failure で残った partial JSONL を complete scenario report として扱わず、同じ load ladder を持つ別 media profile の plan への raw trials 付け替え、または測定後の Node profile/software revision の付け替えも防ぎます。詳細は [`node-capacity-planned-report.md`](node-capacity-planned-report.md) を参照してください。

```text
run-node-capacity-scenario.py
  -> *.trials.jsonl + *.run.json
  -> assemble-node-capacity-planned-report.py
  -> measured scenario report
  -> render-node-capacity-coverage-manifest.py
  -> render-node-capacity-max-sessions-proposal.py
  -> write-node-capacity-review-bundle.py
```

report assembly では `run_id` と operator が選んだ safety margin を明示します。media profile/scenario と Node profile/software revision は run manifest と検証済み plan から自動生成します。coverage manifest へ進めるには各 scenario の planned load level がすべて測定済みである必要があります。review bundle まで進めることで proposal だけでなく coverage manifest、load plan、全 measured reports の exact bytes を digest で固定できます。

## Safety boundary

この runner は operator が明示したローカル command だけを起動します。provider API、課金リソース作成、production scheduler / `max_sessions` 更新、外部配信 credential の取得は行いません。それらが必要な harness は別途明示的な運用承認・資格情報・安全手順の範囲で実行してください。
