# Event alert dry-run

Issue #11 の「主要障害を自動検知できる」状態へ段階的に進めるため、`apps/control-api/operations_event_alerts.py` は structured JSONL と `config/operations-alert-catalog.json` の **event trigger** を read-only で照合します。

このコマンドの目的は、外部監視製品・通知先・credential を導入する前に、producer が出した安定 `event_type` からどの alert ID が発火候補になるかを機械的に検証できるようにすることです。通知、paging、Session 停止、provider cleanup、failover、refund、threshold 判定は実行しません。

## 実行

repository root で structured log の JSONL を stdin から渡します。

```bash
python apps/control-api/operations_event_alerts.py < captured.jsonl
```

正常な入力は Issue #11 の baseline structured-log fields と同じ `timestamp`、`level`、`service`、`event_type` を非空文字列で持つ必要があります。`event_type` は catalog の event trigger と**完全一致**で照合します。大文字小文字変換や曖昧な reason 推測はしません。

結果は source record を再表示せず、次だけを返します。

```json
{
  "matched_alerts": {"CONTROL_PLANE_UNAVAILABLE": 1},
  "records": 1,
  "status": "MATCHED",
  "unmatched_records": 0,
  "violations": {}
}
```

`matched_alerts` の key は repository 管理下の alert ID だけです。入力に含まれる `session_id`、`node_id`、request ID、message、URL、credential、ユーザー入力値などは出力しません。`NO_MATCHES` と `MATCHED` は exit status `0`、入力 batch が壊れている場合は `INVALID` / exit status `3`、catalog 自体を安全に解釈できない場合は `INVALID_CATALOG` / exit status `4` です。

## Fail-closed 境界

1 record は UTF-8 で 256 KiB、nested object / array は 64 level を上限にします。通常 CLI では raw stdin に byte 上限を先に適用し、oversized line は全量保持せず newline まで bounded chunk で drain します。invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、必須 field 欠落・型違い、過剰 nesting は batch 全体を `INVALID` にします。

batch 内に1件でも invalid record がある場合、それ以前に event alert が一致していても `matched_alerts` と `unmatched_records` を空・0へ抑制します。不完全な入力を「検知済みの完全な結果」と誤認して通知 routing へ渡さないためです。

同じ `event_type` を複数 alert ID へ割り当てる catalog も曖昧な routing として拒否します。1 event を複数通知へ展開したい場合は、暗黙の重複ではなく catalog schema / routing policy として明示的に設計します。

## Secret handling

この evaluator は **sanitizer ではありません**。入力値を出力しないことで二次漏えいを避けますが、source log が secret-free であることを証明しません。incident attachment や長期保存の前には `docs/operations/log-redaction-audit.md` の guardrail を別途使い、producer 側で allow-list logging / redaction を行います。

raw captured JSONL を GitHub Issue、PR、chat へ貼り付けないでください。`matched_alerts` の aggregate result だけで event routing の動作確認ができます。

## Threshold alert は対象外

`SESSION_CAPACITY_HIGH`、`NODE_HEARTBEAT_DELAYED`、`RECONNECT_RATE_HIGH` など `trigger.mode = threshold` の alert はこのコマンドでは評価しません。`operations.*` threshold は β 実測や deployment monitoring の ownership が必要であり、この dry-run が数値を推測して固定することはありません。

threshold collector / evaluator と Discord・email 等の notification routing は別の実装単位です。外部サービス、秘密情報、課金、on-call policy を伴うため、導入時は明示的な設定とレビューを行います。

## CI で固定すること

`tests/test_operations_event_alerts.py` は以下を回帰条件にします。

- real catalog の event trigger のみが一致し、threshold alert は event 名だけでは発火しない。
- source の secret・message・correlation identifier を結果へ返さない。
- invalid batch の部分一致結果を公開しない。
- duplicate key、非有限数、invalid UTF-8、oversized record、過剰 nesting を fail-closed にする。
- 同一 event type の曖昧な複数 alert mapping を拒否する。

これにより、notification routing を導入する前でも event detector の契約を repository CI で検証できます。
