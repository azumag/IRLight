# Authentication password-KDF admission

Issue #86 のうち、registration / login が行う PBKDF2-HMAC-SHA256 の**同時実行数**を Control Plane runtime 内で bounded にする運用契約です。これは CPU を使う password KDF の暴走を抑えるための admission gate であり、IP / email 単位の rate limit やアカウント lockout ではありません。

## 何を制限するか

`POST /v1/auth/register` と `POST /v1/auth/login` は password KDF を始める前に non-blocking admission slot を取得します。既定では同じ admission filesystem を共有する worker 全体で最大 **4** 件の KDF-bearing request を同時に通します。

- `IRLIGHT_AUTH_KDF_MAX_CONCURRENT`: `1..32`。未設定、不正値、範囲外は既定 `4`。
- `IRLIGHT_AUTH_KDF_ADMISSION_DIR`: slot file を置く絶対 path。未設定または相対 path は `/tmp/irlight-auth-kdf-admission`。

slot は `flock` で保持するため worker process が異常終了しても kernel が lock を解放します。slot file 名は固定の `slot-N.lock` で、email、password、user ID、session token、request body などは保存しません。directory と slot file は symlink / non-regular-file を拒否し、実効 UID の所有物かつ group / other から書込み・探索できない owner-only permission であることを要求します。安全に admission boundary を構成できない場合は fail closed します。

## API の過負荷契約

全 slot が使用中なら PBKDF2 を開始せず HTTP 503 と次の stable detail を返します。

```json
{"code":"AUTH_COMPUTE_BUSY"}
```

この応答には `Retry-After: 1` を付けます。known / unknown email とも password verification より前の同じ admission boundary を通るため、slot 飽和時の public response から account existence を区別しません。

admission directory / slot を安全に使用できない場合も PBKDF2 を開始せず HTTP 503 で fail closed します。

```json
{"code":"AUTH_COMPUTE_UNAVAILABLE"}
```

これは authority corruption の `AUTH_STATE_UNAVAILABLE` と分けます。compute admission 障害を state file 障害として誤診しないためです。

## Lock scope

login は `authenticate_user()` の password verification の間だけ slot を保持し、認証成功後の auth Session 発行では slot を解放します。これにより Session authority write を password-compute capacity に含めません。

register は現行 store が hash generation と authority write を一つの `register_user()` operation として公開しているため、その call の間 slot を保持します。PBKDF2 自体は従来どおり auth authority file lock の外側で実行されるため、KDF 待ちが state lock を長時間占有する挙動には変更しません。

## 運用上の境界

この変更だけで次の問題は解決しません。

- source IP / normalized email / account 単位の頻度制限
- trusted proxy 経由で client IP を確定するポリシー
- 複数 host / container replica をまたぐ cluster-wide quota
- ユーザー単位の active auth Session 上限

独立した host / container がそれぞれ既定 directory を使う場合、上限は replica ごとに適用されます。cluster-wide な lock filesystem を採用する場合は、その filesystem の可用性・`flock` semantics・failure mode に加え、すべての worker が同じ実効 UID / owner-only permission 契約を満たせることを deployment 設計として確認してください。外部 datastore や有料サービスをこの gate が自動的に追加することはありません。

## 確認ポイント

過負荷調査時は、まず HTTP status / stable code と configured concurrency を確認し、password、email、token をログへ追加して原因調査しないでください。`AUTH_COMPUTE_BUSY` が継続する場合は request rate と Control Plane CPU saturation を別途観測し、上限値を単に引き上げる前に abuse / capacity のどちらが原因かを判断します。

本変更は Issue #86 の「concurrent KDF work を bounded にする」slice のみです。rate-limit key、閾値、window、per-user Session policy は実測・trusted-proxy policy・運用要件なしに決めません。
