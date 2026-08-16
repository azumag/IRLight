# メディアプロファイル比較（2026-08-16 実測）

## 背景

Phase 0のbaselineは映像・音声とも再エンコードする `COMPOSITED_VIDEO_POC` です。
本PRで `AUDIO_PROCESSED`（映像パススルー＋音声のみ処理）と `PASSTHROUGH`（映像・音声とも
パススルー）を追加し、Docker上で動作と負荷を確認しました。

## 各プロファイルの構成

| プロファイル | 入力側 | 映像処理 | 音声処理 | ミュート |
| --- | --- | --- | --- | --- |
| COMPOSITED_VIDEO_POC | uridecodebin（decode） | videoconvert→x264enc | audioconvert→avenc_aac | selectorで無音AACへ切替 |
| AUDIO_PROCESSED | rtspsrc→rtph264depay→appsrc | パススルー（再エンコードしない） | audioconvert→avenc_aac | selectorで無音AACへ切替 |
| PASSTHROUGH | rtspsrc→rtph264depay/rtpmp4gdepay→appsrc | パススルー | パススルー（形式は入力依存） | 無音AAC形式が同形式時に限る |

## 動作確認結果（Docker / MediaMTX 1.20 / GStreamer 1.24）

| シナリオ | COMPOSITED | AUDIO_PROCESSED | PASSTHROUGH |
| --- | --- | --- | --- |
| 入力なし待機（HOLDING/STANDBY/無音） | PASS | PASS | PASS |
| RTMP入力→STABILIZING→LIVE | PASS | PASS | PASS |
| H.264 1280x720@30 + AAC 48k 2ch 出力 | PASS | PASS | PASS（入力形式に一致） |
| ミュート（映像維持・無音AAC） | PASS | PASS | 条件付き（同形式のみ） |
| 切断→待機→再接続→LIVE | PASS | PASS | PASS |
| desired/actual/version維持 | PASS | PASS | PASS |

## 負荷実測（publisher稼働時、コンテナ内 `ps`）

| プロファイル | RSS | CPU |
| --- | --- | --- |
| COMPOSITED_VIDEO_POC（60分soak実測） | 約210MiB（45分以降頭打ち） | 約46〜69% |
| AUDIO_PROCESSED | 約152MiB | 約103% |
| PASSTHROUGH | 約152MiB（安定） | 約44〜97% |

> 注意：ホストのDocker stats表示は仮想メモリ込みで最大4.5GiBと出ることがありますが、
> コンテナ内 `ps` の実RSSは上記の通りです。CPUはホスト論理コア換算で、
> 並行プロセスやGStreamerスレッドの影響を受けます。

## 設計判断

- **本番候補は `AUDIO_PROCESSED`**。映像をパススルーしつつ、ミュートを同一形式の無音AACへ
  確実に切り替えられるため、出力RTMPセッションを維持しやすい。
- `PASSTHROUGH` は「入力がH.264/AACで形式が既知」のケースで成立する参考実装。
  ミュートを完全に行うには入力AAC形式と無音AAC形式の整合が必要で、現状は
  「映像パススルー＋音声フォールバック」に限定。形式不整合時の無音切替は将来課題。
- `COMPOSITED_VIDEO_POC` はオーバーレイ等の再エンコードが必要な機能のベースラインであり、
  本番の低負荷構成と同一視しない。

## 残課題

- Twitch / YouTube / Kick 等の外部配信先での枠維持試験
- `PASSTHROUGH` の無音切替を同一形式で完全に行う方式（FLV連続性の維持）
- 2時間超のsoak（リポジトリルールでは10分程度を標準とし、必要な場合のみ延長）
