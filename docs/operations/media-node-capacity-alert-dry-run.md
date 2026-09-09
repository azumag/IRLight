# Media Node capacity alert dry-run

Issue #11 の `Node capacity 80%超` warning を、collector / notification routing を接続する前に read-only で検証するための補助コマンドです。

`apps/control-api/operations_capacity_alerts.py` は、**検証済み `max_sessions` と active / reserved Session 件数だけ**を JSONL で受け取り、alert catalog の `MEDIA_NODE_CAPACITY_HIGH` 契約と照合します。provider API、Node 作成、Session prepare / stop、利用権、課金、外部通知には触れません。

## 入力

1 record は次の3項目だけを持つ必要があります。

```json
{"max_sessions":10,"active_sessions":8,"reserved_sessions":1}
```

- `max_sessions`: #8 / #13 の scheduler / load test 等で検証済みの正の整数
- `active_sessions`: 同じ Node / 同じ観測時点の active Session 件数（0以上の整数）
- `reserved_sessions`: 同じ Node / 同じ観測時点の reserved Session 件数（0以上の整数）

Node ID、Session ID、user ID、environment、URL、credential、任意 payload はこの dry-run の入力契約に含めません。余分な field は `INVALID_RECORD_FIELDS` として batch 全体を拒否します。

`max_sessions` が未設定・0・不明な場合は推測値を入れず `INVALID` とします。CPU / memory / network 使用率やユーザー単位の `max_concurrent_sessions` を代用してはいけません。

## 実行

repository root で実行します。

```bash
printf '%s\n' '{"max_sessions":10,"active_sessions":8,"reserved_sessions":1}' | \
  python apps/control-api/operations_capacity_alerts.py
```

80%を**超える**場合だけ alert 候補です。境界ちょうど80%は warning ではありません。

```text
(active_sessions + reserved_sessions) / max_sessions > 0.80
```

実装は floating-point 比較ではなく整数の交差積を使うため、境界の丸め誤差を持ち込みません。

成功時の出力例:

```json
{"matched_alerts":{"MEDIA_NODE_CAPACITY_HIGH":1},"records":1,"status":"MATCHED","unmatched_records":0,"violations":{}}
```

結果には入力値や識別子を再表示しません。`MATCHED` / `NO_MATCHES` は exit status `0`、入力 batch が不正なら `INVALID` / `3`、alert catalog の契約が evaluator と一致しなければ `INVALID_CATALOG` / `4` です。

## Fail-closed 境界

- 1 record は raw UTF-8 で 4 KiB 上限。通常 CLI は decode 前に byte 上限を適用し、oversized line を bounded chunk で drain します。
- duplicate JSON key、`NaN` / `Infinity`、invalid UTF-8、JSON 構文不正を拒否します。
- `bool`、float、文字列、負数、`max_sessions=0` を capacity 値として受け付けません。
- batch 内に1件でも不正 record があれば、それ以前の一致結果も抑制して batch 全体を `INVALID` にします。
- active + reserved が `max_sessions` を超える観測は入力破損と決めつけず、capacity 超過として alert 候補にします。reservation leak や二重割当の可能性は runbook で切り分けます。

## Catalog 契約

この evaluator は次がすべて一致する場合だけ実行します。

- alert ID: `MEDIA_NODE_CAPACITY_HIGH`
- severity: `warning`
- signal: `media_nodes.capacity_ratio`
- trigger: `threshold / issue11.media_node_capacity_80_percent`
- runbook: `docs/operations/media-node-capacity-high.md`

catalog 側が変更されたのに evaluator が古い場合、推測で継続せず `INVALID_CATALOG` にします。

## 非目標

これは本番 collector ではありません。Node inventory から `max_sessions` / active / reserved を収集する配線、environment / region / node ID による dedup、復旧通知、Discord / email 等の routing は別の実装単位です。

特に `max_sessions` の authority が repository 上でまだ確定していない段階では、この dry-run のために値を新設・推測しません。実運用への接続は #8 / #13 の capacity model と実測を先に確定してから行います。
