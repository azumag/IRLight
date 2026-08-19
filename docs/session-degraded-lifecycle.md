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

ACTIVE STATE
  └─ media pipeline remains stopped beyond health grace     -> FAILED_CLEANUP -> FAILED
```

`STOPPING / FINISHED / FAILED` などactiveではない状態に入った後のlate ingest observationやlate pipeline healthは lifecycleを復活・失敗へ上書きしない。stopがreconnectやhealth failureより優先されるルールを維持する。

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

## Pipeline health failure gate

Node Agentはmedia supervisorのhealthをheartbeatの `media_health` として報告する。production nodeではMediaMTX / Continuityにrestart policyがあるため、一回の `stopped` 観測だけでSessionを破棄すると自動復旧と競合する。Control PlaneはNode stateに `media_unhealthy_since` を永続化し、明確な停止が一定時間継続した場合だけfatal failureを確定する。

- `media_health=running` を観測するとunhealthy timerを即resetする。
- `media_health=stopped / failed / crashed` の最初の観測ではSession stateを変えずtimerだけ開始する。
- 同状態が `NODE_MEDIA_HEALTH_FAILURE_GRACE_SECONDS`（既定30秒）以上継続すると、active Sessionを `FAILED_CLEANUP` へ遷移し、`failure_reason_code=PIPELINE_CRASHED` を永続化する。
- Nodeがheartbeat `status=FAILED` を明示した場合は、supervisor自身が回復不能と判断したものとしてgraceを待たず同じfatal pathへ入る。
- fatal確定時はNodeの `desired_state=STOPPED` とし、Agentにmedia stack停止を指示する。
- `session.failure_detected` / `PIPELINE_CRASHED` を監査eventとして残し、provider resource cleanup完了後にReaperが最終 `session.failed` を同じreason codeで記録する。
- Control Plane再起動でもgraceを短縮しないよう `media_unhealthy_since` は `nodes.json` に保持する。
- `STOPPING / FAILED_CLEANUP / FINISHED / FAILED` 等に先に進んだSessionはlate health failureで上書きしない。特にuser stopが常に優先される。
- `UNKNOWN` 等の非fatal値は新しいfatal timerを開始しない一方、`running` の証拠でもないため既存timerをresetしない。

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
- `session.failure_detected`: Node pipeline healthがfatal thresholdを満たし、active stateから `FAILED_CLEANUP` へ入った時
- `session.failed`: cleanup完了後に `FAILED_CLEANUP -> FAILED` が確定した時

各ingest lifecycle event payloadには少なくとも以下を含む。

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
- health graceを超えるmedia stack停止または明示FAILED: `PIPELINE_CRASHED`

Credential、stream key、password等のsecretはevent payloadへ含めない。

## Timing fields

- 最初のusable ingestが healthy / degraded のどちらでも `first_ingest_at` を設定する。
- lifecycle transitionでusable online inputを正式採用した時は `last_ingest_at` を更新する。
- `HOLDING` から安定窓を満たしてonline usable stateへ戻った時だけ `hold_deadline_at` をclearする。
- `LIVE/DEGRADED -> HOLDING` では、publisher切断だけでなく `VIDEO_TIMEOUT` / `AUDIO_TIMEOUT` / running format rejectionでも `last_ingest_at` をHOLDING移行観測時刻へ更新する。保持deadlineの復元・永続化は既存Reaperが担当する。
- `recovery_candidate_since` はcandidate開始時刻、`recovery_candidate_source_id` はそのcandidateを開始したpublisher sourceを表す。復帰完了・reset・明示state transition時に両方clearする。
- Node pipelineの `media_unhealthy_since` は最初の明確な停止観測時刻を表し、`running` 復帰またはNode state破棄まで保持する。

## Scope

このsliceはNode media supervisorのhealthをControl Plane Session lifecycleへ接続し、一時的なcontainer restartを許容しつつ継続停止を `PIPELINE_CRASHED -> FAILED_CLEANUP -> FAILED` として監査・cleanupできるようにする。HOLDING復帰の3秒gateはPR #56、片側media停止・format変更の実Docker回帰はPR #53、engine restart reconcileはPR #51、standby asset fallbackはPR #52で固定済み。10秒〜10分クラスの切断復帰soakは後続の #13 と連携して扱う。