# Node heartbeat loss lifecycle

Issue #4 の Continuity Session lifecycleでは、Media Node上のmedia pipeline healthだけでなく、Node Agent自体がControl Planeから消えた場合も復旧不能な内部障害として監査・cleanupする必要がある。

## Source of truth

Node Agent heartbeatはControl Planeの `NODE_STATE_DIR/nodes.json` に保存される。ReaperはSession JSONの `updated_at` をheartbeat用途に流用しない。heartbeatごとにSessionを書き換えると、HOLDING deadline復元など本来のSession lifecycle時刻を汚すためである。

Reaperは1 sweepにつきNode registryを1回だけ読み、active Sessionに割り当てられた `node_id` と照合する。

## Grace rule

- 通常はNode recordの `last_heartbeat_at` を基準にする。
- Node登録直後など、まだ一度もheartbeatがない場合はSessionの `node_registered_at` を基準にする。
- `NODE_HEARTBEAT_GRACE_SECONDS` / `ReaperConfig.heartbeat_grace_seconds` の既定値は120秒。
- `now - baseline < grace` の間はSessionを維持する。短いNode Agent再起動やControl Planeとの一時通信断を許容するためである。
- `now - baseline >= grace` になった最初のReaper sweepでNode喪失を確定する。
- persisted wall clockが現在時刻より未来にある場合は、そのsweepではfailしない。時刻補正だけでSessionを破棄しないためのguardである。

## Failure transition

heartbeat timeoutを確定したactive Sessionは次の順で遷移する。

```text
PROVISIONING / BOOTSTRAPPING / READY_WAIT_INGEST / LIVE / DEGRADED / HOLDING
  -> FAILED_CLEANUP  reason=NODE_SHUTDOWN
  -> provider resource cleanup
  -> FAILED          reason=NODE_SHUTDOWN
```

Reaperは `session.failure_detected` を `NODE_SHUTDOWN` reasonで記録し、payloadに少なくとも以下を残す。

- `node_id`
- `from_state` / `to_state`
- `last_heartbeat_at`
- `node_registered_at`
- `heartbeat_grace_seconds`
- `cleanup_pending`

provider cleanup完了後の `session.failed` でも同じ `NODE_SHUTDOWN` reasonを保持する。

## Fail-safe registry handling

`nodes.json` ファイル自体が欠損・破損・読取不能、またはtop-level schemaが不正な場合、それは「全Nodeが停止した」という証拠ではない。そのsweepではheartbeat timeout判定をskipし、誤って全active Sessionをcleanupしない。

一方、正常に読めたregistry内から割当済み `node_id` が消えている場合は、最後に確認できる基準として `node_registered_at` を使う。grace超過後は `NODE_SHUTDOWN` とする。

## Race rules

- `STOPPING / FAILED_CLEANUP / FINISHED / FAILED` などactiveではないSessionはheartbeat timeoutで再分類しない。
- user stopや既に確定済みfailureがNode timeoutより先に進んだ場合、その状態が優先される。
- 同じSessionは `FAILED_CLEANUP` に入った時点でactive集合から外れるため、後続sweepで `session.failure_detected` を重複生成しない。
- pipeline自身がheartbeatを継続しながら停止しているケースは `PIPELINE_CRASHED` のhealth graceで扱い、Node Agent自体の消失とはreasonを分離する。

## Scope

このsliceはControl Planeに既に永続化されているNode heartbeatをReaper lifecycleへ接続する。Node process crash / Node Agent再起動 / Control Plane断の長時間・反復E2EはIssue #13のQA基盤で継続する。
