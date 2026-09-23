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

## auth-terminal 契約

`scripts/smoke-egress-isolated-auth-terminal.sh` は local MediaMTX に generated username/password の publish auth を設定し、Gateway には意図的に異なる generated password を渡す。外部 credential は使わない。

legacy `rtmpsink` / librtmp が server の auth rejection text を保持した場合は `AUTH_FAILED / AUTH_FAILED`、pre-connect RTMP rejection を `Gst.ResourceError.WRITE` に畳んだ場合は既存 policy に従って `FAILED / PUBLISH_REJECTED` を受理する。どちらの場合も terminal であることが契約であり、Gateway は retry せず exit code 2 で終了し、`next_retry_at` を持たない final status を維持しなければならない。

smoke は configured retry delay より長い観測窓を置き、Gateway が再起動・再試行しないことと Continuity が生存することを確認する。生成した username/password/stream name が egress-gateway logs に現れないことも fail-closed で検査する。

## publish-conflict 契約

`scripts/smoke-egress-isolated-publish-conflict.sh` は local MediaMTX の同一 path を先行 publisher で保持し、その後 process-isolation canary を有効にした legacy `rtmpsink` Gateway を同じ path へ接続する。先行 publisher は `rtmp2sink` を使う独立 service とし、Gateway の child lifecycle と混同しない。

MediaMTX の target log で second publisher が同一 path の publisher collision として拒否されたことを直接確認する。legacy librtmp が rejection detail を保持した場合は `AUTH_FAILED / PUBLISH_CONFLICT`、detail を `Gst.ResourceError.WRITE` に畳んだ場合は `FAILED / PUBLISH_REJECTED` を受理する。いずれも terminal result であり、親 Gateway は exit code 2 で終了し `next_retry_at` を持たず、先行 publisher と Continuity は生存しなければならない。

smoke は configured retry delay より長く final state を再観測し、attempt 番号が変わらず Gateway が再起動しないことを要求する。Gateway container の immutable config から legacy `rtmpsink` と explicit process-isolation canary flag の両方を確認し、unit contract の `legacy_isolation_enabled()` と組み合わせて canary 選択境界を固定する。generated stream key が final status / Gateway log に現れないことも fail-closed で検査する。target の raw conflict log は artifact へ保存せず、failure diagnostics でも generated value を redaction してから出力する。

## CI

`.github/workflows/egress-process-isolation-canary.yml` は egress-gateway、PoC Compose、または process-isolation smoke に関係する pull request でのみ実行する。reconnect、user-stop、auth-terminal、publish-conflict を独立 job として実行し、それぞれ 10 分で hard timeout、workflow job 自体も 12 分で上限を持つ。

この workflow は shared Docker suite の置き換えではない。通常の repository CI、Dependency audit、shared Docker suite、measured-soak chain は従来どおり別 gate として扱う。

## Stage 3 完了後の境界

reconnect/fencing、user-stop、auth rejection、publisher collision の Docker evidence が揃えば、Issue #586 の process-isolation canary Stage 3 は完了とみなせる。`rtmp2sink` path は既存 shared smoke で非回帰を維持する。

ただし、これらの証拠だけで production default を自動的に切り替えない。legacy process-isolation canary を production で既定有効にするか、#131 の `rtmp2sink` へ移行するかは、運用コスト・失敗モード・観測性を比較した別判断として扱う。
