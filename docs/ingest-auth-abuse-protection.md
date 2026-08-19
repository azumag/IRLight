# Ingest auth abuse protection

IRLight の MediaMTX external auth hook (`POST /internal/ingest/auth`) は、RTMP / RTMPS / SRT の publish 認証失敗を短時間の failure window で追跡し、繰り返し失敗する送信元を一時的にロックします。

## 判定単位

失敗は次の2つのbucketへ同時に記録します。

- source IP 単位: 多数のsession / credentialを試す credential spray を抑止する
- credential/session username 単位: IPを変えながら同一stream keyを総当たりする試行を抑止する

既定値は以下です。

- failure window: 60秒
- credential単位: 8回でlock
- IP単位: 20回でlock
- lockout: 120秒
- lock中の `ingest.auth_blocked` audit: 同一bucketにつき5秒間隔
- audit event: 最新200件
- bucket: 最大4096件

環境変数で変更できます。

```text
IRLIGHT_INGEST_AUTH_GUARD_ENABLED=1
IRLIGHT_INGEST_AUTH_FAILURE_WINDOW_SECONDS=60
IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_CREDENTIAL=8
IRLIGHT_INGEST_AUTH_MAX_FAILURES_PER_IP=20
IRLIGHT_INGEST_AUTH_LOCKOUT_SECONDS=120
IRLIGHT_INGEST_AUTH_BLOCKED_EVENT_INTERVAL_SECONDS=5
IRLIGHT_INGEST_AUTH_EVENT_LIMIT=200
IRLIGHT_INGEST_AUTH_BUCKET_LIMIT=4096
```

## 応答

通常のcredential失敗は従来どおり `401 invalid ingest credential` を返します。閾値に到達した試行とlock中の試行は `429 ingest authentication temporarily blocked` と `Retry-After` を返します。

unknown user、wrong secret、expired/revoked credential、終了済みSessionは認証可否の詳細を外部へ返さず、同じcredential failureとして扱います。unsupported action / path / protocol は設定・呼び出し契約の問題として `403` のままで、credential失敗bucketには加算しません。

成功した認証ではcredential/session側の失敗bucketを解除します。source IP側の失敗数は解除しません。これは、同一IPから多数のcredentialを試すsprayを、途中で1回だけ正しいcredentialを通してリセットする回避を防ぐためです。

lockout期限が満了したbucketは過去の失敗windowもリセットされ、通常状態から再開します。

## 永続化と秘密情報

stateは `STATE_DIR/ingest_auth_guard.json` にatomic replaceで保存します。

bucket keyにはsource IPやusernameの平文を使わず、SHA-256 digestを利用します。audit eventには運用上必要なsource IP、protocol、publisher ID、UUIDとして解釈できる場合のみsession ID、username fingerprintを保存します。

次の値はauth guardへ渡さず、stateにも保存しません。

- publisher password / stream key
- token
- query string
- user agent

監査eventは以下を記録します。

- `ingest.auth_failed`
- `ingest.auth_locked`
- `ingest.auth_blocked`

lock成立後のリクエストは毎回メモリ上で即座にlock判定され、HTTP 429を返します。一方、`ingest.auth_blocked` の永続監査は既定5秒間隔で間引きます。これにより、lock後の大量リクエストをJSONのatomic write / `fsync` 増幅に利用されにくくします。

これらは現時点ではauth guard state内のbounded auditです。Node bootstrapと実Session assignmentの統合後、正式なSession event streamへ接続する作業は別途行います。

## ネットワーク境界

`/internal/ingest/auth` はMediaMTXからのみ到達可能なinternal endpointとして扱う前提です。request bodyの `ip` はMediaMTXが報告するpublisher source IPを信頼しています。このendpoint自体をインターネットへ直接公開しないでください。

このrate limitはcredential brute force / spray対策であり、L3/L4 DDoS対策の代替ではありません。公開Media Nodeではクラウド側firewall、connection limit、必要に応じたedge側防御を併用します。

## テスト

`tests/test_ingest_auth_guard.py` でcredential/IP双方のlock、期限切れ復帰、成功時リセット、lock中audit書込みのthrottle、秘密情報非保存、bounded auditを検証します。

`scripts/smoke-ingest-auth-abuse.sh` はControl APIをDockerで起動し、閾値を3回へ下げた上で `401 -> 401 -> 429 -> 429` と `Retry-After`、audit stateへのsecret非保存を確認します。
