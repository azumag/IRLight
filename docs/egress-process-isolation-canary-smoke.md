# Legacy egress process-isolation Docker canary

Issue #586 の Stage 3 として、`scripts/smoke-egress-isolated-reconnect.sh` は legacy `rtmpsink` の process-isolation canary を実際の Docker Compose 構成で検証する。

## 検証する契約

この smoke は `EGRESS_LEGACY_PROCESS_ISOLATION_CANARY=1` を egress-gateway にだけ設定し、production default や `rtmp2sink` の選択は変更しない。外部配信先や実 credential は使用せず、isolated Compose network 上の MediaMTX を destination として使う。

正常接続後に attempt child の PID と `/proc/<pid>/stat` の start time を取得する。PID 単体では急速な再利用を誤判定し得るため、同一 process identity は `pid:start_time` で判定する。child の command line / environment に、生成した destination URL または stream key が含まれないことも container 内で確認し、秘密値自体は CI 出力へ出さない。

次に destination を停止し、既存の 45 秒契約を延長せず `RECONNECTING` を待つ。`RECONNECTING` を観測した時点で、以下を必須とする。

- egress-gateway container が同一 ID のまま running である。
- outage 前の attempt process identity が `/proc` 上に残っていない。
- Continuity が running のままである。

その後 destination を再開し、同じ親 egress-gateway container のまま `CONNECTED` へ復帰すること、新しい attempt process identity が生成されていること、新しい child の command line / environment にも secret が出ていないことを確認する。

## CI

`.github/workflows/egress-process-isolation-canary.yml` は egress-gateway、PoC Compose、またはこの smoke 自体に関係する pull request でのみ実行する。1 回の canary は 10 分で hard timeout し、workflow job 自体も 12 分で上限を持つ。

この workflow は shared Docker suite の置き換えではない。通常の repository CI、Dependency audit、shared Docker suite、measured-soak chain は従来どおり別 gate として扱う。

## 残る Stage 3

この smoke が直接証明するのは reconnect path の child fencing、親 Gateway 生存、recovery、secret boundary である。user stop と terminal `AUTH_FAILED` / `PUBLISH_CONFLICT` を process-isolation canary 有効時にも Docker E2E で固定する作業は #586 の残件とする。production default への切替は、これらの canary evidence が揃った後の別判断であり、この smoke の追加だけでは行わない。
