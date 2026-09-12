# Disk pressure monitoring

Issue #11 の disk full 監視に対して、ホストまたは永続 state filesystem の block / inode 使用率を副作用なく確認するための運用チェックを定義する。

## Check

```bash
bash scripts/check-disk-pressure.sh /
```

監視対象を明示しない場合は `IRLIGHT_DISK_PATH`、`STATE_DIR`、`/state` の順で選ぶ。block 使用率の既定閾値は warning 80%、critical 90%。inode 使用率も既定では同じ閾値を使い、必要な filesystem では独立に変更できる。

```bash
IRLIGHT_DISK_WARNING_PERCENT=80 \
IRLIGHT_DISK_CRITICAL_PERCENT=90 \
IRLIGHT_DISK_INODE_WARNING_PERCENT=80 \
IRLIGHT_DISK_INODE_CRITICAL_PERCENT=90 \
  bash scripts/check-disk-pressure.sh "${STATE_DIR:-/state}"
```

出力は監視系が機械的に扱える固定プレフィックスと key/value に限定する。

```text
IRLIGHT_DISK_PRESSURE status=OK usage_percent=52 available_kb=123456 inode_usage_percent=14 available_inodes=98765 warning_percent=80 critical_percent=90 inode_warning_percent=80 inode_critical_percent=90
```

全体 `status` は block / inode のうち深刻な方を採用する。空き block が十分でも inode が枯渇すると新規ファイル作成に失敗するため、inode pressure も `WARNING` / `CRITICAL` の判定対象とする。

exit code は次の意味を持つ。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | block / inode とも warning 閾値未満 |
| 1 | `WARNING` | block または inode が warning 以上、どちらも critical 未満 |
| 2 | `CRITICAL` | block または inode が critical 以上 |
| 3 | `UNKNOWN` | path、閾値、`df` 実行または block / inode 出力を安全に評価できない |

`UNKNOWN` を正常扱いしない。監視不能時は filesystem headroom を証明できないため、監視障害として通知する。inode 情報を取得できない filesystem も、値を推測して `OK` にせず `UNKNOWN` とする。

## Response

`WARNING` では `df -h`、`df -ih` と `docker system df`、ログ、ビルドキャッシュ、生成物、小さいファイルの大量生成などの増加傾向を確認し、何が block または inode を消費しているかを特定する。`CRITICAL` では新規 allocation を抑制できる運用があれば適用し、進行中 Session と authority state を保全したまま headroom を回復する。`UNKNOWN` は path/mount/permission/監視コマンドを確認し、値を推測して正常判定しない。

この check 自体はファイル、Docker image、container、volume、authority state を削除しない。自動復旧として `docker system prune --volumes`、`docker volume prune`、`docker compose down -v`、state/marker 削除を実行しない。cleanup が必要な場合は消費源と所有権を確認し、対象を限定した別の明示操作として扱う。

通知先や定期実行方式は deployment 固有のため、このスクリプトには外部 webhook やクラウド操作を組み込まない。systemd timer、監視 agent、既存 scheduler 等から exit code と固定出力を収集する。

## Validation

```bash
python -m unittest discover -s tests -p 'test_disk_pressure_check.py' -v
bash -n scripts/check-disk-pressure.sh
```

この check が扱うのは対象 filesystem の block / inode 使用率であり、memory、remote object storage、ネットワーク容量は別のシグナルとして監視する。
