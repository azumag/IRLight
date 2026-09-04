# Node bootstrap and production compose: verification checklist

## 目的

PR 2 (Node bootstrapとproduction compose) の完了条件を満たすため、以下を
ローカルまたは実機で確認する。

- 新規 Node が自動 bootstrap する
- Secret が `docker inspect` と process arguments へ出ない
- Node Agent が READY と heartbeat を返す
- stop でメディア process を終了できる

## ローカル（fake）での確認

```bash
# 1. unit test（internal API、Node Agent、secret file、provider を含む）
python3 -m unittest discover -s tests -v

# 2. compose 定義の検証（base、Agent制御用、public overlay）
docker compose -f docker-compose.node.yml config >/dev/null
docker compose -f apps/node-agent/docker-compose.control.yml config >/dev/null
docker compose -f docker-compose.node.yml \
  -f docker-compose.node.public.yml config >/dev/null

# 3. Agent image内にCompose CLIと制御定義があることを実行確認
docker build -t irlight-node-agent:local ./apps/node-agent
docker run --rm --entrypoint docker irlight-node-agent:local compose version
docker run --rm --entrypoint test irlight-node-agent:local \
  -r /opt/irlight/docker-compose.control.yml
```

## 実機（compose）での確認

### 準備

事前 build 済み image を `NODE_CONTINUITY_IMAGE` / `NODE_AGENT_IMAGE` で指定する。
ローカル検証では既存 Dockerfile から build して指定する。

```bash
docker build -t irlight-continuity:local ./apps/continuity
docker build -t irlight-node-agent:local ./apps/node-agent

export NODE_CONTINUITY_IMAGE=irlight-continuity:local
export NODE_AGENT_IMAGE=irlight-node-agent:local
export NODE_CONTROL_PLANE_URL=http://127.0.0.1:8080
mkdir -p secrets
printf '%s' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  > secrets/bootstrap_token
printf '%s' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  > secrets/node_admin_token
chmod 600 secrets/bootstrap_token secrets/node_admin_token
```

### bootstrap / heartbeat / stop

```bash
# 1. Control Plane を起動し、internal API を有効にする
NODE_BOOTSTRAP_TOKENS="$(cat secrets/bootstrap_token)" \
  NODE_INTERNAL_ADMIN_TOKEN_FILE="$PWD/secrets/node_admin_token" \
  NODE_EGRESS_URL="rtmp://mediamtx:1935/output/relay" \
  NODE_EGRESS_VERIFIED_PEER_IP="198.51.100.10" \
  uvicorn app:app --host 127.0.0.1 --port 8080

# 2. Node スタックを起動
docker compose -f docker-compose.node.yml \
  -f docker-compose.node.public.yml up -d

# 3. Node Agent が登録されたことを確認
sed 's/^/Authorization: Bearer /' secrets/node_admin_token | \
  curl -H @- -fsS http://127.0.0.1:8080/internal/nodes

# 4. Secret が process arguments / env に出ないことを確認
docker inspect node-agent | grep -i "egress_url" || echo "secret not in inspect"
docker compose -f docker-compose.node.yml ps

# 5. stop を発行し、メディア process が終了することを確認
sed 's/^/Authorization: Bearer /' secrets/node_admin_token | \
  curl -H @- -fsS -X POST \
    http://127.0.0.1:8080/internal/nodes/node-0001/stop
docker compose -f docker-compose.node.yml ps

# 6. 後片付け
docker compose -f docker-compose.node.yml \
  -f docker-compose.node.public.yml down --remove-orphans
rm -f secrets/bootstrap_token secrets/node_admin_token
```

## 完了条件チェック

- [ ] 新規 Node が bootstrap token を一度だけ交換し登録される
- [ ] 応答喪失後の同一attempt再送は同じNodeを返し、重複Nodeを作らない
- [ ] 同じbootstrap tokenを別identity / 別Node tokenで再使用すると409を返す
- [ ] Node recordとbootstrap token消費が単一authority fileへatomicに保存される
- [ ] `egress_url` が `docker inspect` / process args / env に現れない
- [ ] Continuity containerから`egress_url`を参照できない
- [ ] heartbeat で `desired_state` を受信できる
- [ ] heartbeatはbootstrap応答のNode Bearerでのみ成功する
- [ ] Node一覧にaccess token digestが出ない
- [ ] 管理Bearerなしのlist / stopが401になる
- [ ] STOP 命令でメディア stack が停止する
- [ ] stop を二度送っても安全（冪等）
- [ ] SupervisorはNode Agent自身を止めずmedia serviceだけを停止する
- [ ] `RELAY_ONLY`ではEgress Gatewayを停止する
- [ ] MediaMTX API / metrics が公開ポートに出ない
