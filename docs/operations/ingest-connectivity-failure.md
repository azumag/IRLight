# Ingest 接続不能の read-only 切り分け

Issue #11 の runbook のうち、配信クライアントが Media Node へ publish できない、または Control Plane 上で ingest が確認できない場合の **read-only 切り分け**を定義する。

この手順は原因を安全に分類するところまでを対象とする。publisher の kick、MediaMTX / Node Agent の restart・recreate、credential rotation、DNS/TLS 変更は自動で行わない。stream key、SRT passphrase、認証 token、credential-bearing URL を出力・記録しない。

## 1. 障害範囲を先に分ける

次の順で確認する。

1. Media Node heartbeat が止まっている場合は [Media Node heartbeat 停止](./media-node-heartbeat-stopped.md) を先に使う。
2. `mediamtx` 自体の停止・restart/OOM が疑われる場合は [Session 実プロセスの crash loop](./session-process-crash-loop.md) を使う。
3. RTMPS だけ失敗し、RTMP/SRT は正常な場合は [RTMPS certificate update failure](./rtmps-certificate-update-failure.md) を併用する。
4. Node と `mediamtx` が動作しているのに publisher が見えない場合、本 runbook の ingest inspector で node-internal path state を確認する。

外部クライアントの「接続できない」という症状だけでは、network/TLS/auth/path mismatch/media policy のどこで失敗したかは確定しない。原因不明のまま service を再起動しない。

## 2. read-only ingest inspector

Node Agent image には `/opt/irlight/ingest_failure_inspect_cli.py` を含める。inspector は Node Agent が通常利用する `NODE_MEDIAMTX_API_URL` の node-internal MediaMTX path API に **GET を1回だけ**行い、`NODE_INGEST_PATH` の現在状態を確認する。

出力するのは次の redacted 情報だけである。

- `status`: `ONLINE` / `OFFLINE` / `UNAVAILABLE`
- fixed `reason`
- publisher が online か
- source protocol: `RTMP` / `RTMPS` / `SRT` / `OTHER`
- track 数

source ID、MediaMTX API URL、path 名、track metadata、API error body は出力しない。inspector は public ingest port へ接続せず、publisher を kick せず、MediaMTX state を変更しない。API response は 1 MiB まで、timeout は 0.2〜10秒へ制限して診断コマンド自体の待機も有界にする。

運用シェルから Node Agent 内で実行する例:

```sh
docker compose \
  -f docker-compose.node.yml \
  -f docker-compose.node.public.yml \
  exec -T node-agent \
  python3 /opt/irlight/ingest_failure_inspect_cli.py
```

RTMPS overlay を使う構成では、実 deployment と同じ compose file の組を指定する。このコマンドは Node Agent container 内に追加の診断 process を起動するだけで、media service の start/stop/recreate は行わない。

exit code / reason は次のとおり。

| Exit | Status | Reason | 意味 |
| --- | --- | --- | --- |
| `0` | `ONLINE` | `INGEST_PUBLISHER_ONLINE` | 対象 path に現在 publisher が存在する。接続不能が継続して見える場合は client/session の取り違えや接続後の media policy を確認する。 |
| `2` | `OFFLINE` | `INGEST_NO_PUBLISHER` | 対象 path は見えているが現在 publisher が online ではない。 |
| `2` | `OFFLINE` | `INGEST_PATH_NOT_VISIBLE` | 対象 path が MediaMTX path list に現れていない。publisher 未接続または path/config の不一致を疑うが、これだけで config 破損と断定しない。 |
| `3` | `UNAVAILABLE` | `INGEST_INSPECTION_UNAVAILABLE` | node-internal MediaMTX API の応答を安全に取得・解釈できず判定不能。 |

`OTHER` source protocol は将来の MediaMTX source type をそのままログへ出さないための redaction であり、それ自体を障害と断定しない。

## 3. `OFFLINE` の切り分け

### publisher 側

- 対象 Session に発行された **現在の** ingest protocol / host / port / path を client が使っているか確認する。
- credential を比較する場合も値そのものを Issue / shell history / log へ貼らない。
- 同じ credential の失敗を繰り返して temporary lockout を悪化させない。認証失敗が疑われる場合は `docs/ingest-auth-abuse-protection.md` の failure-window / lockout 境界を確認する。
- SRT は streamid と passphrase、RTMP/RTMPS は username/password の境界を混同しない。

### Node / listener 側

`media_stack_inspect_cli.py` で `mediamtx` が running かを確認する。RTMPS のみなら certificate runbook も確認する。

public port の疎通確認が必要な場合は、credential を付けず TCP/TLS handshake の層までに留める。実際の publish 試験はテスト用 credential / stream で行い、本番 stream key をコマンド履歴や incident note に残さない。

### `INGEST_PATH_NOT_VISIBLE`

この reason は「publisher が到達していない」と「MediaMTX がその path を現在 materialize していない」を区別できない。次を確認する。

- Session が prepare 済みで、client が別 Session / 古い endpoint を参照していないか;
- `NODE_INGEST_PATH` と deployment の MediaMTX path 設定が一致しているか;
- 直近 deploy/config change があったか。

ここで path を推測して書き換えたり、`all_others` 等へ緩和して認証境界を回避しない。

## 4. `ONLINE` なのに配信が成立しない場合

`ONLINE` は MediaMTX が publisher を認識していることだけを示す。映像・音声が利用可能、egress が正常、Session 全体が healthy であることまでは示さない。

次を別々に確認する。

- ingest policy / quality observation の codec、resolution、bitrate、FPS/GOP/timestamp 判定;
- continuity が入力を取得できているか;
- egress のみ失敗していないか。複数 Session で egress failure が偏る場合は [egress widespread failure](./egress-widespread-failure.md) を使う;
- client が再接続中の場合、古い publisher と新しい接続を取り違えていないか。

inspector は source ID を意図的に出さないため、publisher 個体の強制切断や identity 確認には使わない。そのような操作は影響を伴うため、Session authority と対象を確定してから別の明示的な運用判断として行う。

## 5. Recovery

原因別に最小限の復旧を選ぶ。

- **client endpoint / credential の取り違え**: Control Plane が現在発行している値へ client 設定を直す。credential 値は記録しない。
- **temporary auth lockout**: lockout の有効期間と原因を確認し、同じ誤 credential の retry を止める。安全境界を無効化しない。
- **RTMPS certificate**: certificate runbook に従う。TLS verification を無効化しない。
- **MediaMTX crash/OOM**: crash-loop runbook で原因を分類する。原因不明の restart loop を作らない。
- **deploy/config regression**: production preflight と既知正常 version を確認し、rollback は進行中 Session への影響を評価したうえで別途実施する。

復旧後は inspector が `ONLINE` になったことだけで完了とせず、ingest quality、continuity、egress、Session lifecycle をそれぞれ確認する。

## 6. Incident note に残すもの

secret を含めず、最低限次を記録する。

- Session / Node ID;
- 発生時刻と client protocol;
- inspector の `status` / `reason` / `source_protocol` / `track_count`;
- heartbeat と media-stack inspector の状態;
- RTMPS の場合は certificate checker の fixed code;
- 直近 deploy/config change の有無;
- 実施した復旧操作と、復旧後の ingest / continuity / egress / Session 確認結果。

「何件で ingest 障害と alert するか」「通知先」「自動 restart の可否」は利用規模・運用体制に依存するため、この runbook では新しい閾値や自動復旧 policy を決めない。
