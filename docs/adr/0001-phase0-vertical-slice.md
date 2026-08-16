# ADR-0001: Phase 0の単一ノード縦切り構成

- 状態: Phase 0ベースラインとして採用
- 日付: 2026-08-16
- 関連: #2, #15, #16

## 文脈

IRLightで最初に証明すべきことは、入力が途切れても出力側の配信パイプラインを終了させず、待機映像へ切り替え、入力復帰後に自動で通常映像へ戻せることである。加えて、配信を止めずにWeb UIから音声だけをミュートする必要がある。

通常時の最終目標はH.264映像を可能な限り再エンコードしない中継だが、Phase 0ではまず、切替・復帰・制御状態が一つの実行可能な系として成立することを優先する。

## 決定

### 1. MediaMTXを入口のプロトコルルーターとして使う

- RTMP入力: `live/input`
- SRT入力: `streamid=publish:live/input`
- Continuity Engineは同じストリームを内部RTSP/TCPで読む
- ローカル検証用の出力先として `output/stream` を用意する

RTMPとSRTで別々の継続処理を持たず、入口の違いをMediaMTXで吸収する。

### 2. 出力パイプラインを常時起動する

GStreamerの出力パイプラインは入力の有無にかかわらず動かし続ける。

- 映像selector: 待機映像 / 通常映像
- 音声selector: 無音 / 通常音声
- FLV muxとRTMP sinkはselectorの後段に置き、入力切替で作り直さない
- selectorはclock同期とbuffer cacheを使い、古いtimestampのbufferを捨てる

入力映像が3秒間安定したら通常映像へ戻す。映像が1.5秒以上届かなければ待機映像へ戻す。値は環境変数で変更可能にする。

### 3. ミュートは音声トラック停止ではなく無音AACへの置換とする

`audio.desired` と `audio.actual` を分離する。

```text
desired: LIVE | MUTED
actual:  LIVE | MUTED | APPLYING | FAILED
```

ユーザーがMUTEDを指定した場合、入力の接続・切断・復帰よりもその指定を優先する。入力復帰時に勝手に音声を再開しない。APIはtoggleではなく最終状態をPUTし、versionで古い画面からの競合を検出する。

### 4. ベースラインでは映像・音声を再エンコードする

Phase 0ベースラインはデコード済みraw mediaをselectorへ入れ、H.264/AACへ再エンコードする。これは最終的な原価最適化ではなく、以下の成立性を優先した選択である。

- 待機映像と通常映像を同一capsへ正規化できる
- 無音と通常音声を同じaudio clockで切り替えられる
- 入力解像度や音声形式の差をPoC段階で吸収できる
- Web UI操作とMedia Planeの状態収束を検証しやすい

### 5. Control PlaneはPoC内のインメモリ状態で代用する

本番のDB、認証、Node Agentはまだ作らない。代わりに単一プロセス内でdesired/actual/versionを管理し、本番APIの意味論だけを先に検証する。

## 代替案

### MediaMTX 1.20のalways-available + forward

MediaMTX 1.20には、入力がオフラインの際にファイルを繰り返すalways-availableと、外部サーバーへ送るforwardがある。待機映像だけなら有力な簡素化案である。

一方、任意タイミングの音声ミュート、短いfade、将来の音量調整には音声処理が必要である。したがってPhase 0ではGStreamer制御を基準とし、次の比較実験でMediaMTX単体案の範囲と原価を測る。

### H.264パススルー + 音声のみ再エンコード

本命候補。映像の画質劣化とCPU負荷を抑えられるが、待機映像とのcodec設定、GOP、PTS/DTS連続性を合わせる必要がある。Phase 0の次の実験で比較する。

### エンコード済みAACの切替

音声再エンコードを避けられる可能性があるが、AudioSpecificConfig、sample rate、channel、timestamp、切替ノイズの扱いが難しい。安定性が確認できない限り採用しない。

## 帰結

### 良い点

- RTMP入力から待機映像、復帰、出力、Web UIミュートまでを一度に試せる
- 状態モデルを本番Control Planeへ移植しやすい
- 入力プロトコルと継続処理を分離できる
- ミュート中もAACを送り続けるため、出力側の音声トラックを維持できる

### 制約

- 映像再エンコードのCPU負荷、追加遅延、画質劣化がある
- 単一Session・単一Nodeのみ
- 状態はプロセス再起動で失われる
- 認証、TLS、Secret配送、課金、監査は未実装
- 外部配信先ごとの実機検証は別途必要

## 次の判断

1. ローカルRTMP出力で切断・復帰・ミュートを確認する
2. Twitch / YouTube / Custom RTMPで30秒断と復帰を確認する
3. CPU、メモリ、追加遅延、A/V syncを2時間以上測る
4. `PASSTHROUGH` と `AUDIO_PROCESSED` の比較PoCを追加する
5. オーバーレイは `COMPOSITED_VIDEO` として別profile・別原価に分離する

## 参考

- MediaMTX install: https://mediamtx.org/docs/kickoff/install
- MediaMTX configuration: https://github.com/bluenviron/mediamtx/blob/v1.20.0/mediamtx.yml
- MediaMTX SRT publish: https://mediamtx.org/docs/publish/srt-clients
- GStreamer input-selector: https://gstreamer.freedesktop.org/documentation/coreelements/input-selector.html
- GStreamer rtmp2sink: https://gstreamer.freedesktop.org/documentation/rtmp2/rtmp2sink.html
