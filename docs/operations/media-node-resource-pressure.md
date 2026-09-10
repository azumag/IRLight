# Media Node resource pressure / exhaustion

`NODE_RESOURCE_PRESSURE` Warning と `NODE_RESOURCE_EXHAUSTED` Critical の一次対応 runbook です。CPU、memory、disk I/O、network 等のどれを `media_nodes.resource_pressure` として採用するかは collector / deployment 契約で明示し、この runbook では不明な metric を推測しません。resource exhaustion event も、reason code と実測 evidence を確認するまで具体的な資源種別を決め打ちしません。

## 検知

alert catalog の契約は次の通りです。

- `NODE_RESOURCE_PRESSURE`
  - severity: `warning`
  - signal: `media_nodes.resource_pressure`
  - trigger: threshold ref `operations.node_resource_pressure`
- `NODE_RESOURCE_EXHAUSTED`
  - severity: `critical`
  - signal: `media_nodes.resource_exhausted`
  - trigger: event `NODE_RESOURCE_EXHAUSTED`
- 共通 dedup: `environment`, `node_id`, `reason_code`

pressure threshold の実数、metric の単位、aggregation window は production deployment 側で定義します。pressure threshold の repository dry-run は `docs/operations/node-resource-pressure-alert-dry-run.md` を参照してください。`NODE_RESOURCE_EXHAUSTED` event は event-trigger dry-run の対象です。

alert を受けたら、まず routing が示す environment / Node、reason code、collector / event producer の contract を確認します。Node ID や reason code は運用上の相関情報として扱い、credential、stream key、SRT passphrase、Authorization header、provider secret を通常ログや issue へ転記しません。

## 影響判定

最初に次を read-only で確認します。

1. Warning の場合、`media_nodes.resource_pressure` が何の metric・単位・window を表す deployment か。Critical event の場合、producer がどの resource exhaustion を検出したか。
2. 同一 Node の heartbeat が fresh か、`status` / `desired_state` が想定どおりか。
3. その Node に active / reserved Session が存在するか。ただし authority が読めない場合は「0件」と推測しない。
4. ingest / egress failure、Session crash loop、capacity warning が同時発生していないか。
5. Warning なら pressure が単発 spike か window をまたいで継続しているか。Critical なら既に process / Session failure が発生しているか。
6. 同 region の複数 Node へ広がっているか、単一 Node に限定されているか。

heartbeat や canonical Node authority の read-only 確認は `media-node-heartbeat-stopped.md`、Node capacity は `media-node-capacity-high.md`、Session crash loop は `session-process-crash-loop.md` を併用します。

## Read-only 切り分け

collector / host の観測手段が既に承認されている場合に限り、alert の `reason_code` と metric / event definition に対応する evidence を取得します。例として CPU、memory、disk、network のメトリクスがあり得ますが、alert contract にない metric を代替値として扱いません。

記録する内容は aggregate / operational evidence に限定します。

- 観測時刻と aggregation window、または event 発生時刻
- Warning の場合は threshold と同一単位の current / recent trend
- Critical event の場合は event producer が示す安全な reason code と発生回数
- Node lifecycle (`status`, `desired_state`) と heartbeat freshness
- active / reserved Session の aggregate 件数
- 同時発生 alert ID と aggregate 件数
- deploy / configuration change の有無と commit / release identifier

次は行いません。

- credential や destination secret の dump
- Session payload / user content の収集
- provider API への create/delete/resize 呼び出し
- Node restart / drain / stop
- process kill、filesystem cleanup、cache drop
- authority file の修復、marker 削除、空 state 作成

## 緩和判断

resource pressure Warning だけを根拠に自動で Node を再起動・停止・増設しません。`NODE_RESOURCE_EXHAUSTED` Critical でも、resource 種別、影響中 Session、authority、failover 可否を確認せず destructive action を開始しません。まずユーザー影響と継続性を確認し、最小の可逆な対応を選びます。

### 既存 Session への影響がない場合

- 一過性 Warning spike なら、同じ aggregation window を複数回観測して自然回復を確認します。
- Critical event でも、既に解消しているか継続中かを event / resource evidence で確認します。
- 直前 deploy / configuration change と相関する場合は `deploy-rollback.md` の判定条件に従います。
- capacity 増強が必要に見える場合も、Node shape 変更や provider resource 追加は課金・capacity 判断になるため自動実行しません。必要な evidence と想定影響を issue / incident record にまとめます。

### 既存 Session へ影響がある場合

- ingest / egress / crash-loop 等、実際に失敗している経路の専用 runbook を優先します。
- authority が不明な状態で Session を別 Node へ二重割当しません。
- Node drain / restart / stop が必要な場合は、対象 Session と failover / continuity の成立を確認した明示的な運用判断として実施します。

### Node 全体へ広がっている場合

`MEDIA_NODES_ALL_UNAVAILABLE`、capacity、Control Plane、datastore 等の alert と合わせて共有原因を疑います。単一 Node の resource 対応を全 Node に機械的に適用しません。

## 復旧確認

復旧は Warning が一度 threshold を下回ったことや Critical event が一度途切れたことだけで確定しません。少なくとも deployment が定義する観測期間で次を確認します。

- Warning: `media_nodes.resource_pressure` が threshold 以下へ戻り、複数 window で再上昇していない。
- Critical: `NODE_RESOURCE_EXHAUSTED` event が再発せず、対応する resource evidence が正常化している。
- heartbeat / Node lifecycle が正常。
- 新規 Session と既存 Session の ingest / egress に異常増加がない。
- Session crash loop / failure surge が収束している。
- capacity warning や関連する Critical alert が残っていない。

metric / event source や authority が読めない場合は recovery と推測せず、observability / Control Plane 側の障害として切り分けを継続します。

## 事後作業

incident record には secret を含めず、少なくとも次を残します。

- alert ID、environment / Node の運用識別子、reason code
- Warning の metric definition、単位、aggregation window、threshold、または Critical event producer の contract
- pressure / exhaustion の開始・peak / event・復旧時刻とユーザー影響
- 直前の deploy / configuration change との相関
- 実施した変更操作と rollback 可否
- false positive / false negative の疑い

同種の pressure / exhaustion が繰り返す場合は、collector / event metric の定義、threshold/window、Node sizing、workload 分散、capacity planning を別 issue で見直します。provider の resize / scale-up、追加課金、重大な Session 配置変更はこの runbook だけで自動化しません。
