# Ingest device compatibility

Issue #3 / #39 の実機検証記録です。

この表では、自動CIで確認できる「プロトコル実装が動く」ことと、実際のpublisher UI・OS・firmwareから接続できることを分けて扱います。実機で確認していない組み合わせは推測で `PASS` にせず、必ず `UNVERIFIED` のまま残します。

## Status

- `PASS`: 必須シナリオを実機で完走
- `PARTIAL`: publishは可能だが一部必須シナリオに制約あり
- `FAIL`: IRLightまたはpublisher側の再現可能な非互換あり
- `BLOCKED`: アカウント・機材・ネットワーク等がなく実行不能
- `UNVERIFIED`: 未実施

## Current matrix

| Publisher | Platform / model | Protocol | Status | Last tested | Notes |
|---|---|---:|---|---|---|
| OBS Studio | Desktop / TBD | RTMPS | UNVERIFIED | - | Issue #39必須 |
| Larix Broadcaster | iOS / TBD | RTMP | UNVERIFIED | - | Issue #39必須 |
| Larix Broadcaster | iOS / TBD | SRT | UNVERIFIED | - | SRT優先追加確認 |
| Larix Broadcaster | Android / TBD | RTMP | UNVERIFIED | - | iOS/Androidいずれか1台は必須、両方できれば望ましい |
| Larix Broadcaster | Android / TBD | SRT | UNVERIFIED | - | 同上 |
| PRISM Live Studio Mobile | iOS or Android / TBD | Custom RTMP | UNVERIFIED | - | Larixとは別系統のスマホpublisherとして必須 |
| Hardware encoder | Model TBD | RTMP/RTMPS | BLOCKED | - | 利用可能な実機があれば確認。機材なしは `BLOCKED_NO_HARDWARE` をNotesへ記録 |
| Hardware encoder | Model TBD | SRT | BLOCKED | - | 機材が対応する場合のみ |

## Test targets and why

### OBS Studio

Desktopの基準publisher。IRLightで発行したRTMP/RTMPS接続情報を利用し、720p30または1080p30、H.264 + AAC、48kHz audioで確認します。

### Larix Broadcaster

Softvelumの公式情報では、Larix BroadcasterはiOS/Android向けのmobile broadcasterで、RTMPとSRTを含む複数の送出プロトコルをサポートしています。

Official references:
- https://softvelum.com/larix/
- https://softvelum.com/larix/docs/

IRLightではRTMPに加えてSRT caller側の実機互換性を確認する対象とします。

### PRISM Live Studio Mobile

PRISMの公式ガイドではiOS/Android mobile appからCustom RTMPを利用できます。Larixと異なる実装系のmobile publisherとして確認します。

Official references:
- https://guide.prismlive.com/mobile/guides/streaming/rtmp/using-custom-rtmp
- https://prismlive.com/en_us/mobile.html

### Hardware encoder

モデルを固定しません。利用可能な機材が出た時点で、model / firmware / protocol capabilityを記録して実施します。実機がない状態で互換性を推定してPASSにはしません。

## Required scenario per publisher

### 1. Prepare

記録するもの:

- test date/time and timezone
- device model
- OS version
- app / firmware version
- network: Ethernet / Wi-Fi / 4G / 5G
- protocol
- requested profile
  - video codec: H.264
  - audio codec: AAC
  - resolution: 1280x720 or 1920x1080
  - fps: 30 preferred
  - audio: 48kHz, mono or stereo
  - total bitrate: <= 6Mbps
- IRLight Session ID

credential自体はテスト記録へコピーしません。

### 2. Valid credential publish

1. User Sessionをprepareする。
2. ingest credentialを発行する。
3. publisherへIRLightのconnection infoを設定する。
4. publishを開始する。
5. 30秒以上継続する。

PASS条件:

- `ingest.connected`
- `ingest.format_detected`
- Session status `LIVE`
- hard policyで `REJECTED` されない
- 30秒間、映像/音声が継続する

`WARNING` / `DEGRADED` が出た場合はreason codeとpublisher設定を記録し、原因が意図した設定差なら `PARTIAL`、IRLight側不具合なら `FAIL` とします。

### 3. Disconnect / reconnect

1. publisherを停止するか、短時間networkを切断する。
2. Sessionが `HOLDING` になることを確認する。
3. `ingest.disconnected` を確認する。
4. 同じ有効credentialで再接続する。

PASS条件:

- `HOLDING -> LIVE`
- `ingest.reconnected`
- 再接続後もformat/policyが正常

可能なmobile publisherではWi-Fiから4G/5G、またはその逆への切替も追加確認します。Node-local auth cacheはsource IPをcache keyに含めないため、Control Plane一時障害時の再接続でもIP変更を許容する設計です。

### 4. Invalid / revoked credential

少なくとも1回、誤ったcredentialまたは明示revoke済みcredentialで接続します。

PASS条件:

- publishが成立しない
- 既知Sessionなら `ingest.auth_failed`
- event /通常ログにraw password、stream key、credential secretが含まれない

### 5. Format boundary

可能ならpublisher UI上で1つだけ意図的にunsupported profileへ変更します。

例:

- 640x360
- H.265 / HEVC（選択できる場合）
- 6Mbpsを明確に超えるbitrate

PASS条件:

- IRLightが `ingest.rejected` とreason codeを記録する
- 正しいprofileへ戻した後に再接続可能

## Per-run record template

以下をこのファイル末尾へ追記するか、Issue #39へコメントします。

```markdown
### YYYY-MM-DD HH:MM TZ — <publisher>

- Result: PASS | PARTIAL | FAIL | BLOCKED
- Device/model:
- OS:
- App/firmware:
- Network:
- Protocol:
- Session ID:
- Video: H.264 / 1280x720 / 30fps / ... kbps
- Audio: AAC / 48kHz / 1ch or 2ch / ... kbps
- Connected event sequence:
- Format event sequence:
- Disconnect event sequence:
- Reconnect event sequence:
- Auth-failed event sequence:
- Degraded/rejected reason code:
- Secret scan: PASS | FAIL
- Notes:
```

## Issue #3 acceptance

Issue #3の実機要件は次を満たした時点で完了とします。

- OBS Studio: `PASS`
- Larix Broadcaster: `PASS`
- PRISM Live Studio Mobile: `PASS`
- スマホpublisherが異なる2実装系で `PASS`

Hardware encoderは利用可能な機材がない場合、Issue #39で `BLOCKED_NO_HARDWARE` として独立管理します。機材が利用できる場合は同じscenarioで確認します。
