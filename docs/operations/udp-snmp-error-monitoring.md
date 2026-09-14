# UDP SNMP error delta の read-only 診断

SRT など UDP を使う配信で packet loss や受信停滞が疑われる場合、NIC の error/drop counter だけでは kernel の UDP 層で発生した buffer drop や checksum error を確認できません。`scripts/check-udp-snmp-errors.sh` は Linux `/proc/net/snmp` の UDP 累積 counter を、同一 host / network namespace / boot generation の baseline と比較する read-only 診断です。

この checker は `/proc/net/snmp` や baseline を作成・更新・削除せず、socket、route、sysctl、service、process に変更を加えません。

## 対象 counter

次の4 counter の増分だけを評価します。

- `InErrors`: UDP input error の累積数。
- `RcvbufErrors`: receive buffer 不足等で受信できなかった datagram の累積数。
- `SndbufErrors`: send buffer 側で送信できなかった datagram の累積数。
- `InCsumErrors`: input checksum error の累積数。

`NoPorts` や datagram 総数は、この checker 単独では障害と断定できないため status 判定に使いません。kernel が将来追加した未知 field は、record 全体の数値整合性を確認したうえで無視します。

## baseline

baseline は監視側が `/proc/net/snmp` をそのまま保存して管理します。checker 自身は snapshot を生成しません。

```bash
install -m 0600 /proc/net/snmp /run/irlight-monitor/snmp.baseline

IRLIGHT_UDP_SNMP_BASELINE_PATH=/run/irlight-monitor/snmp.baseline \
  bash scripts/check-udp-snmp-errors.sh
```

任意の current fixture を確認する場合は、第1引数または `IRLIGHT_UDP_SNMP_PATH` を指定できます。既定 current は `/proc/net/snmp` です。

baseline は **同じ host、同じ network namespace、同じ boot/counter generation** でのみ比較してください。再起動、namespace 再作成、counter generation の切替後は、障害がないことを確認して新しい baseline を監視側で取得します。current が baseline より小さい場合、checker は generation を推測せず `UNKNOWN / counter_reset` にします。

## status 契約

- `OK` / exit `0`: 対象4 counter に増加なし。
- `WARNING` / exit `1`: 少なくとも1つの対象 counter が増加。
- `UNKNOWN` / exit `3`: current/baseline 不在、UDP record の欠損・重複・不正値、counter reset 等で安全に評価できない。

UDP counter は host/network namespace 全体の signal であり、増加だけで IRLight Session の障害原因とは断定しません。そのため単独では `CRITICAL` を返さず、SRT retransmit、Media Node の症状、NIC error/drop、CPU/PSI、egress/ingest event と時刻を合わせて判断します。

出力例:

```text
IRLIGHT_UDP_SNMP_ERRORS status=WARNING reason=udp_error_activity in_errors_delta=4 rcvbuf_errors_delta=4 sndbuf_errors_delta=0 csum_errors_delta=0
```

出力には path、IP、interface 名、remote peer、credential を含めません。

## 検証

```bash
python -m unittest discover -s tests -p 'test_udp_snmp_errors_check.py' -v
bash -n scripts/check-udp-snmp-errors.sh
```

この診断は原因を自動修復しません。buffer サイズ変更、sysctl 変更、service restart、traffic shaping、provider 操作は影響範囲を確認した別の運用判断として扱います。
