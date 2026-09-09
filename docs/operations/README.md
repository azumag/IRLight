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
| capacity 枯渇 | [session-capacity-exhaustion.md](session-capacity-exhaustion.md) |
| secret 漏えい疑い | [secret-exposure-suspected.md](secret-exposure-suspected.md) |
| billing webhook 停止 / backlog | [billing-webhook-stalled.md](billing-webhook-stalled.md) |
| 不正配信の緊急停止 | [emergency-abuse-stop.md](emergency-abuse-stop.md) |

## 関連運用手順

Issue #11 の12シナリオ以外にも、同じ `docs/operations/` 配下に state readiness / restore、認証 Session GC、structured log redaction などの補助手順があります。障害の根因が authority や secret 監査にある場合は、該当する専用手順を優先してください。

runbook を追加・移動する場合は `tests/test_operations_runbook_inventory.py` も更新し、Issue #11 の必須シナリオが索引から脱落しないことを CI で確認します。
