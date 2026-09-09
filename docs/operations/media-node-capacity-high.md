# Runbook: Media Node capacity high

Issue #11 の「Node capacity 80%超」warning と capacity 枯渇を、ユーザー単位の利用権上限と混同せず切り分けるための runbook。

ここでいう Media Node capacity は、scheduler / Node inventory が持つ **検証済みの node `max_sessions`** に対する、その Node の active / reserved Session の占有率を指す。ユーザーの `max_concurrent_sessions`、CPU 使用率、memory 使用率、network 使用率を代用してはいけない。

```text
capacity_ratio = (active_sessions + reserved_sessions) / max_sessions
warning = capacity_ratio > 0.80
```

`max_sessions` が未設定、0、古い、または authority / inventory を安全に検証できない場合は capacity 0 や無制限と推測せず、capacity signal を `UNAVAILABLE` として扱う。Issue #11 の 80% は警告境界であり、新しい Node の自動作成や既存 Session の停止条件ではない。

## 検知条件

`MEDIA_NODE_CAPACITY_HIGH` は、同一 Node の `media_nodes.capacity_ratio` が 0.80 を超えた場合の warning とする。alert catalog では `issue11.media_node_capacity_80_percent` を参照し、この runbook を一次対応先とする。

現時点の repository には、この ratio を本番 collector から自動供給する配線はまだない。collector 未接続時に推測値で alert を発火させない。Node heartbeat / assignment の read-only な状況確認には既存 inspector を利用できるが、この inspector 単体は capacity ratio を算出しない。

```bash
python /app/node_heartbeat_inspect_cli.py
```

PoC compose では次でも確認できる。

```bash
docker compose -f docker-compose.poc.yml exec -T control-ui \
  python /app/node_heartbeat_inspect_cli.py
```

## 確認手順

### 1. Node authority と heartbeat を確認する

inspector が authority を安全に検証できない場合、capacity 枯渇と断定しない。まず state readiness / Node authority の欠損・破損・mount 不備を確認する。

```bash
python /app/state_inspect_cli.py
```

Node が stale / terminal / draining の場合、単純な capacity 比率だけで新規割当可否を判断しない。意図的な drain と障害を分け、同じ Node を「空きあり」と数え直すために state を手編集しない。

### 2. capacity の分子と分母の出所を確認する

運用メトリクスまたは scheduler / Node inventory から、同じ時点・同じ Node の以下を確認する。

- `max_sessions`: 負荷試験等から決めた検証済み上限
- `active_sessions`: 実際に稼働中として capacity を占有する Session 数
- `reserved_sessions`: prepare / startup 中として予約済みの Session 数
- `capacity_ratio`: 上記 3 値から計算した比率
- Node の `draining` / health / software version

`max_sessions` が実測・設定されていない段階では、CPU core 数等からその場で上限を作らない。profile 別 weight や transcode / composited video を導入する場合は単純 Session 数では足りないため、scheduler の capacity model を先に更新する。

### 3. ユーザー利用権上限と分離する

`session_capacity_inspect_cli.py` が扱う `max_concurrent_sessions` は **ユーザー単位の entitlement capacity** であり、本 alert の Node capacity とは別物である。

```bash
python /app/session_capacity_inspect_cli.py --user-id "$USER_ID"
```

この結果が `CAPACITY_EXHAUSTED` でも Media Node が 80% 超とは限らない。逆にユーザー枠が空いていても Media Node capacity は逼迫し得る。ユーザー利用権の診断は `session-capacity-exhaustion.md` に従う。

## 影響判定

- 80% 超は **新規割当の余裕が小さい warning** であり、既存 Session を自動停止する理由ではない。
- 100% 以上、または reservation が上限を超えている場合は、新規 prepare / assignment の失敗・待ちが発生し得るため影響範囲を確認する。
- heartbeat 遅延、process crash、CPU / memory / network pressure が同時発生している場合は、それぞれの alert / runbook も参照し、capacity だけを根因と決めつけない。
- 複数 Node 構成では、1 Node の逼迫と region / 全体 capacity の逼迫を区別する。

## 暫定対応

原因確認前に次を行わない。

- 稼働中 Session の一律停止・強制移動
- `max_sessions` の無根拠な引上げ
- user entitlement の引上げ・消費解除
- provider resource / VM の自動作成・削除
- Session / Node authority の手編集
- `docker compose down -v`、volume prune、system prune

正常な Session 終了で reservation / active count が下がる場合は、その lifecycle を待って capacity が戻ることを確認する。追加 Node、より大きい shape、profile weight 変更が必要な場合は、外部課金・原価・scheduler 仕様に関わるため、明示的な運用 / 設計判断として issue または change record に残してから実施する。

## 復旧確認

対応後は同じ source of truth から再計測し、最低限以下を確認する。

1. Node authority / heartbeat が正常である
2. `max_sessions` の出所と値が有効である
3. `(active_sessions + reserved_sessions) / max_sessions <= 0.80` に戻る、または計画した capacity 変更後の新しい有効上限に対して余裕がある
4. reservation leak や二重 assignment がない
5. 既存 Session の ingest / egress が意図せず停止していない
6. 追加 Node を使った場合は provider 上の実リソース数と Control Plane ownership が一致する

単発で 80% 以下になっただけで完了とせず、少なくとも通常の prepare / cleanup 周期をまたいで再上昇しないことを確認する。

## 事後作業

secret を含めず、次を記録する。

- warning 開始・復旧時刻
- 対象 environment / region / node ID
- `max_sessions` の根拠・version
- peak active / reserved / capacity ratio
- 80% 超の継続時間
- prepare / assignment 失敗や利用者影響
- reservation leak、負荷増加、需要増加、drain 等の原因分類
- 追加 capacity が必要だった場合の原価・判断記録への参照
- collector / scheduler / load test / alert の改善 issue

Node capacity の実測上限自体が未確定なら、この alert を実運用へ接続する前に #8 / #13 の load test と scheduler capacity model を完成させる。推測した `max_sessions` で自動 provisioning や課金判断を行わない。
