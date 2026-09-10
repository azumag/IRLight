# Session failure-surge alert dry-run

Issue #11 の Critical `SESSION_FAILURE_SURGE` を、本番の threshold や aggregation window を repository 側で推測せずに検証するための read-only dry-run です。入力は collector 側で既に集計した `sessions.failed_rate` 相当の値だけに限定し、Session authority、process、provider、通知先を変更しません。

## 実行

repository root から、検証したい deployment / operator threshold を明示して実行します。

```bash
printf '%s\n' '{"failed_rate":0.25}' \
  | python apps/control-api/operations_session_failure_alerts.py --threshold 0.20
```

`--threshold` は必須です。repository に production default はありません。入力の rate を何秒・何分の window で集計するか、分母を「開始済み Session」「進行中 Session」「終了した Session」などのどれにするか、どの値を本番 paging threshold にするかは実測と運用負荷を確認して deployment / collector 側で決めます。この dry-run は単位・window・分母を暗黙に固定しません。

## 入力契約

1行1 JSON object の JSONL とし、各 record は `failed_rate` だけを持ちます。値は finite かつ 0 以上の JSON number でなければなりません。`session_id`、`node_id`、`reason_code`、user data、message、URL、credential などの追加 field は拒否します。

1 record は 4 KiB までです。invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、不正 JSON、負値、bool/string/null、巨大値による finite number 境界逸脱は fail-closed です。batch に invalid record が1件でもあれば、それ以前の一致結果を含めて抑制し `INVALID` を返します。

## 判定

catalog の次の契約が一致している場合だけ判定します。

- ID: `SESSION_FAILURE_SURGE`
- severity: `critical`
- signal: `sessions.failed_rate`
- trigger: `threshold_ref=operations.session_failure_surge`
- runbook: `session-process-crash-loop.md`

入力 rate が明示した `--threshold` を**超えた場合**だけ match とします。threshold と同値は match しません。threshold `0` を検証用に指定した場合でも failed rate `0` の正常観測を障害として扱いません。出力は repository 管理下の alert ID と aggregate 件数だけで、入力値や識別子を echo しません。catalog が drift していれば `INVALID_CATALOG`、threshold が非 finite / 負値なら `INVALID_THRESHOLD` として alert 判定を行いません。

この evaluator は `failed_rate` の意味を推測しないため、collector 側では分母が 0 の window を 0 とみなすのか、観測不能として batch を作らないのかを明示的に定義してください。欠損や集計失敗を「障害なし」と補完してこの dry-run へ渡さないでください。

## 安全境界

この command は次を行いません。

- Session authority の read / write、Session stop / cleanup
- media process の restart / kill
- Node provisioning / provider API 呼び出し
- billing 操作
- Discord / email / paging などの通知
- aggregation window、分母、production threshold の自動決定

実際の障害対応は [session-process-crash-loop.md](session-process-crash-loop.md) を入口にし、個別 Session の停止や Node 操作へ進む前に、同じ reason / deploy / region に失敗が集中しているかを read-only で切り分けます。大量 FAILED の検出だけを根拠に自動 restart、全 Session stop、provider 増強、課金変更を行いません。
