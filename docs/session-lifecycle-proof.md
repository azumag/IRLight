# Session lifecycle, workflow and reaper: verification checklist

## 目的

PR 3 (Session lifecycle / workflow / reaper) の完了条件を満たすため、
persistent Session state、prepare / stop API、idempotency、provisioning
workflow、compensation、scheduled reaper が fake provider 上で正しく動く
ことを確認する。

## 完了条件と対応テスト

| 完了条件 | テスト |
| --- | --- |
| double prepare で VPS が 1 台だけ | `test_double_prepare_creates_single_vps` |
| stop during provisioning で作成済み resource を回収 | `test_stop_during_provisioning_reclaims_resources` |
| workflow failure 後も reaper が回収 | `test_provisioning_timeout_fails_and_cleans` |
| server 削除後の volume 残留を検知・削除 | `test_reaper_cleans_orphans_for_unknown_session` |
| FAILED_CLEANUP を reaper が FAILED へ移行 | `test_reaper_finishes_failed_cleanup_sessions` |

## ローカルでの確認

```bash
python3 -m unittest discover -s tests -v
```

## Session API の動作確認（control plane を起動）

```bash
STATE_DIR=/tmp/irlight-sessions \
  python3 -m uvicorn app:app --app-dir apps/control-api --host 127.0.0.1 --port 8099
```

別ターミナルで:

```bash
SESSION_ID="$(uuidgen)"
curl -fsS -X POST "http://127.0.0.1:8099/v1/sessions/$SESSION_ID/prepare" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: test-1' \
  --data '{"user_id":"deadbeef","environment":"dev"}'
# status: READY_WAIT_INGEST, provider_server_id / provider_volume_id が返る

# 同じ Idempotency-Key / session id で再実行 → 同じ resource が返る
curl -fsS -X POST "http://127.0.0.1:8099/v1/sessions/$SESSION_ID/prepare" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: test-1' \
  --data '{"user_id":"deadbeef","environment":"dev"}'

# stop は冪等
curl -fsS -X POST "http://127.0.0.1:8099/v1/sessions/$SESSION_ID/stop"
curl -fsS -X POST "http://127.0.0.1:8099/v1/sessions/$SESSION_ID/stop"
# どちらも status: FINISHED
```

## Reaper の動作確認

reaper は control plane と同じプロセス内では動かさず、単体テストで clock
を注入して検証する（60分・12時間を実時間で待たない）。

```python
from reaper import Reaper, ReaperConfig
result = reaper.run()
# timeout_failures / deadline_stops / orphan_cleanup / failed_cleanup_retries
```

## 実機（ConoHa）での確認

fake provider で全異常系を通した後、ConoHa sandbox / internal alpha で
同じライフサイクルを確認する。provider を実 ConoHa に差し替えて:

1. double prepare → VPS が 1 台だけ（metadata 検索で冪等）
2. stop during provisioning → create 済み resource を回収
3. 手動で server を削除 → reaper が volume 残留を検知・削除
4. provider 一覧で残骸がないことを確認
