# TCP established reset delta の targeted read-only 診断

`Tcp.EstabResets` の増加を、operator が保存した `/proc/net/snmp` baseline と現在値の差分で確認するための一次診断です。接続済み TCP が reset された兆候を host / network namespace 単位で確認できますが、特定の IRLight Session、配信先、service が原因であることを単独で確定するものではありません。

## 使い方

baseline と current は **同じ host、同じ network namespace、同じ boot / counter generation** のものを使用します。baseline は監視・運用側で管理し、この checker は baseline を作成・更新・削除しません。

```bash
bash scripts/check-tcp-snmp-established-resets.sh \
  /proc/net/snmp \
  /run/irlight-monitor/proc-net-snmp.baseline
```

環境変数でも path を指定できます。

```bash
IRLIGHT_TCP_SNMP_PATH=/proc/net/snmp \
IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-tcp-snmp-established-resets.sh
```

baseline の取得は operator の責任で行います。再起動や network namespace の再作成など counter generation が変わったと判断した場合のみ、新しい安定時 snapshot を明示的に baseline として採用してください。診断結果を理由に自動で baseline を更新してはいけません。

## Status 契約

差分がない場合:

```text
IRLIGHT_TCP_SNMP_ESTABLISHED_RESETS status=OK reason=none established_resets_delta=0
```

`EstabResets` が増加した場合:

```text
IRLIGHT_TCP_SNMP_ESTABLISHED_RESETS status=WARNING reason=tcp_established_reset_activity established_resets_delta=3
```

baseline/current の欠損・破損、対象 counter の不正値、counter reset の場合:

```text
IRLIGHT_TCP_SNMP_ESTABLISHED_RESETS status=UNKNOWN reason=<reason>
```

exit code は `0=OK`、`1=WARNING`、`3=UNKNOWN` です。

`WARNING` は接続済み TCP reset の増加を示す host / network namespace 全体の signal です。相手側の切断、local application、NAT/firewall、network path など複数の要因で増えるため、この checker 単独では `CRITICAL` にせず、特定 Session や destination の障害へ自動帰属しません。

## Fail-closed と互換性

- `Tcp:` header/value pair が一意でない、列数が一致しない、`EstabResets` が欠損・重複・非数値・負数・対応範囲外の場合は `UNKNOWN` にします。
- Linux で通常見られる `MaxConn=-1` のような **無関係 counter** は検証対象にしません。
- kernel が将来追加した無関係 counter も header/value の対応が保たれていれば受理します。
- current が baseline より小さい場合は reboot / namespace replacement / counter generation change 等を区別できないため `counter_reset` として `UNKNOWN` にします。
- path、IP、port、destination、credential、secret は stdout に出しません。

## 安全境界

この checker は `/proc/net/snmp` と指定された baseline を読み取るだけです。socket、route、firewall、sysctl、service、process、Session、provider resource、baseline file を変更しません。`WARNING` / `UNKNOWN` を理由に自動 restart、route 変更、firewall 変更、Session stop を行いません。

## 切り分け

reset delta が増えた場合は、同じ観測期間について次を相関させます。

- `check-tcp-snmp-retransmits.sh` の retransmit delta
- `check-network-interface-errors.sh` の NIC error/drop delta
- `check-network-egress-health.sh` の link/default-route と optional network signals
- egress event の reconnect / disconnect / reason code
- 複数 destination で同時発生しているか、単一 destination に偏っているか

複数 destination で同時に reset が増え、NIC/route/retransmit 側にも異常がある場合は Node/network 共通要因を疑います。単一 destination に偏る場合は destination 側や個別経路も候補ですが、counter だけで原因を断定しません。

## 検証

focused regression と shell syntax check:

```bash
python -m unittest discover -s tests -p 'test_tcp_snmp_established_resets_check.py' -v
bash -n scripts/check-tcp-snmp-established-resets.sh
```

通常の repository CI と Dependency audit も merge gate とします。
