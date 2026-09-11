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

認証・秘密境界は `docs/ingest-authentication.md`、
`docs/node-local-ingest-auth-cache.md`、`docs/egress-reconnect.md` を参照。
内部Node APIは、Nodeごとの一回限りbootstrap後に返すheartbeat Bearerと、
list/stop専用の管理Bearerを分離する。

Ingestの実機互換性確認（OBS / mobile publisher / hardware encoder）は
`docs/ingest-device-compatibility.md` と Issue #39 を参照。

Phase 0 Control UI の状態鮮度・通信断時の fail-closed 表示契約は
`docs/control-ui-state-safety.md` を参照。

## CI の外部パッケージ取得

Continuity image は Ubuntu / GStreamer の依存が大きいため、`apt` の取得処理では
Ubuntu の archive/security mirror を HTTPS で参照する。minimal Ubuntu image には
初回 HTTPS 用の CA bundle がないため、official `python:3.14-slim` image から公開 root
bundle だけを bootstrap し、その後 Ubuntu 自身の `ca-certificates` package を通常どおり
install して final trust store を所有させる。一時的な network 障害には最大2回の再試行と
10秒の通信 timeout を使い、index 更新が一部でも取得できなければ package install へ
進まず失敗させる。再試行後も取得できない場合や package 名・repository の不整合は
従来どおり失敗とし、Docker integration / recovery / netem の gate 自体は skip や
allow-failure にしない。
