# Session DEGRADED lifecycle

Issue #4 の Continuity Session lifecycle では、Node Agent が報告する ingest quality の `DEGRADED` をユーザー Session の正式な状態として扱う。ただし、RTMP/SRT接続がオンラインでも映像または音声が実質停止している場合は、通常の品質劣化ではなく「利用可能な入力を喪失した」状態として `HOLDING` を優先する。

## State transitions

```text
READY_WAIT_INGEST
  ├─ healthy ingest   -> LIVE
  └─ degraded ingest -> DEGRADED

LIVE
  ├─ recoverable quality degradation -> DEGRADED
  ├─ video/audio unusable             -> HOLDING
  ├─ running format rejected          -> HOLDING
  └─ ingest offline                   -> HOLDING

DEGRADED
  ├─ quality recovered                -> LIVE
  ├─ video/audio unusable             -> HOLDING
  ├─ running format rejected          -> HOLDING
  └─ ingest offline                   -> HOLDING

HOLDING
  ├─ healthy ingest                   -> LIVE
  ├─ recoverable degraded ingest      -> DEGRADED
  └─ still-unusable media             -> HOLDING
```

`STOPPING / FINISHED / FAILED` などactiveではない状態に入った後のlate ingest observationは lifecycleを復活させない。stopがreconnectより優先される既存ルールを維持する。

## Ingest status mapping

- `ACCEPTED` / `WARNING` + online: healthy ingest
- `DEGRADED` + online + recoverable quality reason: degraded ingest
- `DEGRADED` + online + `VIDEO_TIMEOUT` / `AUDIO_TIMEOUT`:
  - `LIVE / DEGRADED` からは `HOLDING`
  - 既に `HOLDING` ならそのまま保持し、`session.recovered` を発行しない
- offline: `LIVE` または `DEGRADED` から `HOLDING`
- `REJECTED`:
  - 初回接続中の `READY_WAIT_INGEST` ではSessionを開始しない
  - 既に `LIVE / DEGRADED` のSessionでは入力が継続不能になったものとして `HOLDING`
  - codec / resolution / audio channel等のhard format policy違反は lifecycle reason `FORMAT_CHANGED` として記録し、具体的な拒否理由はevent payloadの `reasons` に残す
- `PENDING` は単独ではSessionをLIVE/DEGRADEDへ昇格させない

Node-levelの `ingest.*` eventは従来どおり保持し、その後にSession lifecycle eventを追加する。

## Session lifecycle events

- `session.live`: `READY_WAIT_INGEST -> LIVE`
- `session.degraded`: `READY_WAIT_INGEST -> DEGRADED` または `LIVE -> DEGRADED`
- `session.holding`: `LIVE/DEGRADED -> HOLDING`
- `session.recovered`: `DEGRADED -> LIVE` または `HOLDING -> LIVE/DEGRADED`

各event payloadには少なくとも以下を含む。

- `node_id`
- `from_state`
- `to_state`
- ingest `status`
- `online`
- `bitrate_bps` / `max_bitrate_bps`
- `tracks`
- `quality`
- `reasons` / `warnings`
- `observed_at`

reason codeは次の方針で保存する。

- 通常の `session.degraded`: Node observationの先頭quality reason
- 劣化したまま `HOLDING -> DEGRADED` へ復帰する `session.recovered`: Node observationの先頭quality reason
- publisher切断: `INGEST_DISCONNECTED`
- media sampleで映像停止: `VIDEO_TIMEOUT`
- media sampleで音声停止: `AUDIO_TIMEOUT`
- LIVE/DEGRADED中のhard format拒否: `FORMAT_CHANGED`
- format以外のhard rejection: 具体的な先頭reason（例: `BITRATE_TOO_HIGH`）

Credential、stream key、password等のsecretはevent payloadへ含めない。

## Timing fields

- 最初のusable ingestが healthy / degraded のどちらでも `first_ingest_at` を設定する。
- lifecycle transitionでusable online inputを観測した時は `last_ingest_at` を更新する。
- `HOLDING` から online usable stateへ戻った時は `hold_deadline_at` をclearする。
- `LIVE/DEGRADED -> HOLDING` では、publisher切断だけでなく `VIDEO_TIMEOUT` / `AUDIO_TIMEOUT` / running format rejectionでも `last_ingest_at` をHOLDING移行観測時刻へ更新する。保持deadlineの復元・永続化は既存Reaperが担当する。

## Scope

このsliceはControl Plane Session stateと監査eventの整合性、および実Dockerでの片側media停止・format変更回帰を扱う。GStreamer側の待機映像切替は既存Continuity state machine、standby asset fallbackはPR #52のNode-local fallback contractを使用する。長時間の切断復帰soakとSession/Media双方の安定窓統合は後続sliceで扱う。
