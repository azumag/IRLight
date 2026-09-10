# Asset processing failure-rate alert dry-run

`ASSET_FAILURE_RATE_HIGH` は、待機素材の upload / processing / variant 生成などで失敗率が上昇している可能性を検知する Warning alert です。Issue #11 の「asset processing失敗増加」を、通知やobject storage、worker設定を変更せず read-only で評価するための dry-run を提供します。

## 対象契約

alert catalog の次の契約と一致する場合だけ評価します。

- alert ID: `ASSET_FAILURE_RATE_HIGH`
- severity: `warning`
- signal: `assets.processing_failure_rate`
- trigger: `threshold` / `operations.asset_failure_rate_high`
- runbook: `docs/operations/object-storage-unavailable.md`

catalog がこの契約からずれている場合は `INVALID_CATALOG` とし、推測で評価を継続しません。

## 入力

collector 等で事前に集計済みの processing failure rate を JSONL で標準入力へ渡します。1 record は次の exact field だけを受理します。

```json
{"processing_failure_rate": 0.2}
```

`asset_id`、user/session/node ID、object key、reason code、provider情報、credential 等の追加 field は受理しません。入力 record は 4 KiB を上限とし、invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、負値、bool、文字列、null、不正 JSON は fail-closed に扱います。batch 内に不正 record が1件でもある場合、先に一致した record があっても部分結果を alert として返しません。

## Threshold

production の threshold、aggregation window、rate unit / denominator は repository では決めません。asset数、処理種別、通常時の失敗率、retry方針、object storageの一時障害率を実測した上で deployment / operator が `--threshold` を明示します。

```bash
printf '%s\n' '{"processing_failure_rate":0.2}' | \
  python apps/control-api/operations_asset_failure_alerts.py \
    --repo-root . \
    --threshold 0.1
```

判定は `processing_failure_rate > threshold` です。境界値と同値は一致させません。検証用に threshold `0` を指定しても、failure rate `0` の正常 observation を Warning と誤分類しません。

## 出力

出力は固定 alert ID と aggregate 件数に限定します。

```json
{"matched_alerts":{"ASSET_FAILURE_RATE_HIGH":1},"records":1,"status":"MATCHED","unmatched_records":0,"violations":{}}
```

入力 rate、asset/user/session/node の識別子、object key、provider 情報、credential は echo しません。

## 安全境界

この dry-run は collector や retry worker を実装せず、object storageへの HEAD/GET/PUT、asset再処理、cache purge、variant再生成を行いません。Session authority、Media Node、provider、billing、外部通知も変更しません。

Warning を検出した場合も、失敗assetを存在しないものとして削除したり、既存cacheをpurgeしたり、無制限retryを開始したりせず、`object-storage-unavailable.md` の read-only 切り分けから開始してください。production threshold や通知 routing を決めるには、通常時の処理量、処理種別別の失敗率、aggregation window、denominator、誤検知許容度の実測が必要です。

## 検証

回帰テストでは次を固定します。

- 明示 threshold の strict boundary
- zero-threshold の false-positive 防止
- catalog の ID / severity / signal / trigger / runbook drift の拒否
- 負値・bool・文字列・null・非有限数の拒否
- 追加 field と識別子 field の拒否
- invalid batch の部分一致抑制
- duplicate JSON key / invalid UTF-8 / oversized record の fail-closed
- valid no-match を正常結果として扱うこと
