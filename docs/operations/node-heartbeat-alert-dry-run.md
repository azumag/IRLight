# Media Node heartbeat alert dry-run

Issue #11 の `NODE_HEARTBEAT_DELAYED` warning を、外部監視サービスや通知 credential を使わずに read-only で確認するための dry-run です。

`apps/control-api/operations_heartbeat_alerts.py` は canonical な `nodes.json` と initialization marker を、既存の `node_heartbeat_inspect_cli.py` と同じ非変更 reader で検査します。provider API、reaper、Session state、Node desired state、課金リソースは変更しません。

## 実行

repository root から、Control Plane と同じ Node authority directory を指定します。

```bash
python apps/control-api/operations_heartbeat_alerts.py \
  --node-state-dir /state \
  --heartbeat-grace-seconds "${NODE_HEARTBEAT_GRACE_SECONDS:-120}"
```

既定の alert catalog は `config/operations-alert-catalog.json` です。

## 判定

`desired_state=RUNNING` かつ terminal (`STOPPED` / `FAILED`) ではない Node だけを expected heartbeat の対象にします。

- heartbeat 済み Node: `last_heartbeat_at` からの経過時間を使います。
- 一度も heartbeat していない Node: registration (`created_at`) からの経過時間を使います。
- 既存 Reaper / inspector と同じく、経過時間が `NODE_HEARTBEAT_GRACE_SECONDS` 以上になった時点で stale とします。
- persisted timestamp が現在時刻より未来の場合は age を 0 に clamp し、時計補正だけで stale と断定しません。

catalog の `NODE_HEARTBEAT_DELAYED` が warning / `media_nodes.heartbeat_age_seconds` / `NODE_HEARTBEAT_GRACE_SECONDS` / heartbeat runbook という契約から drift している場合は `INVALID_CATALOG` として fail-closed にします。

## 出力

出力するのは aggregate 件数と repository 管理下の alert ID だけです。

```json
{"expected_running_nodes":2,"inspected_nodes":3,"matched_alerts":{"NODE_HEARTBEAT_DELAYED":1},"status":"MATCHED","violations":{}}
```

Node ID、Session ID、provider server ID、boot ID、token hash、authority record の raw 内容は出力しません。該当 Node を調べる必要がある場合は、アクセス制御された運用端末で `node_heartbeat_inspect_cli.py` を別途実行し、runbook に従って影響範囲を確認します。

## 終了コード

- `0`: `MATCHED` または `NO_MATCHES`
- `3`: Node authority が安全に読めず `UNAVAILABLE`
- `4`: alert catalog が不正または evaluator 契約と不一致

`UNAVAILABLE` を「Node が存在しない」や「復旧済み」と推測しません。state / marker が読めない場合は alert 判定そのものを抑制し、`media-node-heartbeat-stopped.md` と authority 復旧 runbook を使って原因を確認します。

## 非目標

この dry-run は通知 routing、重複抑制の外部 state、復旧通知、provider failover、Node stop / provision、Session cleanup、refund を実行しません。Discord / email 等の通知先と本番 collector 接続は別の運用判断です。
