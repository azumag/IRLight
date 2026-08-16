# Phase 0 手動試験計画

## 事前条件

- Docker Engine / Docker Compose v2
- publisher確認用のFFmpeg
- preview確認用のffplayまたはVLC
- 公開配信先を使う場合は限定公開・テスト用stream key

## 基本試験

1. `docker compose -f docker-compose.poc.yml up --build`
2. `bash scripts/preview-output.sh` で待機映像と無音を確認
3. `bash scripts/publish-test.sh` を開始
4. 約3秒の安定確認後、通常映像と880Hz音へ切り替わることを確認
5. `http://localhost:8080` でミュートし、映像が継続したまま無音になることを確認
6. ミュート解除し、入力音声へ戻ることを確認
7. publisherをCtrl+Cで停止し、出力が終了せず待機映像へ戻ることを確認
8. MUTEDのままpublisherを再開し、映像だけ復帰して音声は無音のままであることを確認

## 切断時間

- 10秒
- 30秒
- 2分
- 10分

各試験で、出力RTMPセッション、黒画面時間、音声ノイズ、復帰時間を記録する。

## 長時間

- 2時間: 基本合格
- 6時間: Phase 0 exit候補

記録項目:

- CPU / memory / network
- A/V sync drift
- GStreamer process restart回数
- 入力再接続成功率
- mute適用時間
- 配信先での枠終了有無

## 既知の未検証

- 本番の映像passthrough
- RTMPS終端
- SRTをContinuity Engineへ直接接続した場合の統計
- 出力先障害時の指数バックオフ
- codec/resolution変更を伴う再接続
- 認証・認可・secret配送
- 複数Session
