# Egress reconnect rate high alert dry-run

`RECONNECT_RATE_HIGH` は、外部配信先への reconnect が平常より増えている可能性を検知する Warning alert です。Issue #11 の「reconnect率上昇」を、通知や本番設定を変更せず read-only で評価するための dry-run を提供します。

## 対象契約

alert catalog の次の契約と一致する場合だけ評価します。

- alert ID: `RECONNECT_RATE_HIGH`
- severity: `warning`
- signal: `egress.reconnect_rate`
- trigger: `threshold` / `operations.egress_reconnect_rate_high`
- runbook: `docs/operations/egress-widespread-failure.md`

catalog がこの契約からずれている場合は `INVALID_CATALOG` とし、推測で評価を継続しません。

## 入力

collector 等で事前に集計済みの reconnect rate を JSONL で標準入力へ渡します。1 record は次の exact field だけを受理します。

```json
{"reconnect_rate": 2.5}
```

`session_id`、`node_id`、destination、reason code、credential 等の追加 field は受理しません。入力 record は 4 KiB を上限とし、invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、負値、bool、文字列、null、不正 JSON は fail-closed に扱います。batch 内に不正 record が1件でもある場合、先に一致した record があっても部分結果を alert として返しません。

## Threshold

production の threshold、aggregation window、rate unit / denominator は repository では決めません。実測と運用判断に基づく値を deployment / operator が `--threshold` で明示します。

```bash
printf '%s\n' '{"reconnect_rate":2.5}' | \
  python apps/control-api/operations_egress_reconnect_alerts.py \
    --repo-root . \
    --threshold 2.0
```

判定は `reconnect_rate > threshold` です。境界値と同値は一致させません。これにより検証用に threshold `0` を指定しても、reconnect rate `0` の正常 observation を Warning と誤分類しません。

## 出力

出力は固定 alert ID と aggregate 件数に限定します。

```json
{"matched_alerts":{"RECONNECT_RATE_HIGH":1},"records":1,"status":"MATCHED","unmatched_records":0,"violations":{}}
```

入力 rate、Session / Node / destination の識別子、provider 情報、credential は echo しません。

## 安全境界

この dry-run は collector を実装せず、Session authority、media process、egress destination、provider、billing、外部通知を変更しません。外部配信先へ probe や再接続を発生させるものでもありません。

rate 上昇を確認した場合も、即座に Session 停止や destination 変更を行わず、`egress-widespread-failure.md` の read-only 切り分けから開始してください。production threshold や通知 routing を決めるには、正常時の reconnect 分布、配信先別特性、aggregation window、誤検知許容度の実測が必要です。

## 検証

回帰テストでは次を固定します。

- 明示 threshold の strict boundary
- zero-threshold の false-positive 防止
- catalog の ID / severity / signal / trigger / runbook drift の拒否
- 負値・bool・文字列・null・非有限数の拒否
- 追加 field と識別子の拒否
- invalid batch で部分一致を抑制
- duplicate key / invalid UTF-8 / oversized record の fail-closed
- valid no-match が成功結果になること
