# Authentication attempt rate admission

Issue #86 のうち、`POST /v1/auth/register` と `POST /v1/auth/login` が受け付ける**試行の頻度**を Control Plane runtime 内で bounded にする運用契約です。`auth-kdf-admission` が「同時に何件の password KDF を走らせるか」を bound するのに対し、この gate は「同じ client / account が一定時間に何回試行できるか」を bound します。

## 何を制限するか

試行ごとに key を作り、2 つの fixed window を両方とも通過したときだけ PBKDF2 へ進みます。

- burst window: 既定 **10 回 / 60 秒**
- sustained window: 既定 **30 回 / 600 秒**

key は「信頼できる client アドレス + 正規化 email」です。正規化は store と同じ `strip().lower()` で、未知 email も既知 email も同じ境界を通ります。したがって 429 の応答から account の存在は判別できません。

| 環境変数 | 既定値 | 範囲 |
| --- | --- | --- |
| `IRLIGHT_AUTH_RATE_LIMIT_BURST_LIMIT` | `10` | `1..10000` |
| `IRLIGHT_AUTH_RATE_LIMIT_BURST_WINDOW_SECONDS` | `60` | `1..86400` |
| `IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_LIMIT` | `30` | `1..10000` |
| `IRLIGHT_AUTH_RATE_LIMIT_SUSTAINED_WINDOW_SECONDS` | `600` | `1..86400` |
| `IRLIGHT_AUTH_RATE_LIMIT_DIR` | `/tmp/irlight-auth-rate-limit` | 絶対 path |

未設定・不正値・範囲外はすべて既定値へ戻ります。上限は mock せず実測で決めるべき値なので、この既定値は abuse と誤検知のどちらにも倒れすぎない保守的な出発点として扱い、実 traffic を見て調整してください。

## API の過負荷契約

window が尽きた場合、PBKDF2・KDF admission slot・Session 発行のいずれも開始せず HTTP 429 を返します。

```json
{"code":"AUTH_RATE_LIMITED"}
```

この応答には `Retry-After`（秒、1 以上）を付けます。値は「まだ塞がっている window が明けるまで」の最大秒数です。rate limit は KDF admission slot より前に評価するため、拒否された試行が password-compute capacity を占有することはありません。

共有 state を安全に使用できない場合も同様に PBKDF2 を開始せず、HTTP 503 で fail closed します。

```json
{"code":"AUTH_RATE_LIMIT_UNAVAILABLE"}
```

これは authority corruption の `AUTH_STATE_UNAVAILABLE`、compute 飽和の `AUTH_COMPUTE_BUSY` / `AUTH_COMPUTE_UNAVAILABLE` と別の stable code です。

## client アドレスの確定

制限 key に使うアドレスは `X-Forwarded-For` を無条件に採用しません。既定では **どの proxy も信頼せず**、ASGI が報告する直接の peer アドレスだけを使います。reverse proxy 配下で運用する場合のみ、`IRLIGHT_TRUSTED_PROXIES` に proxy の exact アドレスをカンマ区切りで設定します。

```
IRLIGHT_TRUSTED_PROXIES=10.20.0.5,10.20.0.6
```

- 直の peer が trusted proxy のときだけ header を参照し、右から左へ走査して最初の非 trusted hop を client とします。
- `X-Forwarded-For` が複数 field で届いた場合、hop 数が 20 を超える場合、非 IP 値が混じる場合、2 KiB を超える場合は header 全体を無視して peer に戻ります（部分的に信頼しない）。
- CIDR 表記は未対応で、無効な entry は黙って無視します。proxy を追加したら必ず明示してください。

## 共有 state と有界性

- counter は `IRLIGHT_AUTH_RATE_LIMIT_DIR` 配下の shard file（既定 16 個、`shard-N.json`）に保持し、各 shard は `flock` で排他します。同じ runtime filesystem を使う worker 全体で 1 つの上限になります（`IRLIGHT_AUTH_KDF_ADMISSION_DIR` と同じ前提）。
- shard file には「正規化 email と client アドレスを random secret で HMAC した key（128 bit に切詰め）」しか保存しません。raw email、password、client アドレス、user ID、session token、CSRF token は残りません。
- secret は同 directory の `rate-limit.secret`（`0600`、owner-only）に初回だけ atomic に作成し、worker 間で共有します。directory と shard は symlink / 非 regular file を拒否し、実効 UID 所有 + group / other 非公開を要求します。
- 1 shard が保持する key 数には上限（既定 4096）があります。上限に達した状態で新しい key が来た場合は、表を無限に伸ばさず、既存 key が明けるまでの `Retry-After` を返します。飽和しても恒久的な lockout にはなりません。
- 期限切れ window は次の試行時にまとめて捨てられるため、通常運転でファイルは有界です。

破損時の扱いは意図的に分けています。admission **境界**（directory / shard の所有者・permission・symlink、secret が読めない、clock が非有限値）は fail closed ですが、counter **内容**が壊れている場合はその shard を空として window をやり直します。counter は authority ではなく一時的な admission state であり、壊れた runtime が認証全体の停止に増幅しない方が安全なためです。

## lockout は自動で解除される

window は固定長で、key ごとに明けたら自動で 0 に戻ります。解除操作・管理 API・手動 unlock は不要で、第三者が正規ユーザーを恒久的に締め出す経路はありません（既定値での最大待ちは sustained window の 10 分）。

## 運用上の境界

この変更だけで次の問題は解決しません。

- account 単位の恒久的な lockout / captcha / 二段階の追加検証
- 複数 host / container replica をまたぐ cluster-wide quota（独立 replica がそれぞれ既定 directory を使う場合、上限は replica ごと）
- user 単位の active auth Session 上限
- 拒否件数・処理時間のメトリクス / alert（合否は HTTP status と stable code で観測）

## 確認ポイント

過負荷調査では HTTP status、`code`、`Retry-After`、configured window を先に確認し、password・email・token をログへ追加して調査しないでください。`AUTH_RATE_LIMITED` が継続する場合は request rate と KDF admission の飽和状況を別々に観測し、上限を上げる前に abuse か定常 traffic かを切り分けます。`AUTH_RATE_LIMIT_UNAVAILABLE` が継続する場合は `IRLIGHT_AUTH_RATE_LIMIT_DIR` の所有者・permission・mount 状態を確認します。
