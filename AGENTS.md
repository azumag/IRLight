# IRLight repository rules

## ランタイム検証

- Dockerランタイム検証は、デフォルトで **10分程度のsoak** で十分とする。
- 60分・2時間・6時間の長時間soakは、基本実施しない。長時間安定性が明示的に必要な場合のみ、個別判断で延長する。
- 検証は必ず実際に実行したコマンドと結果だけを記録する。未実施の試験を「確認済み」と書かない。

## Phase 0の扱い

- 認証・TLS・複数Session・Secret配送は未実装の技術PoCであり、公開環境向けの構成として説明しない。
- Control UI / API、ingest、MediaMTX metrics をインターネットへ直接公開しない。
- 外部配信先（Twitch / YouTube / Kick 等）の試験は限定公開・テスト用stream keyで行い、stream keyや `.env` をリポジトリ・ログ・Issueへ出さない。

## 音声ミュート

- ミュートは音声トラックの削除ではなく、連続した無音AACへの置換とする。
- `audio_desired`（LIVE / MUTED）と `audio_actual`（LIVE / MUTED / SILENT_FALLBACK / APPLYING / FAILED）を分離し、入力断・再接続をまたいでdesiredを維持する。
- 制御APIはトグルではなく最終状態のPUTとし、Idempotency-Keyとexpected_versionで競合を検出する。

## メディアプロファイル

- `PASSTHROUGH`（映像・音声とも再エンコードしない）と `AUDIO_PROCESSED`（映像パススルー＋音声のみ処理）を、Phase 0の再エンコード型 `COMPOSITED_VIDEO_POC` と混同しない。
- PoCの再エンコード型の負荷・遅延・原価を、本番の低負荷パススルー構成の見積もりに使わない。
- 将来のオーバーレイ（時刻・現在地・通信指標）は `COMPOSITED_VIDEO` 系のみで実装し、通常のパススルー利用者へ強制しない。
