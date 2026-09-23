# Legacy egress process-isolation Docker canary

Issue #586 の Stage 3 として、legacy `rtmpsink` の process-isolation canary を実際の Docker Compose 構成で検証する。

## reconnect / fencing 契約

`scripts/smoke-egress-isolated-reconnect.sh` は `EGRESS_LEGACY_PROCESS_ISOLATION_CANARY=1` を egress-gateway にだけ設定し、production default や `rtmp2sink` の選択は変更しない。外部配信先や実 credential は使用せず、isolated Compose network 上の MediaMTX を destination として使う。

正常接続後に attempt child の PID と `/proc/<pid>/stat` の start time を取得する。PID 単体では急速な再利用を誤判定し得るため、同一 process identity は `pid:start_time` で判定する。child の command line / environment に、生成した destination URL または stream key が含まれないことも container 内で確認し、秘密値自体は CI 出力へ出さない。

次に destination を停止し、既存の 45 秒契約を延長せず `RECONNECTING` を待つ。`RECONNECTING` を観測した時点で、以下を必須とする。

- egress-gateway container が同一 ID のまま running である。
- outage 前の attempt process identity が `/proc` 上に残っていない。
- Continuity が running のままである。

その後 destination を再開し、同じ親 egress-gateway container のまま `CONNECTED` へ復帰すること、新しい attempt process identity が生成されていること、新しい child の command line / environment にも secret が出ていないことを確認する。

## user-stop 契約

`scripts/smoke-egress-isolated-stop-terminal.sh` は process-isolation canary を有効にした legacy `rtmpsink` が `CONNECTED` の状態で明示的に Gateway を停止し、active child を bounded に停止・reap してから `STOPPED / USER_STOPPED` へ収束することを検証する。

この smoke では isolation の teardown / terminate / kill timeout を各 1 秒に固定し、Docker の外側の stop grace を 8 秒にする。Gateway が child fence を完了できず Docker に SIGKILL された場合は、container exit code 0 と最終 `STOPPED / USER_STOPPED` の両方を満たせないため fail-closed になる。停止後も Continuity が生存して final status を観測でき、`restart: "no"` により online の destination が Gateway を勝手に再起動しないことも確認する。

user-stop smoke でも接続中 child の command line / environment に generated destination URL / stream key が存在しないことを確認し、秘密値そのものはログへ出さない。

## CI

`.github/workflows/egress-process-isolation-canary.yml` は egress-gateway、PoC Compose、または process-isolation smoke に関係する pull request でのみ実行する。reconnect と user-stop を独立 job として実行し、それぞれ 10 分で hard timeout、workflow job 自体も 12 分で上限を持つ。

この workflow は shared Docker suite の置き換えではない。通常の repository CI、Dependency audit、shared Docker suite、measured-soak chain は従来どおり別 gate として扱う。

## 残る Stage 3

Docker E2E で未固定なのは terminal `AUTH_FAILED` / `PUBLISH_CONFLICT` の process-isolation canary semantics である。`rtmp2sink` path は既存 shared smoke で非回帰を維持し、production default への切替は terminal canary evidence も揃った後の別判断とする。
