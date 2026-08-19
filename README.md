# IRLight

IRL配信向けリレーサーバー。実装はIssueとPull Requestで段階的に進めます。

## Phase B オンデマンド Media Node（spike）

- `provider/` : ConoHa VPS provider spike（admin CLI、fake provider、cleanup手順）
- `apps/node-agent/` : Node Agent（bootstrap / tmpfs Secret / heartbeat / stop）
- `docker-compose.node.yml` : production compose（prebuilt image、internal-only）
- `docker-compose.node.public.yml` : 公開ポートの overlay（RTMP ingest のみ）
- `apps/control-api/session_*.py` / `reaper.py` : Session lifecycle / reaper
- `apps/control-api/reaper_cli.py` : 定期実行する reaper CLI

検証手順は `docs/cleanup-proof.md`、`docs/node-bootstrap-proof.md`、
`docs/session-lifecycle-proof.md` を参照。

Ingestの実機互換性確認（OBS / mobile publisher / hardware encoder）は
`docs/ingest-device-compatibility.md` と Issue #39 を参照。
