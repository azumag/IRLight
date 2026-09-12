# Memory pressure monitoring

Issue #11 の memory pressure 監視に対して、Linux host の `/proc/meminfo` を副作用なく確認する運用チェックを定義する。

## Check

```bash
bash scripts/check-memory-pressure.sh
```

既定では `/proc/meminfo` を読む。テストや限定された診断では第1引数または `IRLIGHT_MEMINFO_PATH` で別ファイルを指定できる。閾値は既定で warning 80%、critical 90%。

```bash
IRLIGHT_MEMORY_WARNING_PERCENT=80 \
IRLIGHT_MEMORY_CRITICAL_PERCENT=90 \
  bash scripts/check-memory-pressure.sh
```

判定には Linux の `MemAvailable` を使う。単純な `MemFree` ではなく、回収可能な page cache 等を含めて新しい workload に利用可能と見積もられるメモリを基準にする。

出力は固定プレフィックスと key/value に限定する。

```text
IRLIGHT_MEMORY_PRESSURE status=OK usage_percent=52 available_kb=503316 total_kb=1048576 warning_percent=80 critical_percent=90
```

exit code は次の意味を持つ。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | warning 閾値未満 |
| 1 | `WARNING` | warning 以上、critical 未満 |
| 2 | `CRITICAL` | critical 以上 |
| 3 | `UNKNOWN` | 閾値、meminfo path、必須metric、値の整合性を安全に評価できない |

`UNKNOWN` を正常扱いしない。`MemAvailable` 欠落、読取不能、非数値、`MemAvailable > MemTotal` 等では値を推測せず監視障害として通知する。

## Response

`WARNING` では `free -h`、`ps`、container/runtime metrics、直近deployとprocess restartの増加を確認し、memoryを消費している主体と増加傾向を特定する。`CRITICAL` では新規Sessionや高負荷処理を抑制できる運用があれば適用し、進行中Sessionとauthority stateを保全したまま原因processを特定する。

この check 自体はprocess停止、container restart、cache drop、swap変更、OOM killer設定変更を行わない。自動復旧として `kill`、`docker restart`、`drop_caches` 等を実行しない。復旧操作は影響範囲と所有権を確認した別の明示操作として扱う。

## Verification

```bash
python -m unittest discover -s tests -p 'test_memory_pressure_check.py' -v
bash -n scripts/check-memory-pressure.sh
```

この check は Linux host の `/proc/meminfo` を対象にする。container 単位の cgroup memory limit / `memory.current`、swap、PSI、process別RSS、GPU memory は別のシグナルとして扱い、hostの `OK` をcontainerの余裕と読み替えない。
