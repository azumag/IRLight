# Destination platform model

Issue #5 のMVPでは、配信先のサービス種別とtransport protocolを分離して扱う。

## Fields

- `platform`: `twitch | youtube | kick | custom`
- `type`: `rtmp | rtmps | srt`

既存実装では `type` がtransport protocolとして広く使われているため、この意味は変更しない。サービス種別は新しい `platform` fieldで表現する。

既存catalog recordに `platform` が無い場合は、後方互換のため `custom` として扱う。

## Allowed combinations

| platform | rtmp | rtmps | srt |
| --- | --- | --- | --- |
| twitch | yes | yes | no |
| youtube | yes | yes | no |
| kick | yes | yes | no |
| custom | yes | yes | yes |

Twitch / YouTube / Kick のMVP送出はユーザーが取得したRTMP/RTMPS endpointとstream keyを使う。プラットフォームAPIによるbroadcast作成、タイトル変更、カテゴリ変更等はこのmodelの責務外。

## API

新規Destination:

```json
{
  "platform": "twitch",
  "type": "rtmps",
  "display_name": "Main Twitch",
  "server_url": "rtmps://example.invalid/app",
  "secret_ref": "destination/main-twitch"
}
```

`platform`を省略した場合は `custom`。

既存Destinationのplatformは `PUT /v1/destinations/{id}` で更新できる。ただし、現在のprotocolと組み合わせられないplatformへの変更（例: `custom + srt` から `twitch`）は `422` で拒否する。

## Verification and secret handling

`platform`はサービス分類であり、Destination verificationとEgress Gatewayのtransport選択は引き続き `type` と `server_url` schemeを使う。

- `server_url`のtransport handshake / SSRF / DNS guardは既存仕様を維持
- stream keyは `secret_ref` の暗号化secret storeで管理
- catalog / Session / Node stateへplaintext secretを保存しない
