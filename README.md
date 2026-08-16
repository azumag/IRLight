# IRLight

**電波が切れても、配信枠は切らさない。**

IRLightは、OBS、スマートフォン配信アプリ、ハードウェアエンコーダー等からRTMP / SRTで映像を受け、Twitch、YouTube、Kick、任意RTMPサーバーへ中継する配信継続サービスを目指すプロジェクトです。

このリポジトリには現在、Phase 0の単一ノード縦切りPoCがあります。

## このPoCでできること

- RTMP / SRT入力をMediaMTXで受ける
- 入力がない間も待機映像と無音を出力し続ける
- 入力が安定したら通常映像へ自動復帰する
- Web UIから、映像を出したまま配信音声だけをミュート／解除する
- desired stateとactual stateを分け、古いUI操作をversionで検出する
- ローカルMediaMTXまたは外部RTMP / RTMPSへ出力する

```text
OBS / Smartphone / FFmpeg
        │ RTMP or SRT
        ▼
┌──────────────────────┐
│ MediaMTX             │
│ path: live/input     │
└──────────┬───────────┘
           │ internal RTSP/TCP
           ▼
┌─────────────────────────────────┐
│ GStreamer Continuity Engine     │
│ video: LIVE ⇄ STANDBY           │
│ audio: LIVE ⇄ SILENCE           │
│ Web UI / PUT /api/audio         │
└──────────┬──────────────────────┘
           │ RTMP / RTMPS
           ▼
Local preview / Twitch / YouTube / Custom
```

## 最短の起動手順

必要なもの:

- Docker EngineまたはDocker Desktop
- Docker Compose v2
- 入出力確認用のFFmpeg / ffplay（任意）

```bash
cp .env.example .env
make up
```

管理画面:

```text
http://127.0.0.1:8080
```

合成テスト映像を入力する:

```bash
make publish
```

別ターミナルで出力を見る:

```bash
make preview
```

入力をCtrl+Cで止めると待機映像へ移り、同じコマンドを再実行すると約3秒の安定確認後に通常映像へ戻ります。

音声操作:

```bash
make mute
make unmute
```

## OBSから入力する

ローカルPoC:

- Service: Custom
- Server: `rtmp://127.0.0.1:1935/live`
- Stream key: `input`

別端末からLAN経由で試す場合は `.env` を次のように変更する。

```dotenv
IRLIGHT_BIND_ADDRESS=0.0.0.0
IRLIGHT_PUBLIC_HOST=192.0.2.10
```

`192.0.2.10` は実際のサーバーLAN IPへ置き換える。Phase 0には入力認証やTLSがないため、ポート1935 / 8554 / 8890をそのままインターネットへ公開しない。

SRT入力:

```text
srt://<server>:8890?streamid=publish:live/input&pkt_size=1316
```

## 外部配信先へ送る

`.env` の `IRLIGHT_OUTPUT_URL` に完全なRTMP / RTMPS URLを設定する。

```dotenv
IRLIGHT_OUTPUT_URL=rtmps://<destination>/<application>/<stream-key>
```

このSecret配送はPhase 0限定です。本番ではKMS等で暗号化し、Sessionごとに短時間だけMedia Nodeへ配送します。実験用のstream keyを使い、リポジトリへコミットしないでください。

## ミュートの仕組み

ミュート時に音声パケットを止めるのではなく、48kHz stereoの無音をAACへエンコードして送出し続けます。これにより、映像とRTMP出力パイプラインを維持したまま音声だけを切り替えます。

```text
audio.desired: LIVE | MUTED
audio.actual:  LIVE | MUTED | APPLYING | FAILED
```

入力が切断・復帰してもdesired=MUTEDを優先するため、復帰時に意図せず声が再開しません。

## 設計上の位置付け

このベースラインは成立性を優先し、映像・音声をGStreamerで再エンコードします。最終構成では次のprofileを分離します。

- `PASSTHROUGH`: H.264 / AACを可能な限り再エンコードしない
- `AUDIO_PROCESSED`: 映像はパススルー、音声のみ処理
- `COMPOSITED_VIDEO`: 時刻・現在地・受信bitrate等のoverlay用。映像再エンコードあり

時刻・現在地・通信状況overlayは #16 のPhase 3です。特に「通信速度」は携帯回線の最大速度ではなく、まずMedia Nodeが観測した受信bitrateとして扱います。現在地は初期OFF、概略化、表示遅延、短期保持を前提にします。

## 現在の制約

- 単一Session・単一Node
- 認証、RTMPS ingest、Secret管理、課金、監査は未実装
- 状態はメモリ内のみで、process再起動では初期化される
- UIは1秒pollingで、SSE / WebSocketではない
- 映像を常時再エンコードするため、最終原価・画質の構成ではない
- 実際のTwitch / YouTube / Kickでの長時間試験は未完了
- オーバーレイは未実装

## 開発

```bash
make test
make smoke
make logs
make down
```

詳しい切断・復帰・外部出力試験は [docs/poc.md](docs/poc.md)、構成判断は [ADR-0001](docs/adr/0001-phase0-vertical-slice.md) を参照してください。

## 関連Issue

- #1 全体計画
- #2 RTMP / SRT中継と切断保護PoC
- #15 Web UIからの配信音声ミュート
- #16 時刻・現在地・通信状況overlay
