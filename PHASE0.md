# IRLight Phase 0 vertical slice

IRLightは、IRL配信の入力が途切れても出力配信枠を維持し、待機映像へ切り替え、入力復帰後に通常映像へ戻すためのリレーサーバーです。

このブランチは #2 と #15 の成立性を早く確認するための**単一ノード・単一セッションPoC**です。本番サービスではありません。

## このPoCで確認できること

- RTMP/SRT受付をMediaMTXへ集約
- 入力がない間も待機映像＋無音AACを出力
- 入力映像を3秒確認してから通常映像へ自動復帰
- 入力断時に同じ出力パイプライン内で待機映像へ戻る
- Web UIから映像を止めずに音声だけミュート／解除
- ミュートdesired stateを入力再接続後も維持
- desired/actual状態をモバイル画面で確認

> 現在のpipelineは機能検証を優先して映像も再エンコードします。低負荷な本番構成ではありません。判断理由は `docs/adr/0001-phase0-vertical-slice.md` を参照してください。

## 起動

```bash
cp .env.example .env
docker compose -f docker-compose.poc.yml up --build
```

起動直後は入力がないため、ローカル出力 `output/relay` に待機映像が流れます。

### テスト映像を送る

FFmpegがローカルにある場合:

```bash
bash scripts/publish-test.sh
```

OBS等では次を指定します。

```text
Server: rtmp://localhost:1935/live
Stream key: input
```

### 出力を見る

```bash
bash scripts/preview-output.sh
```

またはHLS:

```text
http://localhost:8888/output/relay/index.m3u8
```

### 音声をミュートする

ブラウザで次を開きます。

```text
http://localhost:8080
```

APIでも最終状態を指定できます。

```bash
curl -X PUT http://localhost:8080/api/audio \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-1' \
  --data '{"mode":"MUTED","expected_version":0}'
```

状態確認:

```bash
curl http://localhost:8080/api/status
```

## Twitch等へ出す

`.env` の `EGRESS_URL` をテスト用のRTMP/RTMPS URLへ変更します。stream keyをリポジトリ、ログ、Issueへ貼らないでください。

```dotenv
EGRESS_URL=rtmps://example.invalid/app/SECRET_STREAM_KEY
```

## 重要な安全上の制約

- Control UI/API、RTMP ingest、MediaMTX APIには認証がありません
- localhostまたは信頼できる閉域環境だけで実行してください
- `9998` metricsを含むポートをインターネットへ公開しないでください
- stream keyを`.env`以外へ保存しないでください
- 映像の録画は行いませんが、外部配信先へ送った内容は先方の仕様に従います

## 構成

```text
OBS / smartphone / FFmpeg
          │ RTMP or SRT
          ▼
      MediaMTX
          │ internal RTSP
          ▼
 GStreamer Continuity Engine
   ├─ live input
   ├─ standby video
   ├─ live audio
   └─ silent audio (mute/fallback)
          │ RTMP/RTMPS
          ▼
 MediaMTX local preview or external destination

 Mobile Web UI ── desired audio state ──┐
                                       └─ shared state ── Continuity Engine
```

## 検証

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile apps/continuity/state.py apps/continuity/continuity.py apps/control-api/app.py
docker compose -f docker-compose.poc.yml config
```

詳細な手動試験は `docs/phase0-test-plan.md` を参照してください。

2026-08-16時点の実測結果（待機出力・LIVE遷移・ミュート・切断・再接続・ミュート維持）は
`docs/phase0-verification.md` に記録しています。Docker E2Eは
`bash scripts/smoke-compose.sh` で再現できます。

### メディアプロファイル

`.env` の `PROFILE` で切り替えられます。

- `COMPOSITED_VIDEO_POC`: Phase 0 baseline。映像・音声とも再エンコード
- `AUDIO_PROCESSED`: 映像パススルー＋音声のみ再エンコード（本番候補）
- `PASSTHROUGH`: 映像・音声ともパススルー（入力がH.264/AACのとき）

実測比較は `docs/profile-comparison.md` を参照してください。

## 次の実装順

1. Dockerで基本切断・復帰とミュートを実測
2. Twitch / YouTube / Custom RTMPで枠維持を確認
3. H.264 passthrough + 音声のみ再エンコード方式と比較
4. 状態・bitrate・A/V syncを自動測定
5. 認証付きControl Plane / Node Agentへ分離
6. Phase 3で時刻・概略現在地・受信bitrateオーバーレイを検証
