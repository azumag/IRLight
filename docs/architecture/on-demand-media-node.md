# オンデマンドMedia Node詳細設計

- Status: Design approved / implementation pending
- Date: 2026-08-17
- Decision: `docs/adr/0002-phase-b-on-demand-media-node.md`
- Related: #3, #6, #8, #9, #11, #12, #14, #20

## 1. 目的

Phase Bでは予約システムと常設Media Nodeを持たず、ユーザーがWeb UIの「リレーを起動」を押した時だけ、一時VPSを作成する。

本設計の中心は次のとおりである。

- 1 Session = 1 VPS
- ユーザーごとの接続エンドポイントは固定
- stream keyは基本固定だが即時ローテーション可能
- NodeのIPはSessionごとに変わる
- Node READY後1時間publishがなければ自動削除
- LIVE後の入力断は最大30分HOLDING
- 明示終了または最大12時間で削除
- Node自身とは別のwatchdogが削除漏れを回収
- Phase Bでは常設Ingest Gatewayを置かない

## 2. Non-goals

本設計では次を扱わない。

- OBS等からpublishしただけでVPSを自動起動する常設Gateway方式（#20）
- 配信中Nodeの透過フェイルオーバー
- 複数入力または複数出力
- 24時間常設配信
- 予約枠・カレンダー
- Custom RTMP/RTMPSの一般開放
- Kubernetes等のcluster orchestrator
- 映像録画

## 3. ユーザーフロー

### 初回設定

1. ユーザーがIRLightへログインする
2. Twitch、YouTube、Kick等のDestinationを登録する
3. IRLightがユーザー固有の固定接続情報を発行する
4. ユーザーはOBS、スマートフォン、ハードウェアencoderへ一度だけ設定する

```text
Server:     rtmps://u-<opaque-id>.ingest.<service-domain>/live
Stream key: irl_<high-entropy-random>
```

### 毎回の配信

1. UIで「リレーを起動」を押す
2. UIは `準備中` を表示する
3. 一時VPS、Node Agent、MediaMTX、Continuity Engine、RTMPS listenerを起動する
4. 固定ホスト名をNode IPへ切り替える
5. 外部probeが通った後にUIを `配信可能` とする
6. ユーザーがOBS等で配信開始する
7. UIは `配信中`、`HOLDING`、`配信終了中` 等を表示する
8. ユーザーが終了を押すか、期限に達したらVPSを削除する

ユーザーへIPアドレスを見せず、Nodeが変わっても同じServer URLとstream keyを使えるようにする。

## 4. 論理構成

```text
                           Cloudflare Access
                                  │
                                  ▼
┌──────────────────────────────────────────────────────┐
│ Control Plane                                       │
│                                                      │
│  Web UI / API                                        │
│    ├─ Access JWT検証                                 │
│    ├─ Session所有権・同時利用上限                    │
│    ├─ Destination / fixed ingest credential          │
│    └─ Start / Stop / Status                          │
│                                                      │
│  Lifecycle Workflow                                  │
│    ├─ provider API                                   │
│    ├─ bootstrap待機                                  │
│    ├─ DNS切替                                        │
│    ├─ timeout                                        │
│    ├─ retry / compensation                           │
│    └─ cleanup                                        │
│                                                      │
│  Persistent DB / Secret store / Scheduled Reaper     │
└──────────────┬───────────────────────┬───────────────┘
               │                       │
       ConoHa VPS API          Cloudflare DNS API
               │                       │
               ▼                       ▼
       temporary VPS        u-<id>.ingest.<domain>
               │
               ▼
┌──────────────────────────────────────────────────────┐
│ Media Node                                           │
│                                                      │
│  Node Agent                                          │
│    ├─ one-time bootstrap                             │
│    ├─ heartbeat / deadline                           │
│    ├─ secret file delivery                           │
│    └─ graceful stop                                  │
│                                                      │
│  MediaMTX                                            │
│    ├─ RTMPS input                                    │
│    ├─ SRT input（後続slice）                         │
│    ├─ local RTSP relay                               │
│    └─ internal API / metrics only                    │
│                                                      │
│  Continuity Engine                                   │
│    ├─ AUDIO_PROCESSED candidate                      │
│    ├─ standby / recovery                             │
│    └─ Twitch / YouTube / Kick egress                 │
└──────────────────────────────────────────────────────┘
```

## 5. Component responsibilities

### 5.1 Control API

- Cloudflare Access JWTの署名、issuer、audienceを検証する
- JWTのidentityをIRLight userへ解決する
- userが自身のSession、Destination、credentialだけを操作できるよう認可する
- Start / StopをIdempotency-Key付きで受け付ける
- Session状態の正本をpersistent DBへ保存する
- Workflowの内部状態だけを正本にしない
- Secretの平文を通常レスポンスへ返さない

### 5.2 Lifecycle Workflow

- provider token取得
- boot volume作成
- VPS作成
- server status polling
- Node bootstrap待機
- readiness確認
- DNS切替
- READY後の未配信timeout
- LIVE/HOLDING deadlineの監視
- stop / failure時のcompensation

Cloudflare Workflowsを使う場合、外部API呼び出しは独立したdurable stepに分割し、再試行しても同じresourceを重複作成しないようprovider request keyとresource tagを使う。

### 5.3 Provider adapter

Control PlaneからConoHa固有APIを直接散在させず、次のinterfaceへ隔離する。

```ts
interface MediaNodeProvider {
  ensureBootVolume(input: EnsureVolumeInput): Promise<ProviderVolume>;
  ensureServer(input: EnsureServerInput): Promise<ProviderServer>;
  getServer(serverId: string): Promise<ProviderServer>;
  listManagedResources(): Promise<ManagedResource[]>;
  deleteServer(serverId: string): Promise<void>;
  deleteVolume(volumeId: string): Promise<void>;
}
```

`ensure*`は同じSession IDで何度呼ばれても既存resourceを返す。

ConoHa VPS v3では、VPS作成前にboot storage用volumeが必要である。cleanupではserverだけでなく、不要になったvolumeも削除する。

### 5.4 DNS adapter

```ts
interface IngestDns {
  activate(hostname: string, ipv4: string): Promise<void>;
  park(hostname: string): Promise<void>;
  resolveFromOutside(hostname: string): Promise<string[]>;
}
```

- recordはDNS onlyとする
- Node READY前にactive IPへ切り替えない
- Session終了後に前回IPを残さない
- `park`の実装は、NXDOMAINのnegative cache、reserved address、provider-controlled sinkの挙動を比較して決める
- 削除時は、まずNodeのingest受付を停止し、DNSをparkし、少なくとも設定TTL分待ってからVPSを削除する

### 5.5 Secret broker

- Destination stream keyを暗号化保存する
- Nodeへは対象SessionのSecretだけを一回限りで配送する
- Secretをenvironment variableやcommand lineへ載せない
- Node上ではtmpfsへ`0600`で保存する
- Session終了時にtmpfsを破棄する
- bootstrap token、Node machine credential、TLS keyを目的別に分離する

### 5.6 Node Agent

- bootstrap tokenを1回だけ交換する。Agentはattempt IDとNode access tokenを生成し、
  同一attemptの再送だけを冪等に回復できる
- Node access tokenの平文はAgentのメモリだけに保持する
- Node ID、Session ID、provider server ID、software versionを登録する
- Session config、credential digest、Destination secret、TLS material、absolute deadlineを取得する
- Docker Composeまたはprocess supervisorを起動する
- MediaMTX / continuityのhealthを確認する
- heartbeatとSession eventを送る
- STOP命令を冪等に処理する
- Control Planeへ到達できなくても絶対期限でメディア処理を停止する

### 5.7 Scheduled Reaper

Workflowとは独立して定期実行する。

- provider上の`irlight-managed=true` resourceを列挙する
- resource metadataのSession IDをDBと照合する
- DBに存在しないresource
- FINISHED / FAILED後も残るresource
- 絶対期限を超えたresource
- heartbeatがなく、graceも超えたresource

を検出し、DNS、server、volume、credentialをcleanupする。

## 6. 固定エンドポイントとcredential model

### 6.1 Hostname

```text
u-<opaque-id>.ingest.<service-domain>
```

要件：

- email、username、連番を含めない
- 96bit以上相当の推測困難なslugを使う
- user ID変更の影響を受けない
- 1 userに原則1 hostname
- 将来region別hostnameへ移行できるfieldを持つ

### 6.2 Stream key

- 192bit以上相当の暗号学的乱数を推奨する
- 平文は初回発行時だけ表示する
- Control PlaneにはSHA-256等のdigestを保存する
- keyを紛失した場合は再表示ではなく再発行する
- 再発行時は旧keyを即時失効する
- 固定keyはNode停止中には受け付ける場所がないため、それ単独ではVPSを起動できない
- active Sessionでは`user credential -> session ingest grant`を作り、Node削除時にgrantを失効する

```text
user_ingest_credential: 長期・固定・ローテーション可能
session_ingest_grant:   Session限定・Node READYから終了まで有効
```

これによりユーザーが入力するkeyは固定のまま、Node側の利用許可はSession単位で失効できる。

### 6.3 SRT

SRT対応はRTMPS lifecycleが安定してから追加する。

- hostnameは同じ固定hostnameを利用可能
- credentialはstreamidとpassphraseの扱いを分離して検証する
- UDP security group、MTU、latency、retransmit、NAT互換性を実測する
- RTMPS keyとSRT passphraseを同一文字列にするかはsecurity reviewで決める

## 7. RTMPS certificate design

RTMPSはCloudflare proxyを通さないため、Node自身が公開CAに信頼された証明書を提示する必要がある。

### 推奨案

- ユーザー固定hostnameごとに証明書を中央発行する
- DNS-01等、Nodeが存在しなくても更新可能な方式を使う
- 証明書と秘密鍵は暗号化保存する
- active SessionのNodeへだけ一時配送する
- Node image、GitHub、Issue、`user-data`へ秘密鍵を含めない
- renewal failureを監視する

### wildcard certificate

wildcard証明書は実装を簡単にするが、1つの秘密鍵流出で全ユーザーhostnameへ影響する。採用する場合は、Phase Bの少数Nodeに限定し、短期ローテーション、配布監査、即時失効手順を用意する。

証明書方式は実装開始前に短いsecurity spikeを行い、採用理由をADRまたは本書へ追記する。

## 8. Session state model

```text
STOPPED
   │ POST prepare
   ▼
PROVISIONING
   │ server created
   ▼
BOOTSTRAPPING
   │ agent registered + media health ok
   ▼
DNS_ACTIVATING
   │ external probe ok
   ▼
READY_WAIT_INGEST
   │ authenticated publisher online
   ▼
LIVE
   │ input lost
   ▼
HOLDING
   ├─ input recovered ───────────────▶ LIVE
   └─ hold deadline exceeded
                 ▼
STOPPING
   │ resource cleanup completed
   ▼
FINISHED

任意状態 ─ fatal / timeout ─▶ FAILED_CLEANUP ─▶ FAILED
```

### State invariants

- 1 userにつきPROVISIONING / READY_WAIT_INGEST / LIVE / HOLDINGは最大1つ
- `READY_WAIT_INGEST`になるまで接続可能と表示しない
- `LIVE`は認証成功だけでなく、映像・音声または許可されたmedia packetの進行を確認してから設定する
- STOPPING以降のreconnectは受け付けない
- cleanup完了前にFINISHEDへしない
- FAILEDでもprovider resourceが残っている場合は`cleanup_pending=true`を保持する

### Timers

| Timer | Start | Default | Action |
| --- | --- | ---: | --- |
| provisioning timeout | prepare accepted | 10分 | FAILED_CLEANUP |
| no-ingest timeout | external READY | 60分 | STOPPING |
| hold timeout | authenticated input loss | 30分 | STOPPING |
| absolute deadline | READYまたはpolicy決定時 | 12時間 | STOPPING |
| heartbeat timeout | last heartbeat | 実測後決定 | unhealthy / cleanup判断 |
| DNS drain | DNS park | TTL以上 | server delete |

テストではclockを注入し、60分・12時間を実時間で待たない。

## 9. User-facing API

既存 #6 のSession API方針に合わせる。

### Prepare / Start

```http
POST /v1/sessions/{sessionId}/prepare
Idempotency-Key: <uuid>
```

レスポンス：

```json
{
  "session_id": "...",
  "status": "PROVISIONING",
  "version": 3,
  "connection_info": {
    "rtmps_server": "rtmps://u-xxxx.ingest.example/live",
    "stream_key_status": "CONFIGURED"
  }
}
```

- 同じIdempotency-Keyは同じ結果を返す
- active Sessionがある場合は新しいVPSを作らず既存Sessionを返す
- entitlementを使う場合はprepare時にreservationし、失敗時に解放する

### Stop

```http
POST /v1/sessions/{sessionId}/stop
Idempotency-Key: <uuid>
```

STOPPING / FINISHEDへ何度送っても安全にする。

### Status

```http
GET /v1/sessions/{sessionId}
GET /v1/sessions/{sessionId}/events
GET /v1/sessions/{sessionId}/events?after_sequence=123&limit=100
```

UIはSSEまたはpollingで状態を更新し、最後に受け取ったsequenceより古いeventで画面を巻き戻さない。`events` は既存互換のため query 省略時に現在保持しているeventをすべて返す。cursor取得では `after_sequence` より大きいsequenceだけを最大 `limit` 件返し、`next_after_sequence` と `has_more` で継続取得する。

Session eventは有界ringなので、古いcursorがすでに保持範囲から脱落している場合は `retention_gap=true` と `earliest_sequence` を返す。UIや集計処理はgapを通常の空pageとみなさず、必要ならSession snapshotを再取得して再同期する。`latest_sequence` は取得時点で保持されている末尾sequenceを示す。

### Credential rotation

```http
POST /v1/users/me/ingest-credential/rotate
```

- active LIVE Session中の扱いは明示する
- Phase Bでは安全のためactive Session中のrotationを拒否するか、旧keyの短いgraceを設ける
- 新keyの平文は1回だけ返す

## 10. Internal API

### Bootstrap exchange

```http
POST /internal/nodes/bootstrap
Authorization: Bearer <one-time-token>
```

Nodeは次を送る。

- provider instance ID
- boot ID
- agent version
- public/private address
- bootstrap attempt ID
- Agentが生成したNode access token（Control Planeはdigestだけを保存）

Control Planeはtoken hash、Session、provider instance ID、期限、未使用状態を検証し、
Node recordとtoken消費を単一のatomic authority writeで確定する。応答喪失後の同一attemptは
同じNodeを返し、identityまたはNode tokenが異なる再利用は409で拒否する。

### Bootstrap response

- Node ID
- Session ID
- absolute deadline
- ingest credential digest
- MediaMTX config fragment
- Destination secret referenceまたは一回限り取得URL
- RTMPS certificate material reference
- expected container image digests
- `node_access_token`（Agentが送った同じ値を確認用に返し、Control PlaneはSHA-256 digestだけ保存）

大きなSecretをresponse logへ残さず、必要なら用途別の一回限りURLで取得する。

### Heartbeat

```http
POST /internal/nodes/{nodeId}/heartbeat
Authorization: Bearer <node-access-token>
```

別Nodeのtoken、bootstrap token、管理tokenではheartbeatできない。Node一覧にも
token digestを返さない。

- status
- media health
- active publisher
- egress connected
- CPU / memory / network
- software version
- last media packet time
- deadline remaining

### Node administration

```http
GET /internal/nodes
POST /internal/nodes/{nodeId}/stop
Authorization: Bearer <node-admin-token>
```

list / stopはNode access tokenとは別の管理Bearerを要求する。本番では
`NODE_INTERNAL_ADMIN_TOKEN_FILE`から読み、公開ネットワークへ露出しない。

### Events

```http
POST /internal/sessions/{sessionId}/events
```

sequenceとevent IDで重複排除する。

## 11. Data model additions

### user_ingest_endpoints

- `user_id`
- `hostname_slug`
- `hostname`
- `credential_digest`
- `credential_version`
- `credential_status`
- `certificate_secret_ref`
- `created_at`
- `rotated_at`

### stream_sessions additions

- `workflow_instance_id`
- `provider`
- `provider_server_id`
- `provider_volume_id`
- `provider_public_ipv4`
- `dns_record_id`
- `ready_at`
- `first_ingest_at`
- `last_ingest_at`
- `hold_deadline_at`
- `absolute_deadline_at`
- `cleanup_started_at`
- `cleanup_completed_at`
- `cleanup_pending`
- `failure_reason`
- `version`

### node_bootstrap_tokens

- `token_digest`
- `session_id`
- `provider_server_id`
- `expires_at`
- `consumed_at`

### provider_resource_events

- `session_id`
- `provider_request_id`
- `resource_type`
- `resource_id`
- `operation`
- `result`
- `occurred_at`

## 12. Provisioning workflow

### Step 1: lock and validate

- user ownership
- entitlement / β allowlist
- Destination readiness
- active Session uniqueness
- account status
- system-wide concurrency limit

DB transaction内でSessionをPROVISIONINGへ遷移し、workflow instance IDを保存する。

### Step 2: provider authentication

ConoHa tokenは発行後24時間で期限切れになる。sleepやretryをまたいでtokenを保存し続けず、provider stepごとに有効性を確認し、必要なら再発行する。

provider credentialは専用API sub-userを使い、必要なCompute / Volume / Network権限へ制限する。

### Step 3: ensure boot volume

- approved imageまたはprebuilt imageからvolume作成
- metadataへSession ID、owner、created_at、absolute deadlineを付与
- 同じSessionのvolumeが存在すれば再利用

### Step 4: ensure server

- target plan
- Tokyo region
- approved security group
- SSH keyまたはadmin access policy
- boot volume
- `user-data`
- resource metadata

`user-data`に入れるもの：

- Control Plane bootstrap URL
- one-time token
- expected Session ID
- agent artifact locationまたは起動command

`user-data`に入れないもの：

- Twitch / YouTube / Kick stream key
- fixed ingest key平文
- RTMPS private key
- provider credential
- long-lived machine credential

### Step 5: wait for server and agent

- provider server status
- public IPv4取得
- Node Agent bootstrap
- software version check
- clock skew check
- media services health

### Step 6: deliver session material

Node AgentがtmpfsへSecretを取得する。

```text
/run/irlight/continuity-secrets/  # Continuityだけがread
  media_input_uri
  media_publish_uri
/run/irlight/relay-secrets/       # Egress Gatewayだけがread
  media_relay_uri
/run/irlight/egress-secrets/      # 外部Destination、Egress Gatewayだけがread
  egress_url
  egress_verified_peer_ip
```

owner、permission、mount先を限定する。内部3 actionは別々のcredentialを使い、
`media_*`はそのcredentialを含む0600 URI fileである。Node access tokenはメモリ保持し、
fileへ保存しない。Docker ComposeではSecret値をenvironmentへ展開せず、file
pathだけ渡す。

### Step 7: media readiness

最低限確認する。

- MediaMTX process healthy
- continuity process healthy
- MediaMTX API / metricsはlocalhostまたはcontainer networkだけ
- RTMPS listenerが公開portで待機
- ingest authが有効
- egress URLが通常ログへ出ない
- standby pipelineが起動可能

### Step 8: DNS activation

- A recordをNode IPv4へ更新
- DNS only
- external resolverで更新確認
- RTMPS TLS handshakeを外部から確認
- SRT有効時はUDP疎通を別試験

外部probe成功後にだけREADY_WAIT_INGESTへ遷移する。

### Step 9: wait for ingest or timeout

- Node heartbeat / eventでauthenticated publisherを待つ
- 60分で未配信ならSTOPPING
- UIからstopされた場合は即座にcleanupへ進む
- Workflowがwait中でもscheduled reaperがabsolute deadlineを監視する

## 13. Cleanup workflow

削除は次の順序を基本とする。

1. SessionをSTOPPINGへCAS更新
2. 新規publishを拒否
3. Continuity Engineが出力をgraceful stop
4. 最終usage / event / redacted logを送信
5. ingest grant、Node credential、bootstrap tokenを失効
6. Node内tmpfs Secretを削除
7. DNSをpark
8. DNS TTL分のdrain
9. provider serverを削除
10. server削除完了を確認
11. boot volumeを削除
12. DNS、provider ID、cleanup結果を記録
13. FINISHEDまたはFAILEDへ遷移

各stepは再実行可能にする。既にresourceがない場合は成功として扱う。

## 14. Failure and compensation matrix

| Failure | Expected behavior |
| --- | --- |
| 起動ボタン連打 | 1つのSession / workflowだけ返す |
| provider token期限切れ | 再認証してstep再試行 |
| volume作成後にserver作成失敗 | volume削除 |
| server作成後にAgent未登録 | timeout後server・volume削除 |
| Agent version不一致 | READYにせずcleanup |
| Secret配送失敗 | READYにせずcleanup |
| DNS更新失敗 | retryし、READYを表示しない |
| DNS更新後にRTMPS probe失敗 | DNS park後cleanup |
| READY後にpublishなし | 60分でcleanup |
| LIVE中に入力断 | ContinuityはHOLDING、30分以内の復帰を待つ |
| Destination auth失敗 | reason表示、短いgrace後cleanup |
| user stop during provisioning | cancellation flagを保存し、作成済みresourceをcleanup |
| Workflow再起動 | durable stateとDBから再開 |
| Workflowが復旧不能 | scheduled reaperがorphan回収 |
| Control Plane一時停止 | 既存mediaはローカルdeadlineまで継続 |
| Node crash | Session FAILED、provider resource cleanup |
| server削除後volume残留 | reaperがvolumeを削除 |
| DNSに旧IPが残る | parkと外部resolve確認までFINISHEDにしない |

## 15. Network exposure

初期security group案：

| Port | Exposure | Purpose |
| --- | --- | --- |
| 443/TCP | Internet | RTMPS ingest |
| 8890/UDP | Internet when enabled | SRT ingest |
| 1935/TCP | Disabled by default | plain RTMP compatibility only |
| 22/TCP | admin IP / VPN only or disabled | emergency access |
| Control API | not public | Node Agent local/internal |
| MediaMTX API / metrics | not public | local monitoring only |
| HLS / RTSP preview | not public | diagnostics only |

Phase 0の`127.0.0.1` bindをそのまま公開用設定へ流用せず、公開portと内部portを明示的に分離したproduction composeを作る。

## 16. Security controls

### Web/control plane

- Cloudflare Access JWTをAPI側で検証
- user ownership、Session version、CSRFを検証
- Start / Stopのrate limit
- management action audit
- provider credentialをbrowserへ返さない

### Provisioning

- provider API sub-user
- role最小化
- one-time bootstrap token
- token有効期限とprovider instance binding
- resource metadataで所有権を識別
- arbitrary user-dataを受け付けない

### Media node

- non-root container
- read-only filesystem
- tmpfs Secret
- `no-new-privileges`
- capability削減
- CPU / memory / process limit
- image digest pinning
- Docker socketをControl UI/APIへ渡さない

### Ingest

- high-entropy credential
- digest comparison
- 1 credential 1 publisher
- bitrate / duration / connection attempt limit
- oversized metadataや対応外codecの拒否
- inactive user / revoked keyの拒否

### Egress

- Phase B初期は承認済みplatform templateのみ
- Custom URLを無効化
- Secret file delivery
- URL redaction
- Session終了時削除

## 17. Observability

### Metrics

- `session_prepare_duration_seconds`
- `provider_volume_create_duration_seconds`
- `provider_server_create_duration_seconds`
- `node_bootstrap_duration_seconds`
- `dns_activation_duration_seconds`
- `external_probe_duration_seconds`
- `ready_without_ingest_seconds`
- `session_live_seconds`
- `session_holding_seconds`
- `provider_cleanup_duration_seconds`
- `provider_orphan_resources`
- `provider_api_errors_total`
- `workflow_retries_total`
- `node_heartbeat_age_seconds`
- `ingress_bytes` / `egress_bytes`
- CPU / memory / network / process restart

### Events

- `session.prepare_requested`
- `provider.volume_created`
- `provider.server_created`
- `node.bootstrap_completed`
- `dns.activated`
- `session.ready`
- `ingest.connected`
- `session.live`
- `session.holding`
- `session.stop_requested`
- `provider.cleanup_started`
- `provider.cleanup_completed`
- `provider.orphan_detected`
- `session.failed`

各eventはSession ID、user ID、provider resource ID、workflow ID、correlation IDを持つ。Secretは含めない。

### Alerts

- cleanup失敗
- orphan resource検出
- prepare timeout増加
- provider API認証失敗
- DNS update失敗
- certificate expiry
- Node heartbeat timeout
- egress auth failure
- active Node数が全体上限へ到達

## 18. Cost accounting

Sessionごとに次を記録する。

- provider server created / deleted timestamp
- volume created / deleted timestamp
- plan / region
- billable provider hoursまたはprovider明細との照合key
- ingress / egress bytes
- LIVE / HOLDING / READY_WAIT_INGEST seconds
- failed provisioning seconds
- cleanup lag

価格表の値をコードへ固定せず、provider price snapshotと実請求を照合する。停止だけで課金が止まると仮定せず、resource削除完了を計測する。

## 19. Test strategy

リポジトリ規則に従い、通常のDocker runtime検証は10分程度を基本とする。長時間timeoutはfake clockまたは短縮設定で検証する。

### Unit

- state transition
- deadline calculation
- idempotency
- duplicate event
- cleanup order
- provider adapter response mapping
- DNS park/activate logic
- credential digest / rotation

### Integration with fake provider

- volume成功 / server失敗
- Agent timeout
- DNS failure
- double prepare
- stop during provisioning
- Workflow retry
- reaper cleanup

### ConoHa sandbox/internal alpha

- create volume -> server -> bootstrap -> delete server -> delete volume
- resource listに残骸がない
- no-ingest timeoutを短縮して自動削除
- abrupt Node failure後のreaper
- provider token再発行
- DNS切替と外部resolve
- 2GB候補で720p30 / 6Mbpsの10分試験
- CPU、CPU steal、memory、frame drop、A/V sync、egress stability
- 不足時だけ4GBで再試験

### Security

- invalid Access JWT
- cross-user prepare / stop
- revoked ingest key
- double publisher
- bootstrap token再利用
- bootstrap tokenとprovider instance不一致
- Secretがlogs / docker inspect / process argsへ出ない
- MediaMTX API / metricsが外部から到達しない
- expired certificate / wrong hostname

### External destination

限定公開・テスト用stream keyだけを使う。

- Twitch
- YouTube
- Kick

実施していないdestinationを確認済みと記録しない。

## 20. Implementation slices

### Slice A: provider CLI spike

- ConoHa API client
- volume/server create
- metadata tag
- server/volume delete
- resource inventory
- secretsはlocal environmentのみ
- 生成resourceを必ずcleanupするdry run / confirmation

### Slice B: immutable Node bootstrap

- prebuilt imageまたはpinned container image
- Node Agent bootstrap
- one-time token
- tmpfs Secret
- health / heartbeat
- production compose

### Slice C: lifecycle state and workflow

- Session state migration
- prepare / stop API
- provider adapter
- durable workflow
- compensation
- scheduled reaper

### Slice D: stable hostname and RTMPS

- user opaque hostname
- DNS adapter
- certificate issuance / renewal
- MediaMTX auth
- fixed credential and rotation
- external probe

### Slice E: user UI

- リレーを起動
- 準備中のphase表示
- 配信可能
- 未配信削除までの残り
- LIVE / HOLDING
- 配信終了
- failure action

### Slice F: SRT

RTMPSでlifecycle、cleanup、costが安定した後に追加する。

## 21. Phase B acceptance criteria

- UIのprepareを1回押すとVPSが1台だけ作成される
- 二重クリックやAPI再試行で二重作成されない
- ユーザーの固定hostnameと固定keyでpublishできる
- Node IPが変わってもユーザー設定を変更しなくてよい
- READY前はUIが配信可能と表示しない
- READY後1時間未配信でNode、volume、DNS binding、grantがcleanupされる
- LIVE後の入力断で最大30分HOLDINGし、同じkeyで復帰できる
- explicit stopで出力を閉じ、provider resourceを削除できる
- absolute deadlineでNodeが停止し、外部reaperが最終削除できる
- provider上にorphan server / volumeが残らないことを試験で確認する
- provider、DNS、Node、Sessionを同じcorrelation IDで追跡できる
- stream key、Destination secret、TLS keyが通常ログ、Issue、process引数へ出ない
- 2GB候補の採否を対象VPSの実測で決定する

## 22. Open decisions

実装前または各sliceで決める。

1. 実際のingest service domain
2. Control Plane DBの具体製品
3. Cloudflare Workflows採用と現行料金の許容範囲
4. ConoHa plan ID、image、security group、region endpoint
5. prebuilt imageとcontainer pullのどちらを起動標準にするか
6. RTMPS証明書をper-hostにするかwildcardにするか
7. DNS park方式
8. Phase Bの全体同時Node上限
9. heartbeat timeout
10. Destination auth失敗時のcleanup grace
11. SRTをPhase B前半へ含めるか後半へ分けるか
12. fixed key rotation中のactive Sessionをどう扱うか

## 23. Official references

- ConoHa VPS v3 サーバー作成: https://doc.conoha.jp/reference/api-vps3/api-compute-vps3/compute-create_vm-v3/
- ConoHa VPS v3 APIによるVPS作成例: https://doc.conoha.jp/reference/api-vps3/api-utilization-vps3/api-create_vm-v3/
- ConoHa VPS v3 トークン発行: https://doc.conoha.jp/reference/api-vps3/api-identity-vps3/identity-post_tokens-v3/
- ConoHa VPS v3 Compute API index: https://doc.conoha.jp/reference/api-vps3/api-compute-vps3/
- ConoHa VPS v3 Volume削除: https://doc.conoha.jp/api-vps3/volume-delete_vol-v3/
- Cloudflare Workflows: https://developers.cloudflare.com/workflows/
- Cloudflare Workflows sleep / retry: https://developers.cloudflare.com/workflows/build/sleeping-and-retrying/
- Cloudflare Workflows pricing: https://developers.cloudflare.com/workflows/reference/pricing/
- Cloudflare Workflows limits: https://developers.cloudflare.com/workflows/reference/limits/
- Cloudflare Access JWT validation: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
