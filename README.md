# IRLight

IRL配信向けリレーサーバー。実装はIssueとPull Requestで段階的に進めます。

## Phase B オンデマンド Media Node（spike）

- `provider/` : ConoHa VPS provider spike（admin CLI、fake provider、cleanup手順）
- `apps/node-agent/` : Node Agent（bootstrap / tmpfs Secret / heartbeat / stop）
- `docker-compose.node.yml` : production compose（prebuilt image、internal-only）
- `docker-compose.node.public.yml` : 公開ポートの overlay（RTMP ingest のみ）

検証手順は `docs/cleanup-proof.md` と `docs/node-bootstrap-proof.md` を参照。
