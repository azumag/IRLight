# TCP listen overflow / drop delta の read-only 診断

RTMP / RTMPS ingest、Control Plane API、Media Node の管理 endpoint など TCP listener が接続を受け付けにくい症状では、process 自体が生存し route も正常でも、kernel の listener 側で接続受付に失敗している場合があります。`scripts/check-tcp-listen-overflows.sh` は Linux `/proc/net/netstat` の `TcpExt` 累積 counter を、同じ host / network namespace / boot generation の baseline と比較する read-only 診断です。

この checker は `/proc/net/netstat` や baseline を作成・更新・削除せず、socket backlog、sysctl、route、firewall、service、process に変更を加えません。

## 対象 counter

次の2 counter を読みます。

- `ListenOverflows`: TCP accept queue が満杯になった事象の累積 counter。
- `ListenDrops`: listening socket で packet / connection を drop した事象の累積 counter。queue overflow 以外の要因でも増え得るため、`ListenOverflows` と同義とは扱いません。

どちらかが baseline から増加していれば `WARNING` にします。counter は host / network namespace 全体の値であり、特定の IRLight Session、特定 port、特定 service の原因と自動的には結び付けません。また `ListenDrops` の増加だけから backlog 不足と断定しません。

判定対象の2 counter は非負の有界整数として厳密に検査します。`TcpExt` に将来 field が追加されても、header と value の個数対応が保たれ、対象 field の意味が変わらない限り互換に扱います。

## baseline

baseline は monitoring / operator 側が `/proc/net/netstat` をそのまま保存して管理します。checker 自身は snapshot を生成しません。

```bash
install -m 0600 /proc/net/netstat /run/irlight-monitor/netstat.baseline

IRLIGHT_TCP_NETSTAT_BASELINE_PATH=/run/irlight-monitor/netstat.baseline \
  bash scripts/check-tcp-listen-overflows.sh
```

任意の current fixture を確認する場合は、第1引数または `IRLIGHT_TCP_NETSTAT_PATH` を指定できます。既定 current は `/proc/net/netstat` です。

baseline は **同じ host、同じ network namespace、同じ boot / counter generation** でのみ比較してください。再起動、network namespace 再作成、counter generation の切替後は、障害がないことを確認して monitoring 側で baseline を取り直します。current の対象 counter が baseline より小さい場合、checker は generation を推測せず `UNKNOWN / counter_reset` にします。

## status 契約

- `OK` / exit `0`: `ListenOverflows` / `ListenDrops` に増加なし。
- `WARNING` / exit `1`: どちらかが baseline から増加。
- `UNKNOWN` / exit `3`: current / baseline 不在、`TcpExt` record の欠損・重複、対象 counter の不正値、counter reset 等で安全に評価できない。

listen overflow / drop は host / network namespace 全体の signal です。この checker 単独では `CRITICAL` にせず、同時刻の ingest connection failure、API connection failure、CPU / PSI、process FD、conntrack、service log、listener backlog 設定、memory pressure などと合わせて影響範囲を判断します。特定 service の backlog を自動変更したり、`somaxconn` 等の sysctl を自動調整したりしません。

出力例:

```text
IRLIGHT_TCP_LISTEN_OVERFLOWS status=WARNING reason=tcp_listener_pressure listen_overflows_delta=3 listen_drops_delta=5
```

`tcp_listener_pressure` は listener 側の異常兆候をまとめた reason であり、queue overflow の確定診断ではありません。出力には path、IP、interface 名、port、remote peer、destination URL、credential を含めません。

## 検証

```bash
python -m unittest discover -s tests -p 'test_tcp_listen_overflows_check.py' -v
bash -n scripts/check-tcp-listen-overflows.sh
```

この診断は原因を自動修復しません。backlog / sysctl 変更、service restart、traffic shaping、firewall 変更、provider 操作は影響範囲を確認した別の運用判断として扱います。
