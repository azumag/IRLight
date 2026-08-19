# Egress Destination secret delivery

Issue #5 の最初の縦切りとして、Destinationのstream keyをControl Planeで暗号化保存し、選択されたSessionのMedia Nodeへbootstrap時だけ配送します。

この段階ではRTMP / RTMPS Destinationのsecret配送までを対象とします。実際の外部サービスへのpublish成功判定、egress reconnect/backoff、AUTH_FAILED / UNREACHABLE分類は次の縦切りで実装します。

## Destinationとsecret

Destination catalogには平文stream keyを保存しません。

```text
Destination
  id
  user_id
  type: rtmp | rtmps
  server_url
  secret_ref
  enabled
  verification_status
```

平文secretは次のAPIから設定します。

```text
PUT /v1/destinations/{destination_id}/secret
{ "value": "<stream-key>" }
```

レスポンスは `secret_ref / configured / timestamp` のみで、入力した値をechoしません。

削除:

```text
DELETE /v1/destinations/{destination_id}/secret
```

Destination本体の削除ではsecretを自動削除しません。同じuserが同一`secret_ref`を複数Destinationで共有している可能性があるためです。secretを不要にする場合は明示的にsecret DELETEを実行します。

## 暗号化at rest

`STATE_DIR/destination_secrets.json` にはFernetのauthenticated ciphertextだけを保存します。

record keyは `SHA-256(user_id + NUL + secret_ref)` です。同一の`secret_ref`でもuserが異なれば別recordになります。

productionではmaster keyをファイルsecretとしてmountします。

```text
IRLIGHT_SECRET_MASTER_KEY_FILE=/run/secrets/destination_master_key
```

Fernet keyの生成例:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

`IRLIGHT_SECRET_MASTER_KEY` で直接指定するfallbackもありますが、development専用です。productionではcontainer environment / compose config / inspectへ鍵を残さないためFILE形式を使います。

## Master key rotation

現在のspikeは複数master keyや自動re-encryptionを実装していません。

master keyを単純に差し替えると既存ciphertextを復号できなくなるため、production運用前に以下のいずれかを実装する必要があります。

- key version付きenvelope encryption
- old/new keyを使った明示re-encryption migration
- 外部secret managerへの移行

このPRでは「一つのmaster keyでauthenticated encryptionする」境界までとし、master-key rotationはfollow-up対象です。

## Session prepare

`POST /v1/sessions/{session_id}/prepare` に `destination_id` を渡せます。

```json
{
  "environment": "prod",
  "destination_id": "..."
}
```

prepare前に次を検証します。

- DestinationがSession userの所有物
- `enabled=true`
- typeがRTMPまたはRTMPS
- `verification_status=VERIFIED`
- `secret_ref` に暗号化secretが設定済み

実ConoHa providerではDestination指定を既定必須にします。fake/PoCでは既存のローカルoutputを維持するため未指定を許可します。

```text
IRLIGHT_REQUIRE_DESTINATION=1
```

Destination検証はentitlement reservation / provider allocationより前に行うため、secret未設定等の入力エラーでVPSを作成しません。

## server_urlとstream key

一般的な「server URL + stream key」形式を次のルールで一つのpublish URLへ変換します。

`server_url` に `{stream_key}` がある場合:

```text
rtmps://live.example/app/{stream_key}
```

その場所へURL encodeしたstream keyを展開します。

placeholderがない場合:

```text
rtmp://live.example/app
```

path末尾へstream keyを1 segmentとして追加します。

query stringがある場合もstream keyはqueryより前のpathへ追加します。

URL内のusername/passwordとfragmentは拒否します。秘密情報をcatalog URLへ埋め込まないためです。

## Node bootstrap delivery

Node bootstrap時、Control Planeは `provider_server_id` からユーザーSessionを解決し、そのSessionの`destination_id`を取得します。

1. owned Destinationを再取得
2. enabled / VERIFIEDを再確認
3. `secret_ref`からciphertextを復号
4. credentialed RTMP/RTMPS URLをメモリ上で組み立て
5. bootstrap responseの `egress_url` に一度だけ含める
6. Node Agentが即座にsecret fileへ書く

Node Agentの既存動作により、配送されたURLは `NODE_SECRET_DIR/egress_url` にmode `0600` で保存されます。production composeでは`NODE_SECRET_DIR`はtmpfs volumeです。Continuity containerは`EGRESS_URL_FILE`としてこのfileを読むため、credentialed URLをenvironmentやprocess argumentへ置きません。

## 永続化しない場所

credentialed egress URL / raw stream keyは以下へ保存しません。

- catalog.json
- sessions.json
- nodes.json
- Session events
- Node events
- normal API responses

Node stateにはsafeな`destination_id`だけを保存します。

bootstrap responseにはNodeへ配送するためcredentialed URLが含まれるため、internal bootstrap endpointのrequest/response bodyを平文ログへ記録しないことが前提です。現在のControl API access logはbodyを記録しません。

## Failure behavior

次の場合はNode bootstrapを失敗させ、one-time bootstrap tokenを消費する前に止めます。

- Destinationが削除済み
- disabled
- verificationが失効/FAILED
- secretがない
- master keyがない/不正
- ciphertextが復号不能
- egress URLがunsupported

これにより、egress credentialを持たないNodeが起動して課金時間だけ消費する状態を避けます。

## Test coverage

`tests/test_destination_secrets.py`:

- plaintext非永続化
- user isolation
- wrong master keyで復号不可
- explicit delete
- URL placeholder / path append
- SRT egressは現時点で拒否

`tests/test_session_destination.py`:

- prepareのowner/enabled/VERIFIED/type/secret検証
- ConoHaのDestination必須化
- bootstrap egress URL解決

`scripts/smoke-egress-secret-delivery.sh`:

1. Destinationを作成・verify
2. secret未設定prepareが409でprovider allocation前に失敗
3. secret APIでstream keyを暗号化保存
4. state fileに平文がないことを確認
5. Destination付きSessionをprepare
6. 実fake provider server IDでNodeをbootstrap
7. Node secret fileが期待URL・mode0600であることを確認
8. catalog/session/node/secret JSONにraw key・credentialed URLがないことを確認
