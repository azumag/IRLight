# ADR 0002: Phase BはUIトリガーのオンデマンド1 Session = 1 VPSを採用する

- Status: Accepted
- Date: 2026-08-17
- Scope: Phase B（招待制クローズドβ）
- Related: #8, #9, #11, #12, #14, #20
- Detail design: `docs/architecture/on-demand-media-node.md`

## Context

IRLightの主対象は、月1回程度利用する個人・小規模イベント配信者である。Phase Bでは利用者数と同時配信数が少なく、Media Nodeを常時稼働させると、実際に配信していない時間にも固定費が発生する。

現在のPhase 0 PoCは、MediaMTX、GStreamer Continuity Engine、Control UIを1台へ配置する単一Session構成である。この構成は機能成立性の検証には適しているが、認証、TLS、複数Session、Secret配送、Node lifecycleは未実装であり、そのまま公開しない。

予約システムは利用者と運営者の双方に運用負荷がある。一方、VPSが存在しない状態ではRTMP/RTMPS/SRTの接続着信を検知できないため、OBS等からpublishしただけでNodeを起動するには常設Ingest Gatewayが必要になる。常設Gatewayは固定費、追加遅延、追加帯域、単一障害点、不正起動対策を増やすため、Phase Bでは採用しない。将来候補は #20 で扱う。

## Decision

Phase Bでは、ユーザーがWeb UIの「リレーを起動」を明示的に押した時だけ、一時Media Nodeを作成する。

### 基本単位

- `1 Session = 1 VPS`
- 初期providerは、東京リージョンの時間課金ConoHa VPSを第一候補とする
- 初期サイズは2GB / 3vCPU相当を候補とするが、対象VPS上の実測で確定する
- CPU、frame drop、A/V sync、外部配信安定性が不足する場合は4GB / 4vCPU相当へ上げる
- Node capacityや原価をローカルDockerの数値だけで断定しない
- Media Nodeは停止ではなく、Session終了後にサーバーと不要なboot volumeを削除する

provider固有処理はadapterへ隔離し、将来さくらのVPS、GCP、AWS等へ差し替え可能にする。

### 起動方式

1. 認証済みユーザーがUIで「リレーを起動」を押す
2. Control Planeがユーザー権限、同時Session上限、Destination、利用上限を検査する
3. 同じユーザーの起動要求を冪等化し、既存のPROVISIONING / READY / LIVE Sessionがあればそれを返す
4. lifecycle workflowがprovider APIでboot volumeとVPSを作成する
5. `user-data`には短時間有効な一回限りbootstrap tokenだけを渡す
6. Node AgentがControl Planeへ登録し、Session限定設定とSecretを取得する
7. MediaMTX、Continuity Engine、監視を起動して内部health checkを通す
8. ユーザー固有の固定ホスト名を新しいNode IPへ向ける
9. 外部からRTMPS/SRT listenerとNode Agentを確認してからUIをREADYにする

ConoHa API tokenは長時間保持せず、provider APIを呼ぶ各段階で必要に応じて再認証する。

### 固定接続情報

ユーザーには、原則として毎回同じ接続情報を提示する。

```text
RTMPS endpoint: rtmps://u-<opaque-id>.ingest.<service-domain>/live
Stream key:     <high-entropy opaque credential>
```

- ホスト名はメールアドレスや連番を含めないopaque IDとする
- NodeのIPアドレスは毎回変わってよい
- DNS recordだけをSessionごとのNodeへ切り替える
- stream keyは通常固定とするが、ユーザー操作または管理者操作で即時再発行できる
- keyは十分なentropyを持つランダム値とし、Control Planeにはdigestを保存する
- keyの平文をログ、Issue、metrics、例外、process引数へ出さない
- Nodeには対象ユーザーとSessionに必要な検証情報だけを配送する

固定キーであっても永久資格情報とは扱わず、アカウント停止、漏えい、再発行時に失効させる。

### Session lifecycleと期限

既定値は次とする。値は設定可能にし、βの実測で調整する。

| 条件 | 既定動作 |
| --- | --- |
| Node READY後に認証済みpublisherが来ない | 60分で終了・削除 |
| 一度LIVEになった後に入力が切れる | 30分までHOLDING |
| HOLDING中に同じ資格情報で復帰 | LIVEへ自動復帰 |
| HOLDING期限超過 | 出力を安全に終了してNode削除 |
| ユーザーが「配信終了」を押す | graceful stop後に削除 |
| 絶対最大稼働時間 | 12時間で強制終了 |
| provisioning timeout | cleanupを実施してFAILED |

未配信60分の計測開始点は、起動ボタン押下時ではなく、Nodeが外部接続可能なREADYになった時点とする。

### 外部watchdog

Node自身だけに削除を任せない。

- lifecycle workflowがSessionの絶対期限を保持する
- 独立したscheduled reaperがprovider上の管理対象Nodeを列挙する
- DB上のactive Sessionと一致しないNode、期限切れNode、長時間heartbeatがないNodeをorphan候補として検出する
- 削除は冪等にし、server、port、boot volume、DNS、Session credentialを順にcleanupする
- Control Planeまたはworkflowが一時停止しても、Nodeは署名済みの絶対期限をローカルで保持し、期限後にメディア処理を停止する
- 外部watchdog復旧後に最終的なprovider resource削除を行う

### Control Plane

Phase BのControl Planeは常設VPSを前提とせず、Cloudflare Accessで保護したWeb UI/APIと、Cloudflare Workers / Workflows等のserverless orchestrationを第一候補とする。

- Accessを通しただけで認可済みとはみなさず、origin/API側でもAccess JWTの署名、issuer、audienceを検証する
- user IDとSession ownershipをAPIで検証する
- workflowはprovider API呼び出し、poll、sleep、retry、cleanupをdurable stepとして実行する
- Workflowsの料金と制限は実装時の現行仕様を確認し、実利用を計測する
- provider credential、DNS API token、暗号鍵はsecretとして管理する

### Phase Bの初期制限

- 最大1入力・1出力
- 720p30を標準
- 最大6Mbps程度
- `AUDIO_PROCESSED`を本番候補とするが、対象VPSで再測定する
- 1080p30は限定試験後に解放する
- Custom RTMP/RTMPS出力は初期無効とし、Twitch、YouTube、Kick等の承認済みtemplateを優先する
- SRTはRTMPSのlifecycleが安定した後に追加可能とする
- SLAは設けず、単一Node障害では配信枠を維持できないことを明記する

## DNSとTLSの方針

- ingest用DNS recordはCloudflare proxyを通さないDNS onlyとする
- TTLは可能な最小値を使い、READY表示前に外部名前解決と接続を確認する
- STOP時はDNSを前回Nodeから外してからprovider resourceを削除する
- 前回IPを削除後もDNSへ残さない
- RTMPSの公開証明書と秘密鍵をbase imageやリポジトリへ埋め込まない
- Phase Bではユーザー固有ホスト名の証明書を中央で発行・更新し、対象SessionのNodeへ一時配送する方式を優先する
- wildcard証明書を使う場合は秘密鍵のblast radiusを明示し、配布先とローテーションを制限する

証明書発行・更新方式は実装前のsecurity spikeで確定する。

## Consequences

### Positive

- 配信していない時間のMedia Node固定費をほぼなくせる
- 現行の単一Session PoCを、VPS単位の障害・Secret分離として再利用できる
- 予約システムを作らずに利用できる
- ユーザーはOBS等の接続情報を毎回変更しなくてよい
- Session単位のprovider原価、起動時間、CPU、転送量を計測しやすい
- 同時配信時は必要な数だけNodeを追加できる
- Node削除によりSession間の残留状態を減らせる

### Negative

- ユーザーはpublish前にUIで起動操作を行う必要がある
- cold startとDNS切替の待ち時間が発生する
- Control Plane、provider API、DNS APIの複数systemにまたがる補償処理が必要になる
- Node READY前にpublishしても接続できない
- RTMPS証明書の安全な事前発行・配送が必要になる
- workflowやcleanupの不具合で課金resourceが残る可能性があるため、独立reaperが必須になる
- 配信中のNode障害に対する透過フェイルオーバーは提供しない

## Rejected / Deferred alternatives

### Media Node常時稼働

実装は簡単だが、Phase Bの低利用率では固定費の比率が高いため不採用とする。月間稼働時間が時間課金と月額契約の分岐を超えた場合に再評価する。

### 予約制

capacity確保には有効だが、初期ユーザー体験と運用負荷を増やすため採用しない。provider capacity不足が実際に発生した場合に、開始時刻指定ではなく同時起動上限や待ち行列から検討する。

### publish着信による自動起動

常設Gatewayが必要であり、固定費、追加遅延、帯域、DDoS、不正課金対策を増やすためPhase Bでは採用しない。実利用でUI起動が主要な離脱要因になった場合に #20 を検討する。

### 停止中VPSを残して電源だけOFF

provider resourceの課金停止を保証できず、Nodeとboot volumeが残留するため採用しない。Session終了時は不要resourceを削除する。

## Follow-up

- #8: provider adapter、Node Agent、bootstrap、heartbeat、reaperを実装する
- #9: 「リレーを起動」「準備中」「配信可能」「配信終了」のUIを追加する
- #11: orphan cleanup、provider障害、DNS障害、Node障害のrunbookとalertを作る
- #12: provider credential、destination secret、bootstrap token、RTMPS証明書を安全に扱う
- #14: 起動時間、未配信削除率、1Session原価、サポート負荷をβで収集する
- #20: UI操作なしの常設Ingest Gatewayを将来検討する

## Reference

- ConoHa VPS API v3 サーバー作成: https://doc.conoha.jp/reference/api-vps3/api-compute-vps3/compute-create_vm-v3/
- ConoHa VPS API v3 トークン発行: https://doc.conoha.jp/reference/api-vps3/api-identity-vps3/identity-post_tokens-v3/
- ConoHa VPS API v3 APIによるVPS作成例: https://doc.conoha.jp/reference/api-vps3/api-utilization-vps3/api-create_vm-v3/
- Cloudflare Workflows: https://developers.cloudflare.com/workflows/
- Cloudflare Workflows sleeping and retrying: https://developers.cloudflare.com/workflows/build/sleeping-and-retrying/
- Cloudflare Access JWT validation: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
