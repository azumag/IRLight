# Destination verification

`POST /v1/destinations/{id}/verify` は、Destination の `server_url` に対して実際の transport handshake を行い、到達できた場合だけ `verification_status=VERIFIED` を保存する。

## プロトコル別の確認範囲

- RTMP: TCP 接続後、RTMP v3 の C0/C1 → S0/S1/S2 → C2 handshake まで行う。media publish は行わない。
- RTMPS: TLS の証明書検証と hostname 検証を通した後、RTMP v3 handshake まで行う。
- SRT: `srt-live-transmit` を caller mode で起動し、libSRT による実 SRT connection handshake を行う。単なる UDP port 到達性では VERIFIED にしない。

この段階の VERIFIED は **transport endpoint が実プロトコルで応答したこと**を表す。`secret_ref` の secret resolver はまだ接続されていないため、Twitch / YouTube 等への stream key を使った実 publish 認証までは確認しない。

## セキュリティ

Destination URL はユーザー入力なので、verify は既定で public IP に解決される宛先だけを許可する。loopback / private / link-local / reserved 等への接続を拒否し、Control Plane を SSRF に利用できないようにする。

DNS は接続前に解決・検査し、RTMP/RTMPS は検査済み sockaddr へ直接接続する。SRT は検査済み IP literal に URL を書き換えて `srt-live-transmit` へ渡し、検査後の再 DNS lookup を避ける。

ローカル PoC や self-hosted 構成で private destination が必要な場合のみ、次を明示する。

```text
IRLIGHT_VERIFY_ALLOW_PRIVATE_TARGETS=1
```

`docker-compose.poc.yml` はローカル smoke 用としてこの設定を有効にしている。本番では有効にしない。

URL の userinfo (`user:password@host`) と SRT の `passphrase` query は拒否する。secret は `server_url` に埋め込まず `secret_ref` 側で管理する。

## タイムアウト

既定の probe timeout は 5 秒。以下で 0.5〜30 秒の範囲に変更できる。

```text
IRLIGHT_VERIFY_TIMEOUT_SECONDS=5
```

この値は RTMP/RTMPS の各 socket 操作ごとにリセットされる timeout ではなく、probe 開始時に作る単一の単調時計 deadline として扱う。DNS 解決から戻った時点で消費済み時間を差し引き、複数 A/AAAA 候補の connect、TLS handshake、RTMP send/recv は同じ残り予算を共有する。slow-drip peer が短い recv を繰り返しても deadline を延長しない。SRT も DNS 解決後の残り予算を `srt-live-transmit` の `conntimeo` と接続イベント待ちに引き継ぐ。

ただし Python の同期 `socket.getaddrinfo()` 自体は呼び出し途中で安全にキャンセルできないため、**DNS resolver が返らないケースの hard wall-clock 上限はまだ未実装**。現在の deadline は resolver が戻った後に超過を検出して外向き接続を開始しないところまでを保証する。DNS を含む完全な総時間上限、SRT stderr/reader の容量上限、probe admission control は Issue #91 の後続実装で扱う。

SRT 子 process の終了回収には handshake deadline とは別に最大 1 秒単位の cleanup 猶予を使う。cleanup 猶予を handshake 成功判定の追加時間として利用しない。

## 状態更新

成功時:

- `verification_status=VERIFIED`
- `last_verified_at` を更新
- `verification_transport` に protocol / peer IP / peer port / elapsed time を保存
- `last_verification_error` をクリア

失敗時:

- `verification_status=FAILED`
- `last_verification_error` に secret を含まない短い理由を保存
- `verification_transport` をクリア

`server_url` が変更された場合は、以前の検証結果を流用せず `UNVERIFIED` に戻す。
