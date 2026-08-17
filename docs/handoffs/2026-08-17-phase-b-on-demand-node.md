# 引き継ぎ: Phase BオンデマンドMedia Node実装

- Date: 2026-08-17
- Decision ADR: `docs/adr/0002-phase-b-on-demand-media-node.md`
- Detail design: `docs/architecture/on-demand-media-node.md`
- Future Gateway: #20
- Primary implementation issues: #8, #9, #11, #12, #14

## 1. 今回確定した方針

Phase Bは予約制・常設Media Nodeではなく、次の方式で進める。

> ユーザーがWeb UIの「リレーを起動」を押すと、一時VPSを1台作成する。ユーザーごとのServer URLとstream keyは基本固定とし、NodeのIPだけをSessionごとに切り替える。配信終了後はVPSを停止するだけでなく削除する。

既定値：

- 1 Session = 1 VPS
- ConoHa VPS東京・時間課金を第一候補
- 2GB / 3vCPU候補から実測開始
- 720p30、最大6Mbps程度、1入力1出力
- Node READY後60分未配信で削除
- 一度LIVEになった後は入力断から最大30分HOLDING
- 明示的な配信終了で削除
- 絶対最大稼働時間12時間
- 外部watchdogがorphan server / volumeを回収
- Custom RTMP出力は初期無効
- 常設Ingest GatewayはPhase B対象外。将来Issue #20

## 2. 現在のリポジトリ状態

確認済みのPhase 0資産：

- `docker-compose.poc.yml`
  - MediaMTX
  - GStreamer continuity
  - FastAPI control UI/API
  - 公開portはすべて`127.0.0.1` bind
- `config/mediamtx.yml`
  - RTMP / SRT / RTSP / HLS
  - API / metricsはcontainer内で有効だがhost公開なし
  - pathは`live/input`と`output/relay`
  - 認証なし、Phase 0のみ
- `apps/continuity/`
  - 待機映像
  - 入力断検知
  - 自動復帰
  - desired / actual audio state
  - `COMPOSITED_VIDEO_POC` / `AUDIO_PROCESSED` / `PASSTHROUGH`
- `apps/control-api/`
  - 単一Session向け共有JSON制御
  - 無認証
  - `/api/status`
  - `/api/audio`
- `docs/phase0-verification.md`
  - Docker smoke
  - 切断・復帰
  - ミュート
  - 60分の参考試験
- `docs/profile-comparison.md`
  - profile別のローカル負荷結果

### 重要

現在のPoCを公開環境向けとして扱わない。

- 認証なし
- TLSなし
- Secret配送なし
- 複数Sessionなし
- Session lifecycleなし
- provider resource cleanupなし

Phase B実装では、PoCを消して作り直すのではなく、Continuity EngineをMedia Node内部部品として再利用する。

## 3. 作業開始前に読むもの

1. `AGENTS.md`
2. `docs/adr/0001-phase0-vertical-slice.md`
3. `docs/adr/0002-phase-b-on-demand-media-node.md`
4. `docs/architecture/on-demand-media-node.md`
5. `docs/phase0-verification.md`
6. `docs/profile-comparison.md`
7. Issue #8、#9、#11、#12、#14、#20

## 4. 非交渉条件

以下を簡略化のために破らないこと。

### Public exposure

- Control UI/APIを無認証で公開しない
- MediaMTX API / metricsを公開しない
- HLS / RTSP previewを標準公開しない
- ingestは認証付きRTMPSを優先する
- plain RTMPは互換性上必要な場合だけ明示的に有効化する
- SRT UDPはSRT sliceで追加し、security groupを限定する

### Secret

- Twitch / YouTube / Kick keyをGit、Issue、logへ書かない
- provider credentialをGit、`user-data`、browserへ渡さない
- Destination secretをenvironment variableやprocess argumentへ載せない
- Nodeではtmpfsのsecret fileを使う
- bootstrap tokenは一回限り・短時間・provider instanceに紐づける
- fixed ingest keyはdigest保存し、再表示ではなくrotationを行う

### Cleanup

- VPSの電源OFFだけで完了としない
- serverを削除する
-不要なboot volumeも削除する
- DNSから前回IPを外す
- Session grantとNode credentialを失効する
- workflowとは別にscheduled reaperを用意する

### Verification

- 実行していない試験をPASSと書かない
- 通常のruntime検証は10分程度を基本とする
- 60分・12時間timeoutはfake clockまたは短縮設定で検証する
- 長時間soakを初手にしない
- 外部配信試験は限定公開・テスト用stream keyだけを使う

## 5. 推奨ディレクトリ案

最初の実装PRで必要に応じて調整してよいが、責務を混ぜない。

```text
apps/
  control-plane/       # Cloudflare Worker / Workflow / API
  node-agent/          # bootstrap, heartbeat, local supervisor
  continuity/          # existing media engine
  control-api/         # existing Phase 0 UI/API; compatibility only

infra/
  providers/
    conoha/            # provider adapter / docs / fixtures
  media-node/          # production compose, cloud-init template

scripts/
  provider/            # admin-only create/list/delete spike tools

tests/
  lifecycle/           # state, compensation, reaper
  provider/            # fake provider, mapping tests
```

Phase 0の`docker-compose.poc.yml`は再現用に残し、本番候補は別のcomposeとして追加する。

## 6. 実装順

UIから作り始めない。最初に、必ずresourceを作って消せることを証明する。

### PR 1: ConoHa provider spike

目的：

- API token取得
- boot volume作成
- VPS作成
- metadata tag
- public IPv4取得
- server削除
- volume削除
- managed resource一覧

成果物候補：

- provider client
- admin CLI
- fake provider
- unit test
- cleanup確認手順

完了条件：

- test用VPSを作成できる
- serverとvolumeを削除できる
- provider一覧で残骸がない
- retryしても同じSessionのresourceを重複作成しない方針がある
- credentialがlogへ出ない

このPRではまだインターネットへingestを公開しない。

### PR 2: Node bootstrapとproduction compose

目的：

- one-time bootstrap token
- Node Agent登録
- pinned imageまたはprebuilt image
- tmpfs Secret
- health / heartbeat
- graceful stop

変更点：

- `build:`ではなく事前build済みimageを使えるようにする
- MediaMTX API / metricsは内部のみ
- production用port exposureを別composeへ定義する
- `EGRESS_URL_FILE`等、Secret file対応をcontinuityへ追加する
- Node AgentへDocker socketを渡す場合は責務と権限を限定する

完了条件：

- 新規Nodeが自動bootstrapする
- Secretが`docker inspect`とprocess argumentsへ出ない
- Node AgentがREADYとheartbeatを返す
- stopでメディアprocessを終了できる

### PR 3: Session lifecycle / workflow / reaper

目的：

- persistent Session state
- prepare / stop API
- idempotency
- provisioning workflow
- compensation
- no-ingest / hold / absolute deadline
- scheduled reaper

最初はfake providerで全異常系を通し、その後ConoHa sandbox/internal alphaで確認する。

完了条件：

- double prepareでVPSが1台だけ
- stop during provisioningで作成済みresourceを回収
- workflow failure後もreaperが回収
- server削除後のvolume残留を検知・削除

### PR 4: 固定hostname、RTMPS、fixed credential

目的：

- user opaque hostname
- DNS adapter
- DNS activate / park
- public certificate
- MediaMTX ingest auth
- fixed high-entropy key
- rotation
- external TLS probe

未決定事項：

- per-host certificateまたはwildcard
- DNS park方式
- certificate renewal worker

Phase Bへ出す前にsecurity reviewを行う。

### PR 5: User UI

画面状態：

```text
停止中
準備中
配信可能
配信中
接続が切れました・待機画面を配信中
終了中
終了
失敗
```

必要な操作：

- リレーを起動
- 接続情報をコピー
- stream key再発行
- 音声ミュート / 解除
- 配信終了

UIは`PROVISIONING`を単一spinnerで隠さず、少なくともNode作成、起動確認、接続準備の段階を表示する。

### PR 6: SRT

RTMPSのcreate / ready / publish / hold / cleanupが安定してから追加する。

## 7. 最初の小さな実装目標

最初のend-to-endは、ユーザーUIではなく管理者CLIでよい。

```text
admin start <session-id>
  ↓
ConoHa volume/server作成
  ↓
Node bootstrap
  ↓
local/limited RTMPS readiness
  ↓
10分のtest publish
  ↓
admin stop <session-id>
  ↓
server/volume削除
  ↓
resource inventoryが空
```

これが通る前に、課金、公開UI、複数ユーザー、SRTへ広げない。

## 8. Provider実装上の注意

### ConoHa resource lifecycle

- server作成前にboot volumeが必要
- server IDとvolume IDを別々に保存する
- create APIがtimeoutしてもresourceが実際には作成済みの場合があるため、metadata / Session IDで検索してから再作成する
- delete APIのHTTP成功だけで完了とせず、一覧から消えたことを確認する
- server削除後にvolumeを削除する
- token有効期限をまたぐsleepでは再認証する
- API sub-userへ必要なroleだけ付与する

### Metadata / naming

provider上の全resourceへ最低限次を付ける。

```text
irlight-managed=true
irlight-session-id=<uuid>
irlight-user-id=<opaque-id>
irlight-created-at=<timestamp>
irlight-delete-after=<timestamp>
irlight-environment=<dev|beta|prod>
```

user emailやstream keyをmetadataへ入れない。

### Idempotency

provider API自体のIdempotency-Keyだけに依存しない。

- DBにSession lock
- provider metadata検索
- `ensureVolume`
- `ensureServer`
- create result保存
- retry前に現状確認

を組み合わせる。

## 9. Node bootstrap上の注意

`user-data`はSecret配送路ではない。

渡してよいもの：

- bootstrap endpoint
- one-time token
- Session ID
- Agent version

渡してはいけないもの：

- Destination stream key
- fixed ingest key平文
- RTMPS private key
- ConoHa credential
- Cloudflare API token

bootstrap token交換後、Nodeは用途別のSecretをtmpfsへ取得する。tokenは即時consumedにする。

Node imageには共通の待機素材を同梱できるが、ユーザー固有Secretや素材を焼き込まない。

## 10. Timeoutとdeadline

deadlineの正本はControl Plane DBへ保存し、Nodeにも署名済み設定として渡す。

```text
provisioning timeout: READYまで10分
no-ingest timeout:    READY後60分
hold timeout:         LIVE後の入力断から30分
absolute deadline:    最大12時間
```

NodeはControl Planeが一時停止してもabsolute deadlineを超えて配信を続けない。provider resourceの最終削除は外部reaperが行う。

## 11. 実測項目

### 起動

- volume create
- server create
- Agent bootstrap
- image pull / service start
- DNS update
- external TLS probe
- total prepare latency

### Media

- 720p30 / 6Mbps
- `AUDIO_PROCESSED`
- CPU / CPU steal
- RSS
- network
- frame drop
- A/V sync
- Twitch / YouTube / Kick egress

### Cleanup

- graceful stop時間
- DNS park時間
- server delete時間
- volume delete時間
- orphan count
- provider請求上の稼働時間

2GB候補は実測後に採否を決める。ローカルDockerのCPU値だけで決めない。

## 12. 必要なSecret名の例

値はリポジトリへ入れない。

```text
CONOHA_IDENTITY_ENDPOINT
CONOHA_COMPUTE_ENDPOINT
CONOHA_VOLUME_ENDPOINT
CONOHA_PROJECT_ID
CONOHA_API_USER_ID
CONOHA_API_USER_PASSWORD
CONOHA_REGION
CONOHA_PLAN_ID
CONOHA_IMAGE_ID
CONOHA_SECURITY_GROUP_ID

CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_ZONE_ID
CLOUDFLARE_DNS_API_TOKEN
CLOUDFLARE_ACCESS_AUD
CLOUDFLARE_ACCESS_TEAM_DOMAIN

BOOTSTRAP_TOKEN_SIGNING_KEY
NODE_CREDENTIAL_SIGNING_KEY
DESTINATION_ENCRYPTION_KEY
INGEST_SERVICE_DOMAIN
```

実装時は、用途ごとにAPI tokenを分け、不要な権限を付けない。

## 13. 失敗理由code候補

```text
PROVIDER_AUTH_FAILED
PROVIDER_VOLUME_CREATE_FAILED
PROVIDER_SERVER_CREATE_FAILED
PROVIDER_SERVER_TIMEOUT
NODE_BOOTSTRAP_TIMEOUT
NODE_VERSION_MISMATCH
SECRET_DELIVERY_FAILED
MEDIA_HEALTH_FAILED
DNS_UPDATE_FAILED
EXTERNAL_INGEST_PROBE_FAILED
INGEST_NOT_STARTED_TIMEOUT
INGEST_AUTH_FAILED
DESTINATION_AUTH_FAILED
HOLD_TIMEOUT
ABSOLUTE_DEADLINE
USER_STOP
CLEANUP_SERVER_FAILED
CLEANUP_VOLUME_FAILED
ORPHAN_RESOURCE_DETECTED
```

ユーザー向け文言と内部詳細を分離する。

## 14. Phase Bへ進める条件

- start / stop / timeoutが冪等
- serverとvolumeが確実に削除される
- reaperがorphanを回収する
- fixed hostname / fixed keyでRTMPS publish可能
- Access JWTとSession ownershipを検証
- Secretがlog、Issue、process args、Docker envへ漏れない
- MediaMTX API / metricsが外部から到達不能
- 2GBまたは4GBの採用根拠が対象VPS実測にある
- 限定公開の外部配信試験が成功
- Node障害では配信維持できないことをβ参加者へ説明

## 15. 作業者への最初の指示

次の作業では、まず #8 の一部として「ConoHa provider spike + cleanup proof」を実装する。

- UIはまだ作らない
- 本物のDestination keyを使わない
- public ingestをまだ開けない
- createだけでなくdeleteを同じPRで実装する
- serverとvolumeの両方が消えたことを結果へ記録する
- resource metadataとidempotencyを最初から入れる
- 10分程度の検証で十分

PRには、実行したcommand、作成されたresource IDを伏せた結果、削除確認、未実施項目を記録すること。
