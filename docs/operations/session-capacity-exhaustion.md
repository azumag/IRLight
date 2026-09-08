# Runbook: Session capacity exhaustion

Issue #11 の「capacity枯渇」のうち、Control Planeが現在強制している **ユーザー単位の同時Session利用枠** を安全に切り分けるためのrunbook。ここでいうcapacityは `max_concurrent_sessions` と、`SessionStore` が占有中と数えるSession数の差を指す。

このrunbookは、Media NodeのCPU・memory・disk・network、provider側の在庫、Nodeごとの `max_sessions`、Issue #11にある「Node capacity 80%超」のwarningを診断するものではない。それらは実測したNode capacityとNode inventory/metricsが必要であり、この手順で閾値を推測して代用しない。

最初の診断は **read-only** で行う。Session/entitlement storeを起動してlock fileやinitialization markerを作ったり、Sessionを停止したり、利用枠を変更したり、provider resourceを作成・削除したりしない。

## 検知条件

新規prepareが同時Session上限で拒否された、または運用上「利用枠が空かない」疑いがある場合に確認する。

Control API imageにはread-only inspectorを同梱する。

```bash
python /app/session_capacity_inspect_cli.py --user-id "$USER_ID"
```

PoC composeでは次でも実行できる。

```bash
docker compose -f docker-compose.poc.yml exec -T control-ui \
  python /app/session_capacity_inspect_cli.py --user-id "$USER_ID"
```

exit codeは次の通り。

- `0`: `CAPACITY_AVAILABLE`。少なくとも1枠利用可能
- `2`: `CAPACITY_EXHAUSTED` または `CAPACITY_DISABLED`
- `3`: `CAPACITY_UNAVAILABLE`。authorityを安全に検証できない

正常なJSON出力は `status`、`limit`、`occupied`、`available` の集計値だけを返す。指定したuser ID、Session ID、entitlement ID、state path、内部例外、stream key、tokenなどは出力しない。`CAPACITY_UNAVAILABLE` の場合だけ、安全な固定値 `authority=sessions` または `authority=entitlements` を返し、どちらのauthorityを先に復旧確認すべきかを示す。

占有数はprepare時の実装と同じく、対象ユーザーのSessionについて次のいずれかを満たすものを1件として数える。

- `entitlement_reserved=true`
- statusが active state、`STOPPING`、`FAILED_CLEANUP` のいずれか

`FINISHED` / `FAILED` は予約が残っていない限りcapacityを消費しない。永続entitlementがないユーザーは `IRLIGHT_DEFAULT_MAX_CONCURRENT_SESSIONS` のruntime defaultを使う。

## 確認手順

### 1. authorityが読めることを確認する

inspectorがexit `3` の場合はcapacity枯渇と断定しない。`authority=sessions` ならSession authority、`authority=entitlements` ならentitlement authorityの欠損・破損・mount不備を先に疑う。JSON本文やsecretを障害記録へ貼らず、volume/mountの存在と直前のdeploy・restore・disk障害を確認する。

`state_inspect_cli.py` は startup-mandatory authority のreadiness確認には利用できるが、lazy authorityである `sessions.json` / `entitlements.json` 自体は現在その対象外である。したがって `state_inspect_cli.py` が正常でも、このcapacity inspectorの `CAPACITY_UNAVAILABLE` を無視してはいけない。

```bash
python /app/state_inspect_cli.py
```

`CAPACITY_UNAVAILABLE` を「occupied=0」と読み替えて新しいSessionを作成してはいけない。初期化済みauthorityが消失している場合、inspectorはfail-closedする。authorityの復旧が必要なら、空ファイルを作るのではなく、既存のstate restore手順と有効なsnapshotを使う。

### 2. 集計値を確認する

`CAPACITY_AVAILABLE` なら同時Session利用枠そのものはprepare拒否の直接原因ではない。Destination、Secret、Node provisioning、ingest等の別reasonを確認する。

`CAPACITY_DISABLED` は上限が0であり、新規Sessionを許可しない設定である。設定が意図したものかを確認するが、障害対応だけを理由にその場で値を増やさない。

`CAPACITY_EXHAUSTED` は `occupied >= limit` を意味する。正当に進行中のSessionで埋まっている場合は正常な制限動作である。

### 3. 枠が戻らない理由を分類する

占有中Sessionが利用者の意図どおり継続中なら、既存Sessionを強制停止して枠を作らない。

終了操作済みなのに枠が戻らない場合は、`STOPPING` / `FAILED_CLEANUP` やcleanup pendingを疑う。provider resourceの所有権とSession authorityを照合し、既存のreaper/cleanup手順に従う。state JSONを直接編集して `entitlement_reserved=false` やterminal statusへ書き換えない。

同じユーザーで二重prepareが疑われる場合も、Sessionの冪等性・cleanup状態を確認し、別Sessionを作り直すことで問題を隠さない。

## 影響判定

- `CAPACITY_EXHAUSTED` はそのユーザーの **新規prepare** を制限する状態であり、既存Sessionを直ちに停止すべき状態ではない。
- 複数ユーザーで同時に発生していても、このCLIだけではNode/provider全体のcapacity不足を証明しない。
- `occupied > limit` は、limit引下げ、過去の予約、cleanup遅延等でも起こり得る。既存Sessionを機械的に停止して数を合わせない。
- `CAPACITY_UNAVAILABLE` はavailability 0でも無限でもなく「判断不能」である。authority復旧を優先する。

## 暫定対応

原因確認前に次を行わない。

- `sessions.json` / `entitlements.json` / initialization markerの削除・手編集
- `entitlement_reserved` の強制解除
- `STOPPING` / `FAILED_CLEANUP` の強制terminal化
- 利用権上限の無根拠な引上げ
- provider resourceの手動削除
- `docker compose down -v`、volume prune、system prune
- 既存Sessionを一律停止して枠を空ける操作

利用者が明示的に終了を希望したSessionなら、通常のstop/cleanup経路で終了し、cleanup完了後にcapacityが戻ることを確認する。cleanupが失敗している場合は、失敗理由とprovider所有権を確認してからreaper/復旧手順へ進む。

上限変更が必要に見える場合は、plan/entitlementの仕様・契約・原価に関わる判断として扱う。このrunbookやinspectorは自動で上限を変更しない。

## 復旧確認

原因となるSessionが正規のlifecycleで終了・cleanupされた後、同じinspectorを再実行する。

```bash
python /app/session_capacity_inspect_cli.py --user-id "$USER_ID"
```

最低限、以下を確認する。

1. authorityが検証でき、exit `3` ではない
2. 新規prepareを許可すべきユーザーでは `CAPACITY_AVAILABLE` へ戻る
3. `occupied` が実際の進行中/cleanup中Sessionと整合する
4. 既存の正常Sessionを誤停止していない
5. 必要なら実際のprepareを通常経路で1回だけ行い、二重Node/二重予約を作らない

## 事後作業

secretを含めず、次を記録する。

- 検知・復旧時刻
- `limit` / 最大 `occupied` / 復旧後 `available`
- 正常な同時利用、重複prepare、cleanup遅延、limit設定のどれが原因だったか
- `STOPPING` / `FAILED_CLEANUP` が関係した場合はそのreason code
- 利用者影響と補填判断の要否
- 再発防止のtest / alert / cleanup改善issue

Node/provider全体のcapacityを判断したい場合は、この結果だけを根拠にしない。Nodeごとの実測 `max_sessions`、active/reserved sessions、CPU/memory/network等を観測する仕組みと、負荷試験から決めたwarning閾値を別途整備する。