# Egress 大量失敗の切り分け

Issue #11 の Release 1 運用 runbook のうち、複数の `DIRECT_PUSH` Session で外部配信先への接続失敗・再接続・terminal failure が同時期に増えた場合の **read-only 切り分け**を定義する。

この手順では配信先へ新しい接続試験を行わず、既存の Egress Gateway が `/state/egress.json` に保存した redacted status だけを読む。service の restart / recreate、secret の読出し・差し替え、TLS 検証の無効化、追加 Node の作成は行わない。

`RELAY_ONLY` Session は IRLight から外部配信先へ direct push しないため、この runbook の対象外である。relay reader 側の障害として切り分ける。

## 1. 先に障害範囲を分ける

- Media Node heartbeat 自体が止まっている場合は [Media Node heartbeat 停止](./media-node-heartbeat-stopped.md) を先に使う。
- `mediamtx` / `continuity` の停止や再起動が疑われる場合は [Session 実プロセスの crash loop](./session-process-crash-loop.md) で media stack を確認する。
- heartbeat と ingest が継続しており、`egress` だけが `RECONNECTING` / `AUTH_FAILED` / `FAILED` へ偏っている場合に本手順を使う。

「大量」の固定閾値はこの runbook では決めない。Session 数・利用規模に依存する運用ポリシーだからである。少なくとも複数の独立 Session / Node で同じ時間帯に同じ固定 reason code が観測された場合は、共通要因を疑う材料として扱うが、それだけで配信先障害や IRLight 障害と断定しない。

## 2. read-only egress inspector

Node Agent image の `/opt/irlight/egress_failure_inspect_cli.py` を使う。既定では `NODE_EGRESS_STATUS_FILE`、未指定なら `/state/egress.json` を読む。

実 deployment と同じ Compose file の組を指定して、対象 Node で次を実行する。

```sh
docker compose \
  -f docker-compose.node.yml \
  -f docker-compose.node.public.yml \
  exec -T node-agent \
  python3 /opt/irlight/egress_failure_inspect_cli.py
```

RTMPS overlay を使用している場合は実 deployment と同じ overlay を追加する。inspector 自体は Docker API を呼ばず、status file を読むだけである。

出力例:

```json
{
  "action_code": "CHECK_DESTINATION_DNS",
  "attempt": 3,
  "destination_host": "live.example",
  "destination_scheme": "rtmps",
  "egress_status": "RECONNECTING",
  "next_retry_in_seconds": 4.2,
  "observed_at": 1788840000.0,
  "reason_code": "DNS_FAILED",
  "status": "RETRYING"
}
```

出力する reason は IRLight が定義した allowlist の固定 code だけである。未知文字列は `UNCLASSIFIED` に置き換え、生の GStreamer error、URL、stream key、credential は出力しない。

exit code:

| code | 意味 |
| --- | --- |
| `0` | `CONNECTED` かつ接続済み |
| `1` | `STARTING` / `RECONNECTING` / `STOPPED`。確認または経過観察が必要 |
| `2` | `AUTH_FAILED` / `FAILED`。terminal failure |
| `3` | status 欠落・破損・stale・矛盾で安全に判定不能 |

terminal status は再接続中の status と違い age timeout で自動的に消さない。古い terminal record の可能性もあるため、`observed_at` と現在の Session desired state を必ず照合する。

## 3. action code ごとの確認

| action code | 主に確認するもの |
| --- | --- |
| `CHECK_DESTINATION_CREDENTIAL` | credential が失効・変更されていないか。値そのものはログや Issue に貼らない |
| `CHECK_PUBLISH_CONFLICT` | 同じ publish name / key を別 publisher が使用していないか |
| `CHECK_DESTINATION_POLICY` | 配信先が publish を拒否しているか。推測で provider 固有 reason に変換しない |
| `CHECK_DESTINATION_TLS` | 証明書期限・hostname・trust chain。検証を無効化して復旧扱いにしない |
| `CHECK_DESTINATION_DNS` | 同一 hostname を使う複数 Session / Node で同時発生しているか |
| `CHECK_DESTINATION_NETWORK` | timeout / unreachable が Node・destination のどちらへ偏っているか |
| `CHECK_SECRET_DELIVERY` | Session の secret 配送状態。secret file の中身を表示しない |
| `CHECK_LOCAL_MEDIA_PATH` | Egress Gateway の入力側 relay、pipeline、必要 plugin。必要なら crash-loop runbook を併用する |
| `CHECK_RETRY_EXHAUSTION` | retry 上限到達前の固定 reason と発生時刻 |
| `CHECK_EGRESS_STATUS_SOURCE` | status file の欠落・破損・stale。判定不能のまま restart に進まない |
| `CONFIRM_SESSION_DESIRED_STATE` | `STOPPED` がユーザー停止・終了後の正常状態か、意図しない停止か |

## 4. 複数 Session / Node の相関

各対象で inspector の JSON を取得し、secret を含まない次の項目だけを incident note に集める。

- Session / Node ID
- `observed_at`
- `egress_status`
- `reason_code`
- `action_code`
- destination の scheme / hostname
- retry attempt
- 直近 deploy / config 変更の有無

同じ destination hostname で `DNS_FAILED` / `TLS_FAILED` / `UNREACHABLE` が集中していれば destination 側または経路側の共通障害を疑う。複数の異なる destination で同時に同じ失敗が出る場合は Node / network / deploy 側も確認する。いずれも相関であり原因確定ではない。

`AUTH_FAILED` / `PUBLISH_CONFLICT` / `PUBLISH_REJECTED` は terminal であり、機械的な restart loop に変換しない。credential の再発行や配信先設定変更は利用者・運用判断を伴うため、この read-only 手順から自動実行しない。

## 5. 禁止する復旧ショートカット

原因を確認する前に次を行わない。

```text
docker compose restart ...
docker compose up -d --force-recreate ...
docker compose down ...
docker compose down -v ...
TLS verification disable
stream key / secret の Issue・PR・ログへの貼り付け
追加 Node / instance の自動作成
```

外部配信先への実 publish test も、この runbook から暗黙には行わない。必要な場合はテスト用資格情報と明示された運用判断の下で別手順として実施する。

## 6. 復旧確認

復旧は一つの signal だけで判定しない。

1. inspector が最新 status で `CONNECTED` / exit `0` へ戻る。
2. Node heartbeat の `egress.connected` 相当観測が更新される。
3. ingest / continuity の health が独立に正常である。
4. Session lifecycle が期待状態にある。
5. 必要なら配信先側の実受信を、secret を共有せず確認する。

単に container が `running` へ戻った、または retry attempt が止まっただけでは復旧完了としない。

## 7. 残る運用課題

本 runbook は安全な一次切り分けまでを扱う。複数 Session を自動集計する metric / alert rule、通知先、重複抑制、「大量失敗」の正式な閾値は実運用データを基に Issue #11 で別途決める。ここでは新しい外部課金や provider API、secret を必要とする監視を勝手に導入しない。
