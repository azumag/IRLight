# Egress reconnect smoke diagnostics

`scripts/smoke-egress-reconnect.sh` は、外部 RTMP destination の停止・復旧時に Egress Gateway が再接続し、Continuity が停止しないことを確認する local E2E smoke です。

## Secret redaction check

smoke は生成した stream key が次の観測面へ露出していないことを検証します。

- `/state/egress.json`
- `egress-gateway` の Compose logs

ログ検査では `docker compose logs | grep -q ...` のような pipeline を使いません。`set -o pipefail` 下では `grep -q` が一致時に早期終了すると upstream が SIGPIPE になり、実際には secret が含まれていても pipeline 全体が non-zero となって「一致なし」と誤判定できるためです。

代わりに、Compose logs を `umask 077` が有効な run-local temporary file へ最後まで取得してから固定文字列検索します。ログ取得自体が失敗した場合は redaction 成功とは扱わず、fail closed で smoke を失敗させます。temporary file は既存 cleanup で run 終了時に削除します。

## Failure stages

CI の Docker smoke suite は static stage annotation を artifact に残します。secret redaction 関連は次の stage を使います。

- `secret-redaction-status`: status JSON に stream key が含まれていた
- `secret-redaction-logs-read`: Egress Gateway logs を完全に取得できなかった
- `secret-redaction-logs`: 取得済み logs に stream key が含まれていた

stage token に secret 値や外部入力は含めません。再接続 timeout、retry policy、production configuration はこの診断契約の対象外です。
