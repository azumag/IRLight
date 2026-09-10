# Operations runbook index

IRLight の障害対応 runbook を一か所から参照するための索引です。Issue #11 が要求する主要障害シナリオを中心に、運用時の入口を明確にします。

## 共通安全原則

- 最初の切り分けは read-only を優先し、欠損・接続不能・応答不能を「空」「存在しない」「停止済み」と推測しない。
- stream key、SRT passphrase、Authorization header、token、password、DSN、provider credential などの secret を issue、chat、通常ログへ載せない。
- 進行中 Session へ影響する停止、cleanup、restore、failover、provider 操作、課金・補填操作は、対象と影響範囲を確認した明示的な運用判断として行う。
- `docker compose down -v`、volume prune、空 state の作成、marker 削除など、証拠や authority を失う操作を復旧の近道として使わない。
- 復旧後は単発の成功だけで完了とせず、readiness、heartbeat、ingest / egress、backlog など対象シナリオの継続的な正常化を確認する。

## Issue #11 必須 runbook

| シナリオ | Runbook |
| --- | --- |
| Media Node heartbeat 停止 | [media-node-heartbeat-stopped.md](media-node-heartbeat-stopped.md) |
| Session process crash loop | [session-process-crash-loop.md](session-process-crash-loop.md) |
| egress 大量失敗 | [egress-widespread-failure.md](egress-widespread-failure.md) |
| ingest 接続不能 | [ingest-connectivity-failure.md](ingest-connectivity-failure.md) |
| Control Plane 停止 | [control-plane-unavailable.md](control-plane-unavailable.md) |
| DB / Redis 障害 | [datastore-unavailable.md](datastore-unavailable.md) |
| object storage 障害 | [object-storage-unavailable.md](object-storage-unavailable.md) |
| TLS 証明書更新失敗 | [rtmps-certificate-update-failure.md](rtmps-certificate-update-failure.md) |
| Media Node capacity 80% 超 / 枯渇 | [media-node-capacity-high.md](media-node-capacity-high.md) |
| secret 漏えい疑い | [secret-exposure-suspected.md](secret-exposure-suspected.md) |
| billing webhook 停止 / backlog | [billing-webhook-stalled.md](billing-webhook-stalled.md) |
| 不正配信の緊急停止 | [emergency-abuse-stop.md](emergency-abuse-stop.md) |

## 関連運用手順

- deploy / rollback の判断・確認: [deploy-rollback.md](deploy-rollback.md)
- production Compose の read-only preflight: [production-deploy-preflight.md](../production-deploy-preflight.md)
- authority readiness: [state-readiness.md](state-readiness.md)
- backup restore drill: [state-restore-drill.md](state-restore-drill.md)
- state / provider 所有権の read-only 照合: [state-provider-reconciliation.md](state-provider-reconciliation.md)
- 認証 Session GC: [auth-session-gc.md](auth-session-gc.md)
- ユーザー単位の同時 Session 利用枠診断: [session-capacity-exhaustion.md](session-capacity-exhaustion.md)
- structured log redaction audit: [log-redaction-audit.md](log-redaction-audit.md)
- alert ID / severity / runbook / dedup 契約: [alert-catalog.md](alert-catalog.md)
- event-trigger alert の read-only dry-run: [event-alert-dry-run.md](event-alert-dry-run.md)
- Media Node 全停止 Critical alert の read-only dry-run: [media-node-availability-alert-dry-run.md](media-node-availability-alert-dry-run.md)
- Media Node heartbeat warning の read-only dry-run: [node-heartbeat-alert-dry-run.md](node-heartbeat-alert-dry-run.md)
- Media Node capacity 80% warning の read-only dry-run: [media-node-capacity-alert-dry-run.md](media-node-capacity-alert-dry-run.md)
- Media Node resource-pressure Warning alert の明示 threshold read-only dry-run: [node-resource-pressure-alert-dry-run.md](node-resource-pressure-alert-dry-run.md)
- Session process crash-loop Critical alert の明示 threshold read-only dry-run: [session-process-crash-loop-alert-dry-run.md](session-process-crash-loop-alert-dry-run.md)
- Session failure surge Critical alert の明示 threshold read-only dry-run: [session-failure-surge-alert-dry-run.md](session-failure-surge-alert-dry-run.md)
- Egress failure surge Critical alert の明示 threshold read-only dry-run: [egress-failure-surge-alert-dry-run.md](egress-failure-surge-alert-dry-run.md)
- Egress reconnect rate Warning alert の明示 threshold read-only dry-run: [egress-reconnect-rate-alert-dry-run.md](egress-reconnect-rate-alert-dry-run.md)
- Ingest connectivity failure Critical alert の明示 threshold read-only dry-run: [ingest-unavailable-alert-dry-run.md](ingest-unavailable-alert-dry-run.md)
- Asset processing failure-rate Warning alert の明示 threshold read-only dry-run: [asset-failure-rate-alert-dry-run.md](asset-failure-rate-alert-dry-run.md)
- Billing webhook backlog Warning alert の明示 threshold read-only dry-run: [billing-webhook-alert-dry-run.md](billing-webhook-alert-dry-run.md)

障害の根因が authority、deploy regression、secret 監査などにある場合は、必須12シナリオの runbook だけで復旧を推測せず、上記の専用手順を優先してください。

runbook や関連運用手順を追加・移動する場合は対応する inventory test も更新し、Issue #11 の必須シナリオと主要な関連運用契約が索引から脱落しないことを CI で確認します。
