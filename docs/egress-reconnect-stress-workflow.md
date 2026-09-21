# Egress reconnect stress workflow

Issue #545 の intermittent な legacy `rtmpsink` outage-detection failure を、GitHub-hosted runner 上で必要なときだけ再現するための手動 workflow です。

## 実行方法

GitHub Actions の `Egress reconnect stress` を `workflow_dispatch` から明示的に起動します。

- `scenario`: `reconnect` / `stop-terminal` / `both`
- `runs`: 選択した scenario ごとの反復回数。GitHub Actions では `1` / `3` / `5` の固定選択とし、既定は `3`

実処理は `scripts/stress-egress-reconnect.sh` へ委譲します。そこから既存の `smoke-egress-reconnect.sh` / `smoke-egress-stop-terminal.sh` を呼ぶため、#546〜#549 で整備した secret-safe evidence、redacted failure diagnostics、SIGUSR2 thread-stack diagnostics の境界を再利用します。

## 安全性とコスト境界

この workflow は `pull_request` や `schedule` trigger を持たず、通常 CI からは実行されません。`permissions` は `contents: read` のみで、repository secret や実配信先 credential を要求しません。既存 smoke のローカル Docker target だけを使用し、Twitch / YouTube / Kick 等の外部サービスへ接続しません。

ローカル helper 自体は最大50回まで許可しますが、GitHub Actions 側では runner 使用量を診断目的で不用意に増やさないため最大5回に制限します。同一 stress workflow の並行実行も concurrency group で直列化し、自動キャンセルによる中途半端な診断結果も避けます。

この workflow の追加は production runtime、45秒の `RECONNECTING` contract、retry / stall policy、status vocabulary を変更しません。再発を捕捉して原因を分類するための operator 明示実行経路だけを追加します。
