# TCP memory pressure monitoring

Issue #11 の host-level diagnostics で、Linux TCP stack が kernel の TCP memory watermarks に近づいているかを read-only に確認する targeted check です。

```bash
python3 scripts/check-host-tcp-memory-pressure.py
```

既定では `/proc/net/sockstat` の `TCP ... mem` と `/proc/sys/net/ipv4/tcp_mem` を読みます。`mem` と `tcp_mem` はどちらも page count として比較し、`pressure` 未満は `OK`、`pressure` 以上は `WARNING`、`max` 以上は `CRITICAL` です。exit code は順に `0 / 1 / 2`、入力を安全に評価できない場合は `UNKNOWN` / `3` を返します。

出力例:

```text
IRLIGHT_TCP_MEMORY_PRESSURE status=WARNING tcp_mem_pages=60000 pressure_pages=50000 max_pages=100000 reason=at_or_above_pressure
```

この signal は host / kernel TCP memory pressure の診断であり、特定 Session、destination、publisher の障害原因を確定するものではありません。check は sysctl、socket、route、qdisc、service、process、provider resource を変更せず、回復操作もしません。

fixture や明示的に採取した snapshot を検証する場合は `--sockstat PATH --tcp-mem PATH` を使えます。入力はそれぞれ 64 KiB に制限し、symlink、非 regular file、非 ASCII、duplicate TCP field、不正または逆転した watermarks は固定 `UNKNOWN` reason へ fail-closed します。path や入力内容は出力へ再表示しません。

`/proc/net/sockstat` と `tcp_mem` の scope は kernel / namespace 構成の影響を受けるため、この checker 単独で autoscaling、Session stop、sysctl tuning を行わないでください。production threshold を repository 側で独自上書きせず、kernel が公開する pressure/max watermarks を診断境界として使います。
