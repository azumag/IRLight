# Operations alert catalog

Issue #11 の alert / notification 要件を、外部監視製品や通知先へ依存しない機械可読な契約として `config/operations-alert-catalog.json` に定義します。目的は、alert 名・severity・signal・runbook・重複抑制の軸・復旧通知の有無を実装ごとにばらばらに増やさないことです。

この catalog 自体は監視を開始せず、メールや Discord を送らず、provider API や課金リソースを操作しません。実際の collector、threshold evaluation、通知 routing は別途実装・設定が必要です。

## 契約

各 alert は次を持ちます。

- `id`: 利用者任意値を含まない安定した大文字 ID。
- `severity`: `critical` または `warning`。
- `signal`: collector / evaluator が供給する論理 signal 名。catalog は signal 値そのものを保存しません。
- `trigger`: `event` または `threshold`。threshold の場合は数値をこの repository で推測せず、review 可能な `threshold_ref` を参照します。
- `runbook`: `docs/operations/` 配下の対応手順。
- `dedup_keys`: alert storm をまとめるために許可した低 cardinality / correlation key。credential や request body を key にしません。
- `recovery_notification`: Issue #11 の復旧通知要件に合わせ、現 schema では常に `true` とします。

`MEDIA_NODE_CAPACITY_HIGH` は Issue #11 が明示している **Node capacity 80%超** warning を `issue11.media_node_capacity_80_percent` として参照します。論理 signal は `media_nodes.capacity_ratio` で、検証済み `max_sessions` に対する active / reserved Session の占有率を想定します。ユーザー単位の `max_concurrent_sessions` を扱う `session_capacity_inspect_cli.py` / `session-capacity-exhaustion.md` とは別契約です。Node capacity の collector / scheduler metric がまだ接続されていない環境では、この warning をユーザー entitlement の値から推測して発火させません。

`NODE_RESOURCE_PRESSURE` と `NODE_RESOURCE_EXHAUSTED` は専用 runbook `media-node-resource-pressure.md` へまとめて routing します。前者は `media_nodes.resource_pressure` threshold、後者は `media_nodes.resource_exhausted` event です。CPU、memory、disk I/O、network 等のどの scalar / event reason を採用するか、単位、aggregation window、production threshold は collector / deployment が明示する契約であり、repository は heartbeat や capacity 等の別 signal から代用・合成しません。`operations.node_resource_pressure` は pressure warning 用 deployment threshold の参照名です。

それ以外の rate / pressure / backlog の数値 threshold は、実測値と運用負荷を確認せず固定しません。`operations.*` の `threshold_ref` は deployment 側で値を定義する必要がある未接続の契約名です。ただし `MEDIA_NODES_ALL_UNAVAILABLE` の `operations.media_nodes_all_unavailable` は、少なくとも1件の Node が `desired_state=RUNNING` である状況で available count が 0 になる論理境界を repository 内の read-only Node authority から安全に評価できるため、専用 dry-run を接続しています。Node が存在しない idle 環境や全 Node が明示的に停止されている状態は outage と推測しません。provisioning / stopping 等で zero available が何秒継続したら通知するかは deployment 側の運用 threshold として残します。

## Secret と個人情報

alert payload / notification に stream key、SRT passphrase、Authorization header、token、password、DSN、provider credential、webhook signing secret、destination secret を入れてはいけません。catalog の `dedup_keys` は `environment`、`region`、`service`、`node_id`、`session_id`、`reason_code` の allowlist に限定しています。

`session_id` / `node_id` は障害追跡用の識別子であり credential ではありませんが、通知先のアクセス制御と保持期間は別途決める必要があります。ユーザー入力、配信内容、request body を alert label / dedup key に昇格させません。

## 検証

repository root から次を実行すると catalog の strict JSON、schema、runbook link、dedup allowlist を read-only で検証できます。

```bash
python apps/control-api/operations_alert_catalog.py
```

成功時は alert 本文や signal 値を出さず、件数だけを `VALID alerts=<n>` と表示します。CI では `tests/test_operations_alert_catalog.py` が同じ validator を使い、Issue #11 の必須 runbook すべてに少なくとも1つの alert が紐づくこと、critical / warning の代表 alert が欠落しないこと、Node capacity 80% warning がユーザー entitlement capacity へ誤配線されないこと、Node resource pressure / exhaustion が heartbeat runbook へ誤配線されないことも固定します。

さらに `tests/test_operations_alert_detector_coverage.py` は、catalog にあるすべての `threshold` alert が下記の個別 dry-run evaluator と運用文書に明示的に対応していることを固定します。新しい threshold alert を catalog へ追加しただけで detector が未実装・未文書化のままになる場合、CI は失敗します。`event` alert は共通 `operations_event_alerts.py` / `event-alert-dry-run.md` の存在を同じテストで確認します。

## Event trigger の dry-run

`trigger.mode = event` の alert は `apps/control-api/operations_event_alerts.py` で structured JSONL と read-only に照合できます。

```bash
python apps/control-api/operations_event_alerts.py < captured.jsonl
```

この evaluator は一致した repository 管理下の alert ID と件数だけを返し、source log の message、correlation identifier、credential 等は再表示しません。invalid な JSONL が1件でも含まれる batch は部分一致結果を抑制して `INVALID` とし、同じ `event_type` を複数 alert に割り当てる曖昧な catalog も拒否します。

詳細な入力上限、fail-closed 条件、secret handling は `docs/operations/event-alert-dry-run.md` を参照してください。この dry-run は threshold alert、通知 routing、外部サービス、Session / provider の変更操作を実行しません。

## 接続済み threshold dry-run

repository 内に既に authority / aggregate input の安全な取得契約がある threshold は、汎用 threshold engine を先に決めず個別の read-only dry-run で検証します。

- `MEDIA_NODES_ALL_UNAVAILABLE`: `operations_node_availability_alerts.py` が canonical Node authority を既存 heartbeat inspector と同じ read-only reader で検査し、少なくとも1件の `desired_state=RUNNING` Node がある状況で、`status=READY`・heartbeat fresh を満たす Node の aggregate 件数が 0 の場合に Critical alert を一致させます。idle / 明示停止状態は一致させません。詳細は `media-node-availability-alert-dry-run.md`。
- `NODE_HEARTBEAT_DELAYED`: `operations_heartbeat_alerts.py` が canonical Node authority を既存 heartbeat inspector と同じ read-only reader で検査し、`NODE_HEARTBEAT_GRACE_SECONDS` 以上の stale expected heartbeat を aggregate alert 件数へ変換します。詳細は `node-heartbeat-alert-dry-run.md`。
- `NODE_RESOURCE_PRESSURE`: `operations_node_resource_pressure_alerts.py` が識別子を含まない事前集計済み `resource_pressure` JSONL と、deployment / operator が同一 metric・同一 unit で明示した `--threshold` を照合します。repository は production metric、unit、aggregation window、threshold を決めません。詳細は `node-resource-pressure-alert-dry-run.md`。
- `SESSION_PROCESS_CRASH_LOOP`: `operations_session_restart_alerts.py` が識別子を含まない aggregate restart-rate JSONL と、deployment / operator が明示した `--threshold` を照合します。repository は production threshold や aggregation window を決めず、rate が明示 threshold を超えた場合だけ一致させます。詳細は `session-process-crash-loop-alert-dry-run.md`。
- `SESSION_FAILURE_SURGE`: `operations_session_failure_alerts.py` が識別子を含まない事前集計済み failure-rate JSONL と明示 `--threshold` を照合します。部分的に壊れた batch では一致結果を抑制します。詳細は `session-failure-surge-alert-dry-run.md`。
- `MEDIA_NODE_CAPACITY_HIGH`: `operations_capacity_alerts.py` が識別子を含まない aggregate capacity JSONL を検査し、Issue #11 の 80% 超契約を判定します。詳細は `media-node-capacity-alert-dry-run.md`。
- `EGRESS_FAILURE_SURGE`: `operations_egress_failure_alerts.py` が識別子を含まない事前集計済み egress failure-rate JSONL と明示 `--threshold` を照合します。production window / denominator は推測しません。詳細は `egress-failure-surge-alert-dry-run.md`。
- `RECONNECT_RATE_HIGH`: `operations_egress_reconnect_alerts.py` が識別子を含まない事前集計済み reconnect-rate JSONL と明示 `--threshold` を照合します。詳細は `egress-reconnect-rate-alert-dry-run.md`。
- `INGEST_UNAVAILABLE`: `operations_ingest_unavailable_alerts.py` が識別子を含まない事前集計済み ingest connectivity failure-rate JSONL と明示 `--threshold` を照合します。外部 ingest probe は実行しません。詳細は `ingest-unavailable-alert-dry-run.md`。
- `ASSET_FAILURE_RATE_HIGH`: `operations_asset_failure_alerts.py` が識別子を含まない事前集計済み asset processing failure-rate JSONL と明示 `--threshold` を照合します。object storage や processing worker は操作しません。詳細は `asset-failure-rate-alert-dry-run.md`。
- `BILLING_WEBHOOK_DELAYED`: `operations_billing_webhook_alerts.py` が識別子を含まない事前集計済み最古 webhook backlog age JSONL と明示 `--threshold` を照合します。provider API、retry、Entitlement、返金等は操作しません。詳細は `billing-webhook-alert-dry-run.md`。

いずれも通知 routing、provider 操作、Session mutation、課金操作を行いません。authority が読めない場合や catalog 契約が drift した場合は、既知の正常値を推測して alert / recovery を確定しません。

## 今回決めないこと

- Prometheus / CloudWatch 等の collector や alert engine の採用。
- Discord / email 等の通知先、credential、routing 設定。
- `operations.*` threshold のうち rate / pressure / backlog / sustained duration に必要な本番数値。
- `media_nodes.resource_pressure` の production metric、単位、aggregation window。collector / deployment の実測契約として別途決めます。
- Media Node の `max_sessions` を推測で決めること。これは #8 / #13 の scheduler / load test の実測に従います。
- paging / escalation の担当者や当番表。
- alert を契機にした Session stop、provider cleanup / provisioning、failover、refund 等の自動変更。

これらは外部サービス、秘密情報、費用、または運用判断を伴うため、この catalog validator から実行しません。各 alert の一次対応は catalog が指す runbook を入口にし、破壊的操作は runbook の判断境界に従います。
