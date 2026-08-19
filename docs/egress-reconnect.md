# Egress publish / reconnect

Issue #5 の次の縦切りとして、外部RTMP / RTMPS Destinationへのpublishと再接続をContinuity Engineから分離します。

## Architecture

```text
publisher
   |
   v
MediaMTX live/input
   |
   v
Continuity Engine
   |
   | RTMP (always local)
   v
MediaMTX output/relay
   |
   | internal RTSP
   v
Egress Gateway
   |
   | RTMP / RTMPS + Destination secret
   v
Twitch / YouTube / Kick / Custom
```

Continuity Engineは外部Destinationへ直接接続しません。外部RTMP障害、DNS障害、TLS障害、stream key拒否が起きてもContinuityのoutput pipelineを止めず、`output/relay` を維持します。

Egress Gatewayは `rtsp://mediamtx:8554/output/relay` を読み、H.264 / AACをdepay / parseしてFLVへ再muxし、外部RTMP / RTMPSへ送ります。この段階では再エンコードしません。

## Secret boundary

外部のcredentialed URLはNode bootstrap時にだけ配送され、Node Agentがtmpfsの `/run/irlight/secrets/egress_url` へ作成時点から0600で保存します。

- Continuity: secret volumeをmountしない
- Egress Gateway: secret volumeをread-only mount
- Node Agent: secretを作成し、media stack停止後に削除
- `egress.json`: stream key / credentialed URLを含めない
- Node / Session event: scheme / host / status / reason codeのみ

## Runtime destination DNS guard

Destination verification時だけでなく、Egress Gatewayは**各publish / reconnect attemptの直前**にも実際のhostnameをDNS解決し直します。

既定では解決結果の全addressがpublic/globalであることを要求します。1件でもprivate、loopback、link-local、unspecified、multicast等が混じる場合は接続を開始しません。これによりCustom URLをControl Plane内部やcloud metadata endpointへ向けるSSRFをfail closedにします。

- DNS lookup自体の一時失敗: `DNS_FAILED` としてretry
- unsafe address: `DESTINATION_UNSAFE` としてterminal
- `EGRESS_VERIFIED_PEER_IP_FILE` が存在する場合、verify済みpeer IPが現在のDNS answer setから消えていれば `DESTINATION_DNS_CHANGED` としてterminal
- peer metadataが不正なら `DESTINATION_GUARD_INVALID` としてterminal
- `EGRESS_ALLOW_PRIVATE_TARGETS=1` はlocal/self-hosted用途の明示overrideで、production既定は`0`

`type=rtmp` / `server_url=rtmps://...` のようなDestination typeとURL scheme不一致はverification前に拒否し、probe結果のprotocolもtypeと一致することを必須にします。

### DNS rebindingの残余リスク

現在のGStreamer `rtmpsink` / librtmpはguard後に自身でもhostnameを解決します。そのため「guardのDNS lookup」と「sink内部のDNS lookup」の極短い間にanswerを切り替える理論上のTOCTOU windowは残ります。

この実装は各attempt直前の再検証とoptionalなverify済みpeer IP照合でpersistentなDNS driftを検知しますが、完全なtransport-IP pinningではありません。完全に塞ぐには、validated IPへsocketを固定しつつRTMPSのTLS SNI / hostname verificationには元hostnameを使えるconnector/proxy、または同等のsink実装が必要です。

## Connection detection

`rtmpsink` は `GstBaseSink` 派生です。Gatewayはsinkの `stats.rendered` を短周期で確認し、1 buffer以上がrenderされた時点を `CONNECTED` とします。

単にTCP socketが開いたことではなく、FLV bufferがsinkまで到達したことを接続成立の観測点にします。

接続待ちは `EGRESS_CONNECT_TIMEOUT_SECONDS`（既定15秒）で打ち切り、`TIMEOUT` としてretryへ移ります。

## Status

`EGRESS_STATUS_FILE`（productionは `/state/egress.json`）へ次を保存します。

- `STARTING`
- `CONNECTED`
- `RECONNECTING`
- `AUTH_FAILED`
- `FAILED`
- `STOPPED`

安全な付加情報:

- `attempt`
- `reason_code`
- `rendered_buffers`
- `next_retry_at`
- `destination_scheme`
- `destination_host`
- `observed_at`

raw GStreamer / librtmp error messageはstream keyを含む可能性があるため永続化しません。運用向けにはerror domain/codeだけを保持します。

## Reconnect

一時障害は指数バックオフ + jitterで新しいGStreamer pipelineを作り直します。

既定値:

```text
EGRESS_RETRY_INITIAL_SECONDS=1
EGRESS_RETRY_MAX_SECONDS=30
EGRESS_RETRY_MULTIPLIER=2
EGRESS_RETRY_JITTER_RATIO=0.2
EGRESS_MAX_ATTEMPTS=0
EGRESS_MAX_RETRY_SECONDS=0
```

`0` のretry limitは無制限です。最大経過時間は「最後に正常接続してからの累積稼働時間」ではなく、現在の連続outage episodeだけを計測します。正常接続した時点でfailure countとoutage timerをresetします。

## Failure classification

初期reason code:

- `AUTH_FAILED`: credential拒否。terminal、無限retryしない
- `PUBLISH_CONFLICT`: publish name / key競合。terminal、無限retryしない
- `TLS_FAILED`: TLS / certificate failure
- `DNS_FAILED`: hostname解決失敗
- `DESTINATION_UNSAFE`: runtime DNSがnon-public addressを含む。terminal
- `DESTINATION_DNS_CHANGED`: verify済みpeer IPがruntime DNSから消失。terminal
- `DESTINATION_GUARD_INVALID`: runtime guard metadata不正。terminal
- `TIMEOUT`: 接続待ち / transport timeout
- `UNREACHABLE`: connection refused / route / reset等
- `UPSTREAM_UNAVAILABLE`: 内部 `output/relay` を読めない
- `UPSTREAM_EOS`: 内部relayがEOS
- `EGRESS_PIPELINE_FAILED`: 分類できないtransport/pipeline error
- `LOCAL_PIPELINE_FAILED`: 必須pluginやlink失敗。terminal
- `RETRY_EXHAUSTED`: configured retry limit到達
- `SECRET_UNAVAILABLE`: credential file不在/不正

GStreamer / librtmpのerror stringはplatformによって差があるため、Twitch / YouTube / Kick実接続でreason分類を実測し、必要ならsignatureを追加します。

## Session events

Node Agentはsafeな `egress.json` をheartbeatへ含め、Control Planeは変化時に次をSession event streamへ追記します。

- `egress.starting`
- `egress.connected`
- `egress.disconnected`
- `egress.reconnecting`
- `egress.recovered`
- `egress.auth_failed`
- `egress.failed`
- `egress.stopped`

出力障害だけではSessionの `LIVE / HOLDING` を変更しません。これはingest continuityとegress reachabilityを別軸として扱うためです。

## Local E2E

`scripts/smoke-egress-reconnect.sh` は2台目のMediaMTXを外部Destination代替として起動します。

このtargetはisolated Compose networkのprivate addressを使うため、smoke overrideだけ `EGRESS_ALLOW_PRIVATE_TARGETS=1` を明示します。production defaultはfail closedのままです。

1. Continuityが内部 `output/relay` へstandby映像/無音を継続publish
2. Egress Gatewayが別MediaMTXへpublishし `CONNECTED`
3. target MediaMTXを停止
4. Gatewayが `RECONNECTING`
5. Continuity containerが引き続きrunningであることを確認
6. targetを再起動
7. Gatewayが `CONNECTED` に復帰し、target pathがreadyになることを確認
8. status / logsにstream keyが出ていないことを確認

## Remaining platform validation

この縦切りのlocal E2EはRTMP transport / reconnectを検証します。Issue #5 完了には引き続き以下が必要です。

- Twitch実publish
- YouTube実publish
- Custom RTMP実publish
- Kick実publish（MVP対象）
- 各platformでinvalid stream key / duplicate publish等の実reason確認
- RTMPS実Destinationでcertificate validationを含むpublish確認
- 完全なDNS transport-IP pinningが必要なら専用connector/proxyを実装
