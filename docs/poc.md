# Phase 0 PoC試験手順

## 試験対象

このPoCは次の一連の動作だけに範囲を絞る。

1. RTMPまたはSRTで映像・音声を受ける
2. 入力がなければ待機映像と無音を出力する
3. 入力が安定したら通常映像・音声へ切り替える
4. 入力が途切れたら出力パイプラインを残したまま待機映像へ戻す
5. 入力復帰後に通常映像へ戻す
6. Web UIまたはAPIから音声だけを無音へ切り替える

本番のユーザー認証、配信キー暗号化、複数Session、課金、永続化は対象外。

## 基本試験

### 起動

```bash
cp .env.example .env
make up
make ps
```

管理画面は `http://127.0.0.1:8080`。

### テスト入力とローカル出力

別ターミナルで次を実行する。

```bash
make publish
make preview
```

期待結果:

- 起動直後は待機映像
- publisher開始後、約3秒でテスト映像へ切替
- 440Hzのテスト音が聞こえる
- UIが `LIVE`、入力映像・音声受信中、出力接続中になる

### ミュート

UIの「音声をミュート」を押すか、次を実行する。

```bash
make mute
```

期待結果:

- 映像は止まらない
- 出力URLへの接続を作り直さない
- 音声だけが無音になる
- UIのdesired / actualがMUTEDへ収束する

解除:

```bash
make unmute
```

### 入力切断と復帰

1. `make publish` をCtrl+Cで停止する
2. 10秒、30秒、2分の各時間待つ
3. `make publish` を再実行する

期待結果:

- 約1.5秒で待機映像へ切り替わる
- ローカル出力の接続は維持される
- 再入力後、約3秒の安定確認を経て通常映像へ戻る
- 切断前にMUTEDなら復帰後もMUTEDのまま

## OBS入力

- サービス: Custom
- Server: `rtmp://<server>:1935/live`
- Stream key: `input`
- 映像: H.264、720p30または1080p30
- 音声: AAC、48kHz stereo
- Keyframe interval: 2秒

PoCを別端末から試す場合は `.env` の `IRLIGHT_BIND_ADDRESS=0.0.0.0` と `IRLIGHT_PUBLIC_HOST=<サーバーのLAN IP>` を設定する。インターネットへ直接公開しない。

## SRT入力

```text
srt://<server>:8890?streamid=publish:live/input&pkt_size=1316
```

SRT publisherはMPEG-TS、H.264 + AACを使用する。

## 外部RTMP/RTMPS出力

`.env` へ完全な送出先を設定する。

```dotenv
IRLIGHT_OUTPUT_URL=rtmps://<destination>/<application>/<stream-key>
```

変更後:

```bash
make down
make up
```

注意:

- Phase 0では環境変数でSecretを渡すため、本番方式ではない
- `docker compose config` やホスト管理権限を持つ利用者から見える
- 実験専用キーを使い、終了後にローテーションする
- ログへURL全体を出さない実装だが、外部サービス側のログは別途確認する

## 外部配信先の合格基準

| 試験 | 合格条件 |
|---|---|
| 初回接続 | 配信枠が開始し、待機映像が表示される |
| 10秒入力断 | 配信枠が終了せず、待機映像へ移る |
| 30秒入力断 | 配信枠が終了せず、再入力後に復帰する |
| ミュート | 映像継続、無音化、解除後の音声復帰 |
| 再接続 | 同じ入力URLで復帰し、二重publisherにならない |
| 2時間 | A/V syncの目立つずれ、process crash、メモリ増加がない |

## 計測項目

- publisher停止から待機映像まで
- publisher再開から通常映像まで
- ミュート要求からactual=MUTEDまで
- end-to-end追加遅延
- Continuity containerのCPU / memory
- ingress / egress bitrate
- A/V sync drift
- process restart回数

結果は `poc/measurements/` にJSONまたはCSVで保存する。個人情報、位置情報、配信キーは保存しない。

## 障害確認

```bash
make logs
```

PoCはエラー本文にURIやstream keyが含まれる可能性を避けるため、GStreamer errorはdomainとcodeだけを記録する。詳細調査で一時的にdebug logを有効化する場合も、実験用Secretだけを使う。

## 終了

```bash
make down
```
