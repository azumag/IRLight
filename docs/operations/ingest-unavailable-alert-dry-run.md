# Ingest unavailable alert dry-run

`INGEST_UNAVAILABLE` は、RTMP / RTMPS / SRT 等の ingest 接続が広範囲に失敗している可能性を検知する Critical alert です。Issue #11 の「ingest 接続不能」を、通知や本番設定、ingest listener を変更せず read-only で評価するための dry-run を提供します。

## 対象契約

alert catalog の次の契約と一致する場合だけ評価します。

- alert ID: `INGEST_UNAVAILABLE`
- severity: `critical`
- signal: `ingest.connectivity_failure_rate`
- trigger: `threshold` / `operations.ingest_unavailable`
- runbook: `docs/operations/ingest-connectivity-failure.md`

catalog がこの契約からずれている場合は `INVALID_CATALOG` とし、推測で評価を継続しません。

## 入力

collector 等で事前に集計済みの connectivity failure rate を JSONL で標準入力へ渡します。1 record は次の exact field だけを受理します。

```json
{"connectivity_failure_rate": 0.2}
```

`session_id`、`node_id`、region、reason code、credential 等の追加 field は受理しません。入力 record は 4 KiB を上限とし、invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、負値、bool、文字列、null、不正 JSON は fail-closed に扱います。batch 内に不正 record が1件でもある場合、先に一致した record があっても部分結果を alert として返しません。

## Threshold

production の threshold、aggregation window、rate unit / denominator は repository では決めません。正常時の接続試行量、protocol / region ごとの分布、βでの実測と運用判断に基づく値を deployment / operator が `--threshold` で明示します。

```bash
printf '%s\n' '{"connectivity_failure_rate":0.2}' | \
  python apps/control-api/operations_ingest_unavailable_alerts.py \
    --repo-root . \
    --threshold 0.1
```

判定は `connectivity_failure_rate > threshold` です。境界値と同値は一致させません。これにより検証用に threshold `0` を指定しても、failure rate `0` の正常 observation を Critical と誤分類しません。

## 出力

出力は固定 alert ID と aggregate 件数に限定します。

```json
{"matched_alerts":{"INGEST_UNAVAILABLE":1},"records":1,"status":"MATCHED","unmatched_records":0,"violations":{}}
```

入力 rate、Session / Node / region の識別子、provider 情報、credential は echo しません。

## 安全境界

この dry-run は collector を実装せず、外部 publisher や ingest endpoint への能動 probe も行いません。Session authority、MediaMTX / SRT listener、media process、provider、billing、外部通知を変更しません。

Critical を検出した場合も、推測で listener 再起動、Session 停止、credential 再発行、DNS / firewall 変更を行わず、`ingest-connectivity-failure.md` の read-only 切り分けから開始してください。production threshold や通知 routing を決めるには、正常時の protocol / region 別の失敗率、aggregation window、試行数 denominator、誤検知許容度の実測が必要です。

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
