# Disk pressure monitoring

Issue #11 の disk full 監視に対して、ホストまたは永続 state filesystem の使用率を副作用なく確認するための運用チェックを定義する。

## Check

```bash
bash scripts/check-disk-pressure.sh /
```

監視対象を明示しない場合は `IRLIGHT_DISK_PATH`、`STATE_DIR`、`/state` の順で選ぶ。閾値は環境変数で変更でき、既定値は warning 80%、critical 90%。

```bash
IRLIGHT_DISK_WARNING_PERCENT=80 \
IRLIGHT_DISK_CRITICAL_PERCENT=90 \
  bash scripts/check-disk-pressure.sh "${STATE_DIR:-/state}"
```

出力は監視系が機械的に扱える固定プレフィックスと key/value に限定する。

```text
IRLIGHT_DISK_PRESSURE status=OK usage_percent=52 available_kb=123456 warning_percent=80 critical_percent=90
```

exit code は次の意味を持つ。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | warning 閾値未満 |
| 1 | `WARNING` | warning 以上、critical 未満 |
| 2 | `CRITICAL` | critical 以上 |
| 3 | `UNKNOWN` | path、閾値、`df` 実行または出力を安全に評価できない |

`UNKNOWN` を正常扱いしない。監視不能時は disk headroom を証明できないため、監視障害として通知する。

## Response

`WARNING` では `df -h` と `docker system df`、ログ、ビルドキャッシュ、生成物の増加傾向を確認し、何が容量を消費しているかを特定する。`CRITICAL` では新規 allocation を抑制できる運用があれば適用し、進行中 Session と authority state を保全したまま headroom を回復する。`UNKNOWN` は path/mount/permission/監視コマンドを確認し、値を推測して正常判定しない。

この check 自体はファイル、Docker image、container、volume、authority state を削除しない。自動復旧として `docker system prune --volumes`、`docker volume prune`、`docker compose down -v`、state/marker 削除を実行しない。cleanup が必要な場合は消費源と所有権を確認し、対象を限定した別の明示操作として扱う。

通知先や定期実行方式は deployment 固有のため、このスクリプトには外部 webhook やクラウド操作を組み込まない。systemd timer、監視 agent、既存 scheduler 等から exit code と固定出力を収集する。

## Validation

```bash
python -m unittest discover -s tests -p 'test_disk_pressure_check.py' -v
bash -n scripts/check-disk-pressure.sh
```

この check が扱うのは対象 filesystem の block 使用率であり、inode 枯渇、memory、remote object storage、ネットワーク容量は別のシグナルとして監視する。
