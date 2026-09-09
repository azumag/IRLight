# Media Node availability alert dry-run

Issue #11 の Critical alert `MEDIA_NODES_ALL_UNAVAILABLE` を、canonical Node authority から read-only に検証するための dry-run 手順です。外部監視製品、provider API、通知先、Session cleanup には接続しません。

## 判定契約

`media_nodes.available_count` は、この dry-run では次をすべて満たす Node の aggregate 件数として扱います。

- `desired_state=RUNNING`
- `status=READY`
- heartbeat age が `NODE_HEARTBEAT_GRACE_SECONDS` 未満

件数が `0` の場合に `MEDIA_NODES_ALL_UNAVAILABLE` を1件だけ `MATCHED` として返します。Node authority が正常に初期化済みだが Node が0件の場合も available count は0です。これは「実行環境に稼働可能な Media Node がない」という signal 契約をそのまま表します。

`BOOTSTRAPPING`、`STOPPING`、`STOPPED`、`FAILED` は available と数えません。heartbeat が新しくても `READY` でなければ配信を受けられる状態と推測しません。逆に `READY` でも heartbeat が stale なら available と数えません。

heartbeat の stale 境界と authority reader は `node_heartbeat_inspect_cli.py` を再利用します。そのため delayed-heartbeat warning と Critical availability alert で grace の意味が分岐しません。

## 実行

repository root から:

```bash
python apps/control-api/operations_node_availability_alerts.py \
  --node-state-dir /path/to/state
```

必要なら既存と同じ環境変数で grace を変更します。

```bash
NODE_HEARTBEAT_GRACE_SECONDS=120 \
  python apps/control-api/operations_node_availability_alerts.py \
  --node-state-dir /path/to/state
```

この evaluator は現時点では repository checkout から実行する運用補助ツールです。Control API image の明示的な source packaging には追加していないため、image 内に存在すると仮定しません。

## 出力と exit code

出力は JSON 1件です。repository 管理下の alert ID と aggregate 件数だけを返し、`node_id`、`session_id`、provider server ID、boot ID、token digest、raw authority は表示しません。

- `0`: authority と catalog を検証できた。alert が一致した場合も dry-run 自体は正常終了
- `3`: Node authority を安全に検証できない
- `4`: alert catalog の severity / signal / threshold / runbook 契約が evaluator と一致しない

例:

```json
{"available_nodes":0,"inspected_nodes":2,"matched_alerts":{"MEDIA_NODES_ALL_UNAVAILABLE":1},"status":"MATCHED","violations":{}}
```

authority が欠損・破損・marker 不整合などで読めない場合は `UNAVAILABLE` を返し、available count が0だったと推測して Critical alert を確定しません。部分的に読めた値から recovery も推測しません。

## 安全境界

この command は Node authority を read-only で検査します。実行中に state directory、lock、marker、JSON を作成・修復しません。また次を行いません。

- Node / VPS の作成、再起動、停止、削除
- Reaper / cleanup の実行
- Session の割当変更、停止、failover
- Discord / email 等への通知
- provider credential や stream key の読出し・表示
- 課金、refund、補填

`MATCHED` は自動復旧命令ではありません。`docs/operations/media-node-heartbeat-stopped.md` に従い、authority 障害、Control Plane / network 障害、意図した drain / deploy、個別 Node 障害を切り分けてから操作します。

## テスト

`tests/test_operations_node_availability_alerts.py` では、fresh READY Node、stale Node、terminal Node、BOOTSTRAPPING、empty authority、catalog drift、secret/identifier 非露出、authority 欠落、read-only 性を回帰確認します。

本番 alert routing を接続する場合も、この dry-run の aggregate 判定をそのまま通知 payload とみなさず、environment / region の routing、dedup、復旧通知、collector freshness を別途定義してください。