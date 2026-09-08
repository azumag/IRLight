# Session 実プロセスの crash loop 切り分け

Issue #11 の Release 1 運用 runbook のうち、Media Node 上の Session 実プロセスが停止・再起動を繰り返しているように見える場合の **read-only 切り分け**を定義する。

この手順の目的は「何が壊れているか」を安全に分類することまでである。自動 restart、container recreate、secret 差し替え、image rollback は行わない。特に `egress-gateway` と `node-agent` は意図的に `restart: "no"` であり、停止を見て機械的に再起動すると terminal failure や bootstrap token の安全境界を壊し得る。

## 1. まず Control Plane 側を確認する

heartbeat 自体が止まっている場合は先に [Media Node heartbeat 停止 runbook](./media-node-heartbeat-stopped.md) を使う。Node Agent が到達不能なら、Media Node 内のプロセス状態だけを見ても Session authority との対応を確定できない。

heartbeat が継続しているのに `media_stack` が `stopped` / `starting` へ落ちる、または Session が `DEGRADED` を繰り返す場合に本 runbook を使う。

## 2. read-only inspector

Node Agent image には `/opt/irlight/media_stack_inspect_cli.py` を含める。Node Agent が使う media-only Compose control file と read-only Docker socket を利用し、次の情報だけを JSON で出力する。

- service 名
- container state
- health 状態
- exit code
- OOMKilled
- lifetime restart count
- 判定済みの `problems` / `warnings`

container の environment、command line、mount、secret file、外部配信 URL は出力しない。内部では `docker compose ... ps --all --format json` と、明示的に field を限定した `docker inspect --format` だけを使う。`docker inspect` の完全な JSON は secret を含み得るため runbook でも使用しない。

運用シェルから Node Agent の診断プロセスを起動できる場合の例:

```sh
docker compose \
  -f docker-compose.node.yml \
  -f docker-compose.node.public.yml \
  exec -T node-agent \
  python3 /opt/irlight/media_stack_inspect_cli.py
```

RTMPS overlay を使っている構成では、実 deployment と同じ compose file の組を指定する。上記コマンドは診断プロセスを追加で実行するだけで、service の start / stop / recreate は行わない。

exit code は次の意味を持つ。

| code | 意味 |
| --- | --- |
| `0` | 現時点で問題・警告なし |
| `1` | 警告のみ。典型例は lifetime restart count が閾値以上 |
| `2` | `restarting`、非 running、OOMKilled、unhealthy、期待 service 欠落などを検出 |
| `3` | Docker / Compose 状態を安全に取得できず判定不能 |

`NODE_RESTART_WARNING_COUNT` の既定値は `3`。これは **lifetime restart count** なので、値が大きいだけで「現在 crash loop 中」と断定してはいけない。現在の `state=restarting`、heartbeat / Session event の時刻、直近の運用変更と突き合わせる。

## 3. 判定の読み方

### `mediamtx` / `continuity`

この2 service は `restart: unless-stopped` である。`state_restarting` が続く、または短い観測間隔で restart count が増加する場合は crash loop の可能性が高い。

- `oom_killed`: メモリ圧迫または limit / host memory を優先確認する。
- `unhealthy`: process は生存していても healthcheck が成立していない。依存先・media path の状態と分けて確認する。
- `not_running`: exit code と直前の deploy/config 変更を確認する。

restart count を比較するために同 inspector を時間を空けて再実行してよい。ただし本 runbook は自動ポーリングや自動復旧を要求しない。

### `egress-gateway`

`egress-gateway` は transient reconnect を process 内部で扱い、terminal な `AUTH_FAILED` / `PUBLISH_CONFLICT` では **意図的に停止する**。したがって `not_running` を見ても即 restart しない。

まず `/state/egress.json` の既存 redacted status と Session event を照合し、terminal failure なら配信先 credential / concurrent publisher / destination policy を直す。terminal failure を Docker restart loop に変換しない。

`NODE_EGRESS_MODE=RELAY_ONLY` または `EGRESS_GATEWAY_ENABLED=0` では inspector は `egress-gateway` を expected service に含めない。

### `node-agent`

`node-agent` 自体は inspector の media service 判定対象外である。`restart: "no"` なのは、clean STOP 後に消費済み bootstrap token で再登録を繰り返さないためである。heartbeat が止まっている場合は heartbeat runbook に戻り、Node authority と provider 状態を先に確認する。

## 4. やってはいけない自動復旧

原因を分類する前に、次を実行しない。

```text
docker compose restart ...
docker compose up -d ...
docker compose down ...
docker compose down -v ...
docker rm ...
docker volume prune ...
```

また secret / stream key を Issue、PR、ログへ貼らない。full `docker inspect`、`docker compose config` の環境展開済み出力も、secret を含み得る場所へそのまま保存しない。

## 5. 復旧方針

- **直近 image / config 変更後から発生**: [production deploy preflight](../production-deploy-preflight.md) の記録と既知正常 image を確認し、rollback の判断材料を揃える。実 rollback は別の明示的な運用判断として行う。
- **OOM**: host memory、container limit、同時 Session / process 数を確認する。勝手に instance size を上げたり外部課金を増やさない。
- **egress terminal failure**: destination credential / conflict / policy を解消し、terminal 状態を通常の reconnect と混同しない。
- **原因不明 / inspector `UNAVAILABLE`**: Docker daemon / socket / Compose control file の可用性を確認し、read-only で判定できない間は restart に進まない。

復旧後は heartbeat、Session lifecycle、media health を別々に確認する。プロセスが `running` に戻ったことだけで Session 復旧完了とは扱わない。

## 6. 記録する項目

Issue / incident note には secret を含めず、最低限次を残す。

- 対象 Session / Node の ID
- inspector の `status` と各 service の redacted state
- restart count を比較した場合は観測時刻と差分
- OOM / unhealthy / terminal egress failure の有無
- 直近 deploy / config 変更の有無
- 実施した復旧操作と、その後の heartbeat / Session / media health の確認結果
