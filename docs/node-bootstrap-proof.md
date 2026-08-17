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

# 2. compose 定義の検証（base と public overlay）
docker compose -f docker-compose.node.yml config >/dev/null
docker compose -f docker-compose.node.yml \
  -f docker-compose.node.public.yml config >/dev/null
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
chmod 600 secrets/bootstrap_token
```

### bootstrap / heartbeat / stop

```bash
# 1. Control Plane を起動し、internal API を有効にする
NODE_BOOTSTRAP_TOKENS="$(cat secrets/bootstrap_token)" \
  NODE_EGRESS_URL="rtmp://mediamtx:1935/output/relay" \
  uvicorn app:app --host 127.0.0.1 --port 8080

# 2. Node スタックを起動
docker compose -f docker-compose.node.yml \
  -f docker-compose.node.public.yml up -d

# 3. Node Agent が登録されたことを確認
curl -fsS http://127.0.0.1:8080/internal/nodes

# 4. Secret が process arguments / env に出ないことを確認
docker inspect node-agent | grep -i "egress_url" || echo "secret not in inspect"
docker compose -f docker-compose.node.yml ps

# 5. stop を発行し、メディア process が終了することを確認
curl -fsS -X POST http://127.0.0.1:8080/internal/nodes/node-0001/stop
docker compose -f docker-compose.node.yml ps

# 6. 後片付け
docker compose -f docker-compose.node.yml \
  -f docker-compose.node.public.yml down --remove-orphans
rm -f secrets/bootstrap_token
```

## 完了条件チェック

- [ ] 新規 Node が bootstrap token を一度だけ交換し登録される
- [ ] `NODE_BOOTSTRAP_TOKEN` が再使用されると 409 を返す
- [ ] `egress_url` が `docker inspect` / process args / env に現れない
- [ ] heartbeat で `desired_state` を受信できる
- [ ] STOP 命令でメディア stack が停止する
- [ ] stop を二度送っても安全（冪等）
- [ ] MediaMTX API / metrics が公開ポートに出ない
