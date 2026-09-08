# Runbook: Media Node heartbeat stopped

Issue #11 の「Media Node heartbeat停止」を、最初の切り分けから復旧確認まで安全に進めるためのrunbook。Node heartbeat lifecycleの仕様自体は `docs/node-heartbeat-lifecycle.md` を参照する。

この手順の最初の診断は **read-only** で行う。Node authorityを欠損時に作り直したり、最初からReaper・provider cleanup・VM再作成を実行したりしない。authority障害と本当のNode停止を混同すると、稼働中Sessionを誤って終了させる可能性があるためである。

## 検知条件

`NODE_HEARTBEAT_GRACE_SECONDS`（既定120秒）を超えて、`desired_state=RUNNING` かつ terminal (`STOPPED` / `FAILED`) ではないNodeのheartbeatが更新されない場合をheartbeat staleとして扱う。

Control API imageにはread-only inspectorを同梱する。

```bash
python /app/node_heartbeat_inspect_cli.py
```

PoC compose上では次でも実行できる。

```bash
docker compose -f docker-compose.poc.yml exec -T control-ui \
  python /app/node_heartbeat_inspect_cli.py
```

exit codeは次の通り。

- `0`: authorityを検証でき、staleなexpected-running Nodeはない
- `2`: authorityを検証でき、staleなexpected-running Nodeが1件以上ある
- `3`: Node authorityを安全に検証できない

JSON出力には `node_id` / `session_id` / status / desired state / heartbeat ageだけを含め、bootstrap token、Node access token digest、provider server ID、boot IDなどは出力しない。heartbeatを一度も受けていないNodeは `created_at` からの経過を `registration_age_seconds` として表示する。未来時刻はage 0にclampし、clock correctionだけでstale判定しない。

## 確認コマンド

### 1. authorityが読めることを確認する

inspectorがexit `3` の場合、Node停止と決めつけない。まずControl Plane全体のauthority readinessを確認する。

```bash
python /app/state_inspect_cli.py
```

PoC composeの場合:

```bash
docker compose -f docker-compose.poc.yml exec -T control-ui \
  python /app/state_inspect_cli.py
```

`nodes` authorityが `UNAVAILABLE` の場合は、欠損・破損・mount不備を先に扱う。`nodes.json` や initialization markerを手作業で空ファイルへ置換して復旧扱いにしてはいけない。

### 2. stale Nodeと影響Sessionを特定する

inspectorの `nodes[]` から `stale=true` の `node_id` と `session_id` を控える。token hashやsecretをインシデント記録へコピーしない。

Node hostへアクセスできる場合は、まずread-onlyな状態確認を行う。

```bash
docker compose -f docker-compose.node.yml ps
```

必要な範囲だけ直近ログを確認する。

```bash
docker compose -f docker-compose.node.yml logs --since 10m node-agent
```

ログを外部へ共有するときはstream key、SRT passphrase、認証token、destination credentialが含まれていないことを再確認する。

### 3. 意図した停止かを確認する

`desired_state=STOPPED` または既にterminalなNodeはheartbeat alert対象ではない。drain / stop / deploy中のNodeを障害として再起動しない。

`desired_state=RUNNING` のNodeだけを障害候補として扱い、同じ時間帯のhost restart、network断、OOM、Node Agent process exit、Control Plane到達不能を確認する。

## 影響判定

- stale Nodeに紐づく `session_id` を影響候補とする。
- heartbeatだけが停止しmedia processが動いている可能性と、Node全体が停止している可能性を区別する。
- 複数Nodeが同時にstaleなら、個別processよりControl Plane経路、共通network、host/provider障害を優先して疑う。
- 全expected-running Nodeが同時にstaleでも、authority `UNAVAILABLE` は「全Node停止」の証拠にしない。

Reaperのlifecycle判定では、通常は `last_heartbeat_at`、heartbeat未受信時はSession側の `node_registered_at` を基準にする。このinspectorはoperator向けの早期診断であり、Session cleanupのsource of truthを置き換えない。

## 暫定対応

原因を確認する前に次を行わない。

- `nodes.json` の削除・初期化
- `docker compose down -v`
- volume prune / system prune
- provider resourceの手動削除
- Reaperの連打
- 同じSessionへ別Nodeを手動で二重割当

Node Agentだけが停止し、hostとstateが正常で、そのNodeを継続利用する判断ができた場合は、通常のservice manager / compose運用に従ってNode Agentを復帰させる。VM再作成、provider削除、進行中Sessionの強制終了が必要なら、影響範囲を記録してから実施する。

原因がControl Plane到達不能なら、Nodeを再作成する前にControl Plane/networkを復旧する。heartbeat送信経路が壊れたままNodeを増やしても同じstaleを増やすだけである。

## 復旧確認

復旧後、同じinspectorを再実行する。

```bash
python /app/node_heartbeat_inspect_cli.py
```

最低限、以下を確認する。

1. exit codeが `0`
2. 対象Nodeの `stale=false`
3. `heartbeat_age_seconds` が継続的に小さい値へ更新される
4. 対象Sessionのingest / egress状態が期待値へ戻っている
5. cleanup済みSessionや意図的に停止したNodeを誤って再開していない

単発でexit `0` になっただけでは復旧完了とせず、少なくとも複数heartbeat周期で更新が継続することを確認する。

## 事後作業

インシデント記録には、secretを含めず次を残す。

- 検知時刻・復旧時刻
- 影響した `node_id` / `session_id`
- 最大heartbeat age
- Node / host / network / Control Planeのどこで失敗したか
- Reaperが `NODE_SHUTDOWN` を確定したか
- Sessionへの実影響とユーザー案内の要否
- 再発防止のissue / test / alert改善

複数Node同時停止、繰り返すheartbeat遅延、または原因不明のauthority `UNAVAILABLE` は、個別Nodeの再起動だけでcloseせず、共通原因を追う。
