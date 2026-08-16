# Phase 0 検証結果

検証日: 2026-08-16
対象: `agent/phase0-poc` (PR #18)
環境: macOS + Docker (OrbStack) / Docker Compose v5、MediaMTX 1.20.0、GStreamer 1.24 (Ubuntu 24.04 コンテナ)

## 静的検査

| 検査 | 結果 |
| --- | --- |
| `python3 -m unittest discover -s tests -v` (状態遷移4件) | PASS |
| `python3 -m py_compile apps/continuity/state.py apps/continuity/continuity.py apps/control-api/app.py` | PASS |
| `bash -n scripts/publish-test.sh scripts/preview-output.sh scripts/smoke-control.sh scripts/smoke-compose.sh` | PASS |
| `docker compose -f docker-compose.poc.yml config` | PASS |
| `config/mediamtx.yml` のYAML parse | PASS |

## Dockerランタイム（`scripts/smoke-compose.sh`）

クリーンな状態 (`docker compose down -v`) から build → 起動 → 配信 → 切断 → 再接続を実行し、最終行
`IRLight Docker smoke test passed.` を確認した。

### 入力なし（待機）

- `session_status=HOLDING` / `video_source=STANDBY` / `actual_audio_mode=SILENT_FALLBACK`
- `http://127.0.0.1:8888/output/relay/index.m3u8` が 200 を返し、待機映像＋無音AACを配信継続
- 入力パイプラインは3秒間隔で再接続を試行し、出力パイプライン・RTMP出力は維持

### RTMP入力 → LIVE

- publisher開始直後: `STABILIZING`（映像・音声受信開始）
- 約3秒の安定確認後: `session_status=LIVE` / `video_source=LIVE` / `actual_audio_mode=LIVE`
- 出力をFFprobe: `h264 1280x720 30fps` + `aac 48000Hz 2ch`

### 音声ミュート / 解除

- `PUT /api/audio` に `Idempotency-Key` + `expected_version` を付与して `MUTED` を送信 → `version` が1増加
- ミュート中: `video_source=LIVE` のまま `actual_audio_mode=MUTED`（映像・出力セッション維持）
- ミュート中の出力をFFprobe: 音声トラックは消失せず `aac 48000Hz 2ch` が存在
- ミュート中の音量: `mean_volume -91.0 dB / max_volume -91.0 dB`（無音AAC送出）
- 解除後: `actual_audio_mode=LIVE`、音量 `mean_volume -24.2 dB`（入力音声復帰）

### 入力切断 / 再接続 / ミュート維持

- publisher停止: 約1.5秒で `HOLDING` / `STANDBY` / `SILENT_FALLBACK` へ遷移
- 切断中もHLS 200を維持し、MediaMTXの `output/relay` へのRTMP接続回数は再作成されず1のまま
- MUTED中に切断: `desired_audio_mode=MUTED` / `actual_audio_mode=MUTED` を維持
- 再接続: `STABILIZING + MUTED` → `LIVE + MUTED` へ収束
- 状態列: `HOLDING → STABILIZING → LIVE → MUTED → HOLDING+MUTED → STABILIZING+MUTED → LIVE+MUTED → LIVE`

## 修正した不具合

1. **`textoverlay` プラグイン非依存化**: Ubuntu 24.04 / Debian bookworm のGStreamerには `textoverlay`
   が無く、待機映像のbuildがクラッシュしていた。待機映像を `videotestsrc`（黒）に変更し、要素生成の
   Noneチェックを全箇所に適用した。
2. **入力再接続の作り直し**: 実行中パイプラインへの `uridecodebin` の動的追加・状態トグルでは
   RTSP PLAYが送信されず、再接続できない事象を確認した。入力パイプラインを都度フレッシュに生成する
   構成（出力パイプラインは常時維持）へ変更した。
3. **CIワークフローのパス修正**: `poc/` 参照のままになっていた unit / py_compile / docker-smoke を
   このリポジトリの `apps/`・`tests/`・`scripts/` 構成へ合わせた。docker-smoke 用に
   `scripts/smoke-compose.sh` を追加した。
4. **公開面の縮小**: Compose の公開ポートを `127.0.0.1` バインドへ変更し、MediaMTX metrics のホスト公開を
   廃止した。MediaMTX の `paths` を `live/input` と `output/relay` のみに制限した。
5. **RTSPトランスポート**: リレー用途で安定する TCP interleaved (`rtspTransports: [tcp]`) を指定した。

## 60分soak試験（2026-08-16 夜、追加実施）

`docker compose down -v` のクリーン状態から、`COMPOSITED_VIDEO_POC` 構成で
MUTED指定のまま60分間連続運転し、5分毎に入力publisherを約20秒停止する入力断試験を12回実施した。

| 項目 | 結果 |
| --- | --- |
| 出力RTMP接続（MediaMTX `output/relay`） | 60分間・12回の入力断を通して常に1本（再作成なし） |
| 状態遷移 | 毎回 `LIVE+MUTED → HOLDING+MUTED → LIVE+MUTED` へ収束 |
| desired/actual | 全期間 `MUTED / MUTED` を維持 |
| 入力source世代 | 入力断毎に数回の再試行を観測（`SOURCE_RETRY_SECONDS=3`）、復帰後に安定 |
| CPU | 約46〜69%（x264再エンコード込み） |
| メモリ | 約96MiBから約210MiBまで増加し、45分以降は約200〜216MiBで頭打ち |
| 出力映像 | 終了時もH.264 1280x720@30 + AAC 48kHz 2ch |
| 出力音声 | 終了時も `mean/max_volume -91.0dB`（無音AAC継続） |
| PTS連続性 | 70秒の壁時計に対しPTSも+70,000ms（1:1進行）、巻き戻り・不連続なし |

メモリが約96→約210MiBで頭打ちすることは確認したが、これが6時間超でも安定するかは
今後のsoakで継続監視する。本試験はローカルMediaMTX出力でのもので、外部配信先の枠維持は未実施。

## 未実施 / 残課題

- Twitch / YouTube / Kick などの外部配信先での実配信試験
- 2時間 / 6時間のsoak test（本試験は60分）、CPU・メモリ・遅延・drift の性能比較
- `PASSTHROUGH` / `AUDIO_PROCESSED` との比較
- 認証・TLS・複数Session・Secret配送（Phase 0範囲外）
