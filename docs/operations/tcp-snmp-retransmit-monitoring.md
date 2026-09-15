# TCP SNMP retransmit delta の read-only 診断

RTMP / RTMPS など TCP を使う配信で reconnect、throughput 低下、送出停滞が疑われる場合、link state や default route が正常でも kernel の TCP retransmission が増えていることがあります。`scripts/check-tcp-snmp-retransmits.sh` は Linux `/proc/net/snmp` の TCP 累積 counter を、同じ host / network namespace / boot generation の baseline と比較する read-only 診断です。

この checker は `/proc/net/snmp` や baseline を作成・更新・削除せず、socket、route、sysctl、service、process に変更を加えません。

## 対象 counter

次の2 counter を読みます。

- `OutSegs`: 送信した TCP segment の累積数。観測期間の送出 activity を確認する文脈値として出力します。
- `RetransSegs`: 再送した TCP segment の累積数。baseline から増加していれば `WARNING` にします。

`OutSegs` の増加だけでは warning にしません。判定に使う `OutSegs` / `RetransSegs` は非負の有界整数として厳密に検査します。一方、Linux の標準 `Tcp:` record には `MaxConn=-1` のような signed sentinel が存在するため、判定対象外 field の値域を unsigned counter と決め付けず無視します。header と value の個数対応は維持し、将来 field が追加されても対象2 counter の意味が変わらない限り互換に扱います。

## baseline

baseline は monitoring / operator 側が `/proc/net/snmp` をそのまま保存して管理します。checker 自身は snapshot を生成しません。

```bash
install -m 0600 /proc/net/snmp /run/irlight-monitor/snmp.baseline

IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/snmp.baseline \
  bash scripts/check-tcp-snmp-retransmits.sh
```

任意の current fixture を確認する場合は、第1引数または `IRLIGHT_TCP_SNMP_PATH` を指定できます。既定 current は `/proc/net/snmp` です。

baseline は **同じ host、同じ network namespace、同じ boot / counter generation** でのみ比較してください。再起動、network namespace 再作成、counter generation の切替後は、障害がないことを確認して新しい baseline を monitoring 側で取得します。current の `OutSegs` または `RetransSegs` が baseline より小さい場合、checker は generation を推測せず `UNKNOWN / counter_reset` にします。

## status 契約

- `OK` / exit `0`: `RetransSegs` に増加なし。
- `WARNING` / exit `1`: `RetransSegs` が baseline から増加。
- `UNKNOWN` / exit `3`: current / baseline 不在、TCP record の欠損・重複、対象 counter の不正値、counter reset 等で安全に評価できない。

TCP retransmission は host / network namespace 全体の signal です。少量の再送は通常のネットワークでも起こり得るため、この checker 単独では `CRITICAL` にせず、特定 IRLight Session や特定 destination の障害原因へ自動的に帰属させません。RTMP / RTMPS reconnect、destination 側 event、NIC error / drop、CPU / PSI、同時刻の packet loss 等の証跡と合わせて判断します。SRT など UDP の切り分けには別の UDP SNMP 診断を使います。

出力例:

```text
IRLIGHT_TCP_SNMP_RETRANSMITS status=WARNING reason=tcp_retransmit_activity out_segments_delta=1874 retrans_segments_delta=6
```

出力には path、IP、interface 名、port、remote peer、destination URL、credential を含めません。

## 検証

```bash
python -m unittest discover -s tests -p 'test_tcp_snmp_retransmits_check.py' -v
bash -n scripts/check-tcp-snmp-retransmits.sh
```

この診断は原因を自動修復しません。TCP tuning、sysctl 変更、route / firewall 変更、service restart、traffic shaping、provider 操作は影響範囲を確認した別の運用判断として扱います。
