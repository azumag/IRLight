# Egress reconnect smoke diagnostics

`scripts/smoke-egress-reconnect.sh` は、外部 RTMP destination の停止・復旧時に Egress Gateway が再接続し、Continuity が停止しないことを確認する local E2E smoke です。

## Secret redaction check

smoke は生成した stream key が次の観測面へ露出していないことを検証します。

- `/state/egress.json`
- `egress-gateway` の Compose logs

ログ検査では `docker compose logs | grep -q ...` のような pipeline を使いません。`set -o pipefail` 下では `grep -q` が一致時に早期終了すると upstream が SIGPIPE になり、実際には secret が含まれていても pipeline 全体が non-zero となって「一致なし」と誤判定できるためです。

代わりに、Compose logs を `umask 077` が有効な run-local temporary file へ最後まで取得してから固定文字列検索します。ログ取得自体が失敗した場合は redaction 成功とは扱わず、fail closed で smoke を失敗させます。temporary file は既存 cleanup で run 終了時に削除します。

failure diagnostics 自体も secret の観測面になり得るため、失敗時に収集する Continuity / Egress Gateway / target の logs は generated stream key を固定文字列で `<redacted>` に置換してから CI へ出します。status timeout も同じ redactor を通し、target path timeout では stream key を含み得る Control API snapshot をそのまま出力しません。redactor が失敗した場合に raw diagnostics へフォールバックする挙動は持たせません。

## RECONNECTING timeout evidence

外部 target 停止後に `RECONNECTING` を観測できず timeout した場合は、通常の redacted log に加えて `IRLIGHT_EGRESS_RECONNECT_EVIDENCE` という1行の secret-safe evidence を stderr と GitHub Step Summary に残します。Step Summary は shared Docker suite の failure artifact に保存されるため、raw Compose logs を artifact 化せずに intermittent failure を分類できます。

公開する値は allowlist 済みの status / reason code、非負整数として検証した attempt と `rendered_buffers`、boolean に正規化した connected / `next_retry_at` の有無、`egress-gateway` container が running かどうか、status の `observed_at` から計算した bounded な経過秒数だけです。`observed_at` 自体は公開せず、経過秒数は非有限値・未来値を拒否したうえで 0〜999 秒へ制限します。これにより、target 停止後も status heartbeat / legacy rendered counter が進んでいるのか、最後の status 更新自体が長時間止まっているのかを secret-safe evidence だけで切り分けやすくします。

status JSON 自体は Python helper の stdin から読み、destination URL / host / path、generated stream key、raw timestamp、raw JSON、raw container logs は evidence line に含めません。不正 JSON や想定外値は raw 値を反射せず `UNREADABLE` / `OTHER` / `-` に縮退します。

この evidence は timeout を延長したり `CONNECTED -> RECONNECTING -> CONNECTED` の契約を緩めるものではありません。target 停止後に gateway が `CONNECTED` のまま固着したのか、status/progress が更新中なのか停止しているのか、terminal/unknown state に遷移したのか、gateway process 自体が停止したのかを切り分けるための診断面だけを追加します。

## Failure stages

CI の Docker smoke suite は static stage annotation を artifact に残します。secret redaction 関連は次の stage を使います。

- `secret-redaction-status`: status JSON に stream key が含まれていた
- `secret-redaction-logs-read`: Egress Gateway logs を完全に取得できなかった
- `secret-redaction-logs`: 取得済み logs に stream key が含まれていた

stage token に secret 値や外部入力は含めません。再接続 timeout、retry policy、production configuration はこの診断契約の対象外です。

## Stop / terminal smoke の failure stages

`scripts/smoke-egress-stop-terminal.sh` も同じ annotation 契約を使います。explicit stop と terminal unsafe-destination のどこで最初に失敗したかを、Docker smoke artifact の `Stage` から判別できます。

`reconnecting` timeout では、通常の reconnect smoke と同じ secret-safe 境界で `IRLIGHT_EGRESS_STOP_TERMINAL_RECONNECT_EVIDENCE` を stderr と GitHub Step Summary に残します。公開するのは allowlist 済み status/reason、検証済み非負 attempt、connected、`next_retry_at` の有無、gateway liveness だけです。`gateway` は `running` / `not-running` に正規化し、helper に想定外値が渡った場合も `unknown` に縮退させます。raw status JSON は helper の stdin から読み、generated stream key、credentialed URL、raw status、raw logs を marker に含めません。parse failure や未知値は raw 値へフォールバックせず `UNREADABLE` / `OTHER` / `-` に fail closed します。この診断追加でも既存の45秒 `RECONNECTING` contract は延長しません。

主な stage は次のとおりです。

- `initial-connected`: 初期 `CONNECTED` へ到達しなかった
- `reconnecting`: target 停止後に `RECONNECTING` へ到達しなかった
- `backoff-window`: explicit stop と競合させるための long backoff 条件を満たさなかった
- `stopped-user-stopped`: gateway stop 後の `STOPPED / USER_STOPPED` 契約を満たさなかった
- `continuity-survives-stop`: egress stop/reconnect race 中に Continuity が停止した
- `target-recovery-no-restart`: user stop 後の target 復旧で gateway が再起動した
- `unsafe-destination-terminal`: unsafe destination が terminal exit contract を満たさなかった
- `unsafe-destination-failed` / `unsafe-destination-reason`: `FAILED / DESTINATION_UNSAFE` 契約を満たさなかった
- `secret-redaction-terminal-output`: terminal guard output に generated secret が露出した
- `secret-redaction-logs-read`: Egress Gateway logs を完全に取得できず、secret 不在を証明できなかった
- `secret-redaction-logs`: 取得済み logs に generated secret が露出した

Stop / terminal smoke の失敗時 diagnostics も、run-local private file に一度収集した後、初期 stream key と unsafe-destination 用の generated secret の両方を `<redacted>` に置換してから CI へ出します。status timeout や unexpected terminal exit の payload も同じ redactor を通し、redactor または log read が失敗した場合に raw payload へフォールバックしません。

stage token は hard-coded ASCII token に限定し、secret や外部入力を workflow annotation へ反射しません。既存の retry、timeout、status、reason-code assertion は診断追加のために緩和しません。

## Intermittent failure の手動 stress runner

Issue #545 のような intermittent な legacy `rtmpsink` outage-detection failure を再現するときは、`scripts/stress-egress-reconnect.sh` で既存 smoke を繰り返せます。この helper 自体は新しい Docker/credential 経路を持たず、`smoke-egress-reconnect.sh` と `smoke-egress-stop-terminal.sh` を順番に呼ぶだけなので、既存の secret redaction、bounded evidence、SIGUSR2 thread-stack diagnostics をそのまま利用します。

```bash
# reconnect と stop-terminal をそれぞれ10回（既定）
scripts/stress-egress-reconnect.sh

# reconnect だけを20回
IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO=reconnect \
IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS=20 \
  scripts/stress-egress-reconnect.sh
```

`IRLIGHT_EGRESS_RECONNECT_STRESS_RUNS` は 1〜50 に制限し、`IRLIGHT_EGRESS_RECONNECT_STRESS_SCENARIO` は `reconnect` / `stop-terminal` / `both` のみ受理します。各 run は sequential に実行し、最初の failure で停止して `scenario` と iteration だけを固定形式で出します。runner 自身は destination URL、stream key、raw status/log を新たに保存・反射しません。

これは再現・診断用の **手動 helper** であり、通常の `ci-docker-smoke-suite.sh` には組み込みません。intermittent failure を追うためだけに全 PR の Docker 利用量を増やさず、必要なときに operator が明示実行します。45秒 `RECONNECTING` contract、production runtime、retry/stall policy を変更するものでもありません。
