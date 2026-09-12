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

### Media Node provider 方針

Phase B の Media Node は **ConoHa VPS を第一候補**として維持する。既存 provider 実装、時間課金、現行の `1 Session = 1 VPS` 構成との相性を優先する。

将来、同時 Session 数・継続トラフィック・必要帯域・リージョン要件が増えた場合は、**Akamai Cloud を追加 provider 候補**として評価し、Control Plane がコスト・capacity・ネットワーク品質等に基づいて provider / region を選択できる構成を検討する。現時点では Akamai 実装や自動切替は Phase B の必須範囲に含めない。

詳細は `docs/provider-selection.md`、将来TODOは Issue #287 を参照。

## CI の外部パッケージ取得

Continuity image は Ubuntu / GStreamer の依存が大きいため、`apt` の取得処理では
Ubuntu の archive/security mirror を HTTPS で参照する。minimal Ubuntu image には
初回 HTTPS 用の CA bundle がないため、official `python:3.14-slim` image から公開 root
bundle だけを bootstrap し、その後 Ubuntu 自身の `ca-certificates` package を通常どおり
install して final trust store を所有させる。

Continuity / Control API / Node Agent の各 runtime image では、一時的な package mirror
や network 障害に対して APT transport 内で最大4回の再試行と10秒の request timeoutを使う。
APT の request timeout は `apt-get` プロセス全体の上限ではないため、さらに index 更新を
1回120秒、package install を600秒の hard deadline で囲む。Continuity の index 更新だけは、
120秒で停止した接続を同じ process のまま待ち続けず、最大3回まで `apt-get update` 自体を
再起動する。3回すべて失敗した場合も non-zero のまま終了するため、最悪時間は bounded である。
index 更新が一部でも取得できない場合も package install へ進まず失敗させる。再試行後も
取得できない場合、TLS 検証失敗、package 名・repository の不整合は従来どおり失敗とし、
Docker integration / recovery / netem の gate 自体は skip や allow-failure にしない。
これにより一時的な mirror 障害は bounded に吸収しつつ、APT 内部の一部処理が停止しても
shared Docker build が job 上限まで待ち続ける経路を防ぐ。
