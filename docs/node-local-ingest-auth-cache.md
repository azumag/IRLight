# Node-local ingest auth cache

IRLight の production Media Node では、MediaMTX の HTTP authentication を Control Planeへ直接送らず、Node Agent container内の local auth proxy (`http://node-agent:8090/auth`) へ送ります。

通常時、proxyはすべての認証要求をControl Planeの `/internal/ingest/auth` へ転送します。Control Planeが明示的に許可したcredentialだけを短時間のpositive cacheへ記録します。cacheはControl Planeとの通信障害または5xx時に限ってfallbackに使います。

## 目的

Control Planeが短時間再起動したりネットワーク経路が一時断になっても、直前まで正常に使えていたpublisherが再接続できるようにします。

これはControl Planeから独立した恒久認証ではありません。新しいcredentialや、一度も正常認証されていないcredentialはControl Plane障害中には許可しません。

## 通常時と障害時

1. MediaMTXがNode Agent proxyへ認証POSTを送る
2. proxyがControl Planeへ同じ認証要求を転送する
3. Control Planeが2xxで `authorized=true` を返した場合のみpositive cacheを更新する
4. 4xx（401 / 403 / 429を含む）は明示的な拒否として、そのcredentialのcacheを削除してそのままMediaMTXへ返す
5. upstream transport failureまたは5xxの場合だけpositive cacheを検索する
6. 有効なcache entryがあれば2xxを返し、なければ503でfail closedする

MediaMTXはHTTP authの20x応答を認証成功、それ以外を失敗として扱うため、cache fallbackも同じHTTP境界を維持します。

## cache keyと秘密情報

cache keyは以下だけをcanonicalizeしてSHA-256 digestにします。

- action
- path
- protocol
- user
- password
- token

publisherのIPやMediaMTX connection IDはkeyに含めません。モバイル回線切替などでIPが変わった再接続でも、同じcredentialなら障害中fallbackできるためです。

一方でraw password / token / usernameはcache entryとして保存しません。cacheはNode Agent processのメモリ内だけに存在し、ファイルやtmpfsにも永続化しません。Node Agent再起動時にはcacheは空になります。

## stale allow window

Control Planeの成功応答には `cache_valid_until` を含めます。これは以下の短い方です。

- credential自身の `expires_at`
- Control Planeの `IRLIGHT_INGEST_AUTH_CACHE_MAX_AGE_SECONDS`（既定300秒）

Node Agentも `NODE_INGEST_AUTH_CACHE_MAX_AGE_SECONDS`（既定300秒）でさらに上限をかけます。

したがってControl Plane停止中にcredentialが別経路でrevokeされた場合、最悪でもこのbounded stale windowを超えて許可されません。Control Planeが到達可能な状態でrevoke後のauthが1回でも行われれば、明示401によってNode cacheは即時削除されます。

## 設定

Node側:

```text
NODE_INGEST_AUTH_URL=http://node-agent:8090/auth
NODE_INGEST_AUTH_PROXY_ENABLED=1
NODE_INGEST_AUTH_LISTEN_HOST=0.0.0.0
NODE_INGEST_AUTH_LISTEN_PORT=8090
NODE_INGEST_AUTH_UPSTREAM_URL=
NODE_INGEST_AUTH_UPSTREAM_TIMEOUT_SECONDS=2
NODE_INGEST_AUTH_CACHE_MAX_AGE_SECONDS=300
NODE_INGEST_AUTH_CACHE_MAX_ENTRIES=256
```

`NODE_INGEST_AUTH_UPSTREAM_URL` が空の場合は `NODE_CONTROL_PLANE_URL + /internal/ingest/auth` を使います。

Control Plane側:

```text
IRLIGHT_INGEST_AUTH_CACHE_MAX_AGE_SECONDS=300
```

## 失効戦略

- credential rotation: 古いcredentialはControl Planeで401になり、Node cacheから削除
- explicit revoke: 次のControl Plane到達可能なauthで401になり、Node cacheから削除
- Session stop: credential store側でrevokeされるため同様に401→cache削除
- Control Plane障害中のrevoke: 最大stale allow windowまでは既存positive cacheが残り得る
- credential expiry: `cache_valid_until` がcredential expiryを超えないため、期限後fallback不可

将来的にNode assignment/event channelが確立したら、revoke通知をNodeへpushしてstale windowをさらに短縮できます。このPRではControl Plane障害とrevoke通知経路の同時喪失を考慮し、TTLを最終防衛線にします。

## テスト

- `tests/test_ingest_auth_proxy.py`
  - successでcache prime
  - upstream 5xxで同credentialをfallback許可
  - connection ID / source IP変更後も再接続可能
  - wrong secretはfallback不可
  - explicit 401 / 429でcache eviction
  - local TTL / entry count上限
  - cache内部にraw username / passwordを保持しないこと
- `scripts/smoke-ingest-auth-cache.sh`
  - 実MediaMTX経由で認証済みRTMP publisherを接続してcache prime
  - Control Plane containerを停止
  - 同credentialでRTMP再接続できること
  - Control Plane復旧後にcredentialをrevokeし、明示401でcacheを削除
  - 再度Control Planeを停止してもrevoked credentialがfallbackしないこと
