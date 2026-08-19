# HOLDING recovery stability gate

Issue #4 の復帰条件として、入力が一瞬戻っただけでユーザーSessionを `HOLDING` から `LIVE` / `DEGRADED` へ即時復帰させない。

## Default policy

`SESSION_RECOVERY_STABLE_SECONDS` の既定値は3秒。0〜30秒へ設定可能とする。

`HOLDING` 中にNode ingest observationが次を満たすと復帰候補を開始する。

- `online=true`
- `status=ACCEPTED|WARNING` → candidate target `LIVE`
- `status=DEGRADED` → candidate target `DEGRADED`

Sessionには候補を永続化する。

- `recovery_candidate_started_at`
- `recovery_candidate_source_id`
- `recovery_candidate_target`

同一source・同一targetが安定化時間以上継続した次のheartbeatで、初めて `session.recovered` を記録して `LIVE` / `DEGRADED` へ昇格する。

## Reset conditions

以下では安定化時計をリセットする。

- publisher/source ID変更
- `LIVE`候補と`DEGRADED`候補の切り替わり
- offline
- PENDING / UNKNOWN / REJECTEDなど復帰可能品質ではない観測

短時間の reconnect → disconnect を繰り返しても、候補期間中は `last_ingest_at` と `hold_deadline_at` を更新しない。したがってflapだけでHOLD保持期限を無限に延長しない。

## Restart behavior

復帰候補はSession storeへ保存するため、Control Plane process再起動で安定化時計を最初からやり直さない。再起動後も同じsource/targetの健全なheartbeatが届けば、保存済みcandidate startから経過時間を計算する。

逆に時刻が候補開始より巻き戻った場合はcandidateを新規開始扱いにし、早すぎる復帰を防ぐ。

## Heartbeat vs audit events

Nodeの `ingest.*` audit eventは従来どおり状態・source等が変化した時だけ記録する。一方、Session lifecycle判定にはassigned Sessionの全ingest heartbeatを渡す。

これにより同じ `ACCEPTED` 状態が続く通常ケースでも3秒経過を確認でき、監査eventをheartbeatごとに増殖させない。

## Scope

このgateはControl Plane Session lifecycleの復帰確定を安定化するもの。Continuity media pipeline側にも既存 `RECOVERY_STABLE_SECONDS` があり、映像selector自身の切り替えを保護する。今後profile/codec changeやaudio/video片系停止をSession lifecycleへ統合する際も、このgateへ渡る `ingest.status` / quality判定を入口とする。
