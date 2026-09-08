# Control Plane 停止 / readiness failure runbook

Issue #11 の「Control Plane停止」を、安全な切り分けから復旧判断まで扱う。最初の診断は read-only とし、原因が分からない状態で container restart、state 修復、provider cleanup、credential 再発行を行わない。

## 影響の考え方

Control Plane が停止すると、新規 Session prepare、利用者/API 操作、Node からの状態反映などが利用不能になる可能性がある。一方、すでに Media Node 上で動作している media process を「Control Plane が見えない」だけで停止・削除してはならない。既存 Session の実際の media 状態は Node / ingest / continuity / egress 側で別に確認する。

`/healthz` は process liveness、`/readyz` は authority を含む application readiness のための endpoint であり、両者を混同しない。`healthz=2xx` かつ `readyz!=2xx` は、再起動で隠すべき単純な process down とは別の状態である。

## 最初の read-only 診断

Control API container 内、または同じ host の loopback から次を実行する。

```bash
python /app/control_plane_health_inspect_cli.py
```

必要なら loopback port だけを明示できる。

```bash
python /app/control_plane_health_inspect_cli.py \
  --base-url http://127.0.0.1:8080 \
  --timeout-seconds 3
```

CLI は `GET /healthz` と `GET /readyz` だけを実行する。非 loopback host、credential 入り URL、path/query/fragment、redirect は受け付けない。`HTTP_PROXY` / `HTTPS_PROXY` 等の proxy 環境設定も使用せず、literal loopback へ直接接続する。response body、URL、例外 detail は出力しない。

結果は次の固定分類になる。

- exit `0`, `READY`: liveness と readiness がともに 2xx。
- exit `2`, `NOT_READY`: liveness は 2xx だが readiness が非 2xx。authority / mount / state validation を優先して調べる。
- exit `3`, `UNAVAILABLE`: liveness 自体に到達できない、liveness が非 2xx、または readiness request 自体が成立しない。

この結果だけで external load balancer、DNS、reverse proxy、provider、Media Node の正常性まで断定しない。

## `NOT_READY` の場合

まず [state readiness runbook](state-readiness.md) に従い、authority の欠落・破損・marker 異常・mount 不一致を read-only に確認する。

禁止事項:

- marker を削除して初期化し直す
- 空 JSON を作る
- live state を backup で直接上書きする
- `docker compose down -v`、volume prune、volume recreate を行う
- authority が読めないことを「Session が存在しない」と解釈して provider resource を削除する

復元が必要なら [state restore drill](state-restore-drill.md) の比較手順で sandbox / restored candidate を検証し、実 restore は writer quiesce、generation / fencing、credential 失効方針を含む明示的な運用判断として行う。

## `UNAVAILABLE` の場合

read-only で次を確認する。

1. 対象 container / process が存在し、expected image/version であるか。
2. container health status と直近 exit code / OOMKilled / restart count。
3. host の disk / memory / file descriptor 枯渇。
4. 直近 deploy / config 変更の有無。
5. Control Plane 停止中も Media Node 上の進行中 Session が動いているか。Node を止めて確認しない。

ログを共有する場合は request header、cookie、authorization、stream key、destination secret、credentialed URL、provider credential を除外する。生の environment dump や unrestricted `docker inspect` 全文を Issue/PR へ貼らない。

## 復旧判断

- **deploy/config regression**: [production deploy preflight](../production-deploy-preflight.md) の記録、現在 image digest、既知正常 version を照合する。rollback は進行中 Session への影響を確認したうえで明示的に実施する。
- **authority failure**: restart loop を作らず state readiness / restore 手順へ進む。
- **OOM / disk full / host resource exhaustion**: まず不要な一時 artifact や異常 process の原因を特定する。instance size 増強や外部課金は勝手に行わない。
- **process crash**: crash reason と直近 deploy の関係を確認する。単発 restart で原因を隠さず、再発時は crash loop として扱う。
- **external proxy / DNS only**: local inspector が `READY` なら application process と外部経路を分離して調べる。TLS verification や認証を無効化して回避しない。

## 復旧確認

復旧後は最低限以下を確認する。

1. inspector が `READY`。
2. `/readyz` が authority を変更せず成功する。
3. 既存 Session の Node heartbeat / ingest / egress が事故前の期待状態へ戻っている。
4. 新規 prepare を試す場合、課金・provider resource 作成を伴うことを認識し、必要な承認・テスト枠の中で行う。
5. 失敗中に reaper / cleanup が authority 不明を理由に実 resource を削除していない。

## 事後記録

secret を含めず、発生時刻、最初の検知、`READY | NOT_READY | UNAVAILABLE` の分類、影響 Session 数、原因、復旧操作、復旧確認、再発防止を記録する。restart / rollback / state restore を実施した場合は、実施理由と対象 version / generation を残す。
