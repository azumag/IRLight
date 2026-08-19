# Continuity HOLDING lifecycle

Issue #4 の最初の縦切りとして、入力断後の保持期限と安全終了の責務を Control Plane の永続 Session state に固定する。

## 状態遷移

```text
LIVE
  ↓ ingest offline
HOLDING
  ├─ ingest recovered before deadline → LIVE
  └─ hold deadline exceeded → STOPPING → FINISHED
```

`STOPPING` 以降は ingest observation で active state へ戻さない。stop と reconnect が競合した場合は stop を優先する。

## hold deadline

既定の保持時間は reaper の `hold_timeout_seconds`（現在 1800 秒）である。

既存実装では Session が `HOLDING` になっても `hold_deadline_at` が未設定のケースがあり、その場合 reaper が永遠に停止条件を満たさない問題があった。

reaper は `HOLDING` Session に deadline が無い場合、次の順で保持開始時刻を復元する。

1. `last_ingest_at`
2. `updated_at`
3. reaper の現在時刻（古いstateに時刻情報が無い場合のみ）

そして `hold_started_at + hold_timeout_seconds` を `hold_deadline_at` として永続化する。これにより reaper / Control Plane が再起動しても新しい保持時間を付与しない。

## events

保持期限の復元・終了は Session event stream に残す。

- `session.holding` / `INGEST_DISCONNECTED`
- `session.stopping` / `HOLD_TIMEOUT`
- `session.finished` / `HOLD_TIMEOUT`

入力待ち期限で終了する場合は `NO_INGEST_TIMEOUT` を使用する。

## idempotency

- `hold_deadline_at` は一度永続化したら同じ HOLDING cycle 内では再計算しない。
- `session.holding` は deadline を初めて補完した sweep だけで記録する。
- `STOPPING` 以降の late ingest observation は SessionStore が無視する。
- provider cleanup は既存 reaper と同様に再実行可能である。

## follow-up

このPRは HOLDING timeout / stop race / restart persistence を対象とする。Issue #4 の残りとして、Continuity Engineの `DEGRADED` 正式状態、pipeline crash/restart復元、Control Planeとの desired/actual reconcile、長時間切断E2Eを後続で扱う。
