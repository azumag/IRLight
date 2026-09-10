# Billing webhook backlog alert dry-run

`BILLING_WEBHOOK_DELAYED` は、billing webhook の未処理 backlog の最古 age が上昇している可能性を検知する Warning alert です。Issue #11 の「webhook backlog」を、通知や決済provider、Payment / Entitlement state を変更せず read-only で評価するための dry-run を提供します。

## 対象契約

alert catalog の次の契約と一致する場合だけ評価します。

- alert ID: `BILLING_WEBHOOK_DELAYED`
- severity: `warning`
- signal: `billing.webhook_backlog_age_seconds`
- trigger: `threshold` / `operations.billing_webhook_delayed`
- runbook: `docs/operations/billing-webhook-stalled.md`

catalog がこの契約からずれている場合は `INVALID_CATALOG` とし、推測で評価を継続しません。

## 入力

collector 等で事前に集計済みの oldest webhook backlog age を JSONL で標準入力へ渡します。1 record は次の exact field だけを受理します。

```json
{"webhook_backlog_age_seconds": 600}
```

payment/customer/subscription/event ID、user/session/node ID、provider response、payload、signature、credential 等の追加 field は受理しません。入力 record は 4 KiB を上限とし、invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、負値、bool、文字列、null、不正 JSON は fail-closed に扱います。batch 内に不正 record が1件でもある場合、先に一致した record があっても部分結果を alert として返しません。

## Threshold

production の threshold と aggregation policy は repository では決めません。通常時の webhook 遅延、provider retry 間隔、consumer throughput、backlog の揺らぎ、利用者影響を実測した上で deployment / operator が `--threshold` を秒単位で明示します。

```bash
printf '%s\n' '{"webhook_backlog_age_seconds":600}' | \
  python apps/control-api/operations_billing_webhook_alerts.py \
    --repo-root . \
    --threshold 300
```

判定は `webhook_backlog_age_seconds > threshold` です。境界値と同値は一致させません。検証用に threshold `0` を指定しても、backlog age `0` の正常 observation を Warning と誤分類しません。

## 出力

出力は固定 alert ID と aggregate 件数に限定します。

```json
{"matched_alerts":{"BILLING_WEBHOOK_DELAYED":1},"records":1,"status":"MATCHED","unmatched_records":0,"violations":{}}
```

入力 age、payment/customer/subscription/event ID、provider 情報、payload、signature、credential は echo しません。

## 安全境界

この dry-run は collector、webhook receiver、consumer、retry worker を実装せず、billing provider API を呼びません。webhook の再送、Payment / Subscription / Entitlement の更新、返金、capture、manual grant、queue 削除、外部通知も行いません。

Warning を検出した場合も、backlog を捨てたり、署名検証を無効化したり、provider 上の決済結果を推測して Entitlement を発行したりせず、`billing-webhook-stalled.md` の read-only 切り分けから開始してください。production threshold を決めるには、通常時の oldest backlog age、consumer throughput、provider retry、利用者影響、誤検知許容度の実測が必要です。

## 検証

回帰テストでは次を固定します。

- 明示 threshold の strict boundary
- zero-threshold の false-positive 防止
- catalog の ID / severity / signal / trigger / runbook drift の拒否
- invalid UTF-8 / duplicate JSON key / non-finite number / oversized record の fail-closed
- 負値・bool・文字列・null・追加識別子 field の拒否
- invalid batch で先行 MATCHED 結果を抑制すること
- 出力に入力識別子や secret を再表示しないこと

この dry-run の成功は billing webhook 処理経路自体の正常性や provider との整合性を保証しません。実際の復旧判断は backlog の継続低下、event ID による冪等処理、provider と内部 state の照合を別途確認します。
