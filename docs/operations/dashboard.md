# 運用 dashboard 契約（運用開始前の read-only 定義）

## 目的

Issue #11 が要求する「dashboard」成果物について、現時点で repository 内に実体がない gap を docs-only の形で閉じます。ここでは **何を表示するか、どの runbook に紐づけるか、どの metric/log schema に従うか** を固定し、実 collector / 可視化 backend / 本番 config は別 slice とします。

dashboard は値の推測や自動変更を行いません。表示するすべての値は `config/operations-metric-log-schema.json` と `config/operations-alert-catalog.json` の定義に従います。

## データソース

- metric schema: `config/operations-metric-log-schema.json`
- alert catalog: `config/operations-alert-catalog.json`
- runbook index: `docs/operations/README.md`
- structured log audit: `apps/control-api/log_redaction_inspect_cli.py`

## 共通表示ルール

- すべての panel に metric 名、unit、source、refresh interval、owner runbook を表示する。
- 集計のない ad hoc 値を dashboard に直接入力しない。
- `UNKNOWN` は `0` と表現せず、欠損として表示して原因調査へ誘導する。
- secret、stream key、SRT passphrase、token、DSN は panel に出さない。該当値は redaction audit を通す structured log のみを参照する。
- `version` と `environment` をすべての panel に付与し、cross-node / cross-deploy の比較ができるようにする。

## パネルグループ

### 1. Service availability

| Panel | Metric | Unit | Runbook |
| --- | --- | --- | --- |
| Media Node up/down | `media_nodes.available_count` | count | `media-node-heartbeat-stopped.md` |
| Heartbeat age | `media_nodes.heartbeat_age_seconds` | seconds | `node-heartbeat-alert-dry-run.md` |
| Node reservation | `media_nodes.reserved_session_count` | count | `media-node-capacity-high.md` |
| Process restart count | `process.restart_count` | count | `session-process-crash-loop.md` |
| OOM count | `process.oom_count` | count | `oom-kill-monitoring.md` |

### 2. Sessions

| Panel | Metric | Unit | Runbook |
| --- | --- | --- | --- |
| Active sessions | `sessions.active` | count | `session-failure-surge-alert-dry-run.md` |
| Reserved sessions | `sessions.reserved` | count | `media-node-capacity-high.md` |
| Holding sessions | `sessions.holding` | count | `session-degraded-lifecycle.md` |
| Failed sessions | `sessions.failed` | count | `session-failure-surge-alert-dry-run.md` |
| Prepare success rate | `sessions.prepare_success_rate` | ratio | `ingest-connectivity-failure.md` |
| Unexpected termination rate | `sessions.unexpected_termination_rate` | ratio | `session-process-crash-loop.md` |

### 3. Input / output

| Panel | Metric | Unit | Runbook |
| --- | --- | --- | --- |
| Ingest connections | `ingest.connections` | count | `ingest-connectivity-failure.md` |
| Ingest bitrate | `ingest.bitrate_bps` | bps | `ingest-quality.md` |
| Input FPS / audio rate | `ingest.fps`, `ingest.audio_rate_hz` | mixed | `ingest-quality.md` |
| Session prepare latency | `ingest.prepare_latency_seconds` | seconds | `production-deploy-preflight.md` |
| Egress connections | `egress.connections` | count | `egress-widespread-failure.md` |
| Egress reconnects | `egress.reconnects` | count | `egress-reconnect-rate-alert-dry-run.md` |
| Egress failure rate | `egress.failure_rate` | ratio | `egress-failure-surge-alert-dry-run.md` |
| Cutover / recovery latency | `cutover.latency_seconds`, `recovery.latency_seconds` | seconds | `continuity-hold-lifecycle.md` |

### 4. Resources and capacity

| Panel | Metric | Unit | Runbook |
| --- | --- | --- | --- |
| Capacity utilization | `node.capacity_utilization_percent` | percent | `media-node-capacity-high.md` |
| Host CPU | `host.cpu_utilization_percent` | percent | `host-pressure-monitoring.md` |
| Host memory | `host.memory_utilization_percent` | percent | `memory-pressure-monitoring.md` |
| Host disk | `host.disk_utilization_percent` | percent | `disk-pressure-monitoring.md` |
| Host network | `host.network_bytes` | bytes | `targeted-network-egress-health.md` |

### 5. Control plane, data, and billing

| Panel | Metric | Unit | Runbook |
| --- | --- | --- | --- |
| API latency / error rate | `api.latency_seconds`, `api.error_rate` | mixed | `state-readiness.md` |
| DB health | `db.health` | status | `datastore-unavailable.md` |
| Redis health | `redis.health` | status | `datastore-unavailable.md` |
| Object storage health | `object_storage.health` | status | `object-storage-unavailable.md` |
| Asset queue / failed jobs | `asset.processing_queue_depth`, `asset.processing_failed_count` | count | `asset-failure-rate-alert-dry-run.md` |
| Webhook failure rate / backlog | `webhook.failure_rate`, `webhook.backlog_depth` | mixed | `billing-webhook-stalled.md` |

## SLI/SLO表示ルール

β阶段で実測し更新します。dashboard では値そのものを出すだけでなく、以下を分けて表示します。

- ベストエフォートで維持する品質指標
- 絶対に切れないことを保証しないこと
- 閾値は `config/operations-alert-catalog.json` の `threshold_ref` を参照し、dashboard 側に独自閾値を持たせないこと

## 関連 Runbook

dashboard のすべての Critical / Warning panel は、該当する alert から直接 runbook へリンクします。runbook の完全な一覧は `docs/operations/README.md` にあります。

## 未実装の境界

- 実 dashboard backend / graph definition / provisioning file は未着手です。
- 実 collector の接続と本番 threshold tuning は別 Issue を必要とします。
- 本 doc は read-only 設計契約です。
