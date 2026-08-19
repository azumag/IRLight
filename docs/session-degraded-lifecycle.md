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
  ├─ healthy ingest stable for recovery window              -> LIVE
  ├─ recoverable degraded ingest stable for recovery window -> DEGRADED
  └─ unstable / unusable / rejected / offline input         -> HOLDING
```

`STOPPING / FINISHED / FAILED` などactiveではない状態に入った後のlate ingest observationは lifecycleを復活させない。stopがreconnectより優先される既存ルールを維持する。

## Recovery stability gate

Continuity Media Engineは既に `RECOVERY_STABLE_SECONDS`（既定3秒）を使い、入力映像を再検出しても即座にstandbyからliveへ切り替えず `STABILIZING` を経由する。Control Plane Session lifecycleも同じ設定名・既定値を使い、`HOLDING` からの復帰だけを同じ安定窓でgateする。

- `HOLDING` 中に最初のusable online observationを受けた時点ではSessionを復帰させず、`recovery_candidate_since` と `recovery_candidate_source_id` を永続化する。
- 同一 `source_id` のusable observationが `RECOVERY_STABLE_SECONDS` 以上継続した時だけ `session.recovered` を発行して `LIVE` または `DEGRADED` へ遷移する。
- `offline` / `PENDING` / `REJECTED` / `VIDEO_TIMEOUT` / `AUDIO_TIMEOUT` を観測した場合は候補をresetする。
- recovery candidate中にpublisher `source_id` が変わった場合も安定窓を最初から数え直す。
- Control Plane再起動で安定窓を短縮しないようcandidate時刻は `sessions.json` に永続化する。
- candidate中も `hold_deadline_at` はclearしない。保持期限直前の短いflapでHOLDING寿命を延長し続けないため、安定確認完了前の入力は正式復帰扱いにしない。
- `RECOVERY_STABLE_SECONDS=0` はテスト等でgateを無効化するために利用できる。production推奨値はIssue #4の受け入れ条件どおり3〜5秒。

Node Agentはquality/event差分がないheartbeatでもSessionStoreへ最新observationを渡す。これにより、最初の `ingest.reconnected` 後に新しいNode-level eventが発生しなくても安定窓を完了できる。

## Ingest status mapping

- `ACCEPTED` / `WARNING` + online: healthy ingest
- `DEGRADED` + online + recoverable quality reason: degraded ingest
- `DEGRADED` + online + `VIDEO_TIMEOUT` / `AUDIO_TIMEOUT`:
  - `LIVE / DEGRADED` からは `HOLDING`
  - 既に `HOLDING` ならそのまま保持し、recovery candidateをresetする
- offline: `LIVE` または `DEGRADED` から `HOLDING`。既に `HOLDING` ならrecovery candidateをresetする
- `REJECTED`:
  - 初回接続中の `READY_WAIT_INGEST` ではSessionを開始しない
  - 既に `LIVE / DEGRADED` のSessionでは入力が継続不能になったものとして `HOLDING`
  - `HOLDING` 中はrecovery candidateをresetする
  - codec / resolution / audio channel等のhard format policy違反は lifecycle reason `FORMAT_CHANGED` として記録し、具体的な拒否理由はevent payloadの `reasons` に残す
- `PENDING` は単独ではSessionをLIVE/DEGRADEDへ昇格させない。`HOLDING` 中ならrecovery candidateをresetする

Node-levelの `ingest.*` eventは従来どおり保持し、その後にSession lifecycle eventを追加する。安定窓待ちだけを理由に追加の監査eventを毎heartbeat生成しない。

## Session lifecycle events

- `session.live`: `READY_WAIT_INGEST -> LIVE`
- `session.degraded`: `READY_WAIT_INGEST -> DEGRADED` または `LIVE -> DEGRADED`
- `session.holding`: `LIVE/DEGRADED -> HOLDING`
- `session.recovered`: `DEGRADED -> LIVE`、または安定窓を満たした `HOLDING -> LIVE/DEGRADED`

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
- 劣化したまま安定窓を満たして `HOLDING -> DEGRADED` へ復帰する `session.recovered`: Node observationの先頭quality reason
- publisher切断: `INGEST_DISCONNECTED`
- media sampleで映像停止: `VIDEO_TIMEOUT`
- media sampleで音声停止: `AUDIO_TIMEOUT`
- LIVE/DEGRADED中のhard format拒否: `FORMAT_CHANGED`
- format以外のhard rejection: 具体的な先頭reason（例: `BITRATE_TOO_HIGH`）

Credential、stream key、password等のsecretはevent payloadへ含めない。

## Timing fields

- 最初のusable ingestが healthy / degraded のどちらでも `first_ingest_at` を設定する。
- lifecycle transitionでusable online inputを正式採用した時は `last_ingest_at` を更新する。
- `HOLDING` から安定窓を満たしてonline usable stateへ戻った時だけ `hold_deadline_at` をclearする。
- `LIVE/DEGRADED -> HOLDING` では、publisher切断だけでなく `VIDEO_TIMEOUT` / `AUDIO_TIMEOUT` / running format rejectionでも `last_ingest_at` をHOLDING移行観測時刻へ更新する。保持deadlineの復元・永続化は既存Reaperが担当する。
- `recovery_candidate_since` はcandidate開始時刻、`recovery_candidate_source_id` はそのcandidateを開始したpublisher sourceを表す。復帰完了・reset・明示state transition時に両方clearする。

## Scope

このsliceはMedia Engineが既に使う3秒のrecovery stability windowとControl Plane Session復帰条件を揃え、短時間のreconnect flapで `HOLDING -> LIVE/DEGRADED` が即時発火しないようにする。片側media停止・format変更の実Docker回帰はPR #53、engine restart reconcileはPR #51、standby asset fallbackはPR #52で固定済み。10秒〜10分クラスの切断復帰soakは後続の #13 と連携して扱う。
