# Session DEGRADED lifecycle

Issue #4 の Continuity Session lifecycle では、Node Agent が報告する ingest quality の `DEGRADED` をユーザー Session の正式な状態として扱う。

## State transitions

```text
READY_WAIT_INGEST
  ├─ healthy ingest   -> LIVE
  └─ degraded ingest -> DEGRADED

LIVE
  ├─ quality degraded -> DEGRADED
  └─ ingest offline   -> HOLDING

DEGRADED
  ├─ quality recovered -> LIVE
  └─ ingest offline    -> HOLDING

HOLDING
  ├─ healthy ingest   -> LIVE
  └─ degraded ingest -> DEGRADED
```

`STOPPING / FINISHED / FAILED` などactiveではない状態に入った後のlate ingest observationは lifecycleを復活させない。stopがreconnectより優先される既存ルールを維持する。

## Ingest status mapping

- `ACCEPTED` / `WARNING` + online: healthy ingest
- `DEGRADED` + online: degraded ingest
- offline: `LIVE` または `DEGRADED` から `HOLDING`
- `PENDING` / `REJECTED` は単独ではSessionをLIVE/DEGRADEDへ昇格させない

Node-levelの `ingest.*` eventは従来どおり保持し、その後にSession lifecycle eventを追加する。

## Session lifecycle events

- `session.live`: `READY_WAIT_INGEST -> LIVE`
- `session.degraded`: `READY_WAIT_INGEST -> DEGRADED` または `LIVE -> DEGRADED`
- `session.holding`: `LIVE/DEGRADED -> HOLDING`
- `session.recovered`: `DEGRADED -> LIVE` または `HOLDING -> LIVE/DEGRADED`

各event payloadには少なくとも以下を含む。

- `node_id`
- `from_status`
- `to_status`
- ingest `status`
- `online`
- `bitrate_bps` / `max_bitrate_bps`
- `tracks`
- `quality`
- `reasons` / `warnings`
- `observed_at`

`session.degraded` と、劣化したまま `HOLDING -> DEGRADED` へ復帰する `session.recovered` では、Node observationの先頭reasonをreason codeとして保存する。切断による `session.holding` は `INGEST_DISCONNECTED` を使う。

Credential、stream key、password等のsecretはevent payloadへ含めない。

## Timing fields

- 最初のusable ingestが healthy / degraded のどちらでも `first_ingest_at` を設定する。
- lifecycle transitionでusable online inputを観測した時は `last_ingest_at` を更新する。
- `HOLDING` から online stateへ戻った時は `hold_deadline_at` をclearする。
- `LIVE/DEGRADED -> HOLDING` では `last_ingest_at` を切断観測時刻へ更新する。保持deadlineの復元・永続化は既存Reaperが担当する。

## Scope

このsliceはControl Plane Session stateと監査eventの整合性を扱う。GStreamer側の映像切替、待機素材fallback、engine process再起動後のmedia pipeline reconcileは後続sliceで扱う。
