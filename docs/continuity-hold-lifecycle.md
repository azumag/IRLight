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

既定の保持時間は reaper の `hold_timeout_seconds`（現在 1800 秒）。`HOLDING` なのにdeadlineが未設定の場合、reaperは `last_ingest_at`、次に `updated_at`、最後に現在時刻から保持開始時刻を復元し、deadlineを永続化する。

同じHOLDING cycleではdeadlineを再計算しないため、reaper / Control Plane再起動で保持時間が延長されない。

## events

- `session.holding` / `INGEST_DISCONNECTED`
- `session.stopping` / `HOLD_TIMEOUT`
- `session.finished` / `HOLD_TIMEOUT`
- 入力待ち期限では `NO_INGEST_TIMEOUT`

## follow-up

このPRはHOLDING timeout / stop race / restart persistenceを対象とする。Issue #4 の残りとして正式なDEGRADED Session state、pipeline crash/restart復元、desired/actual reconcile、長時間切断E2Eを後続で扱う。
