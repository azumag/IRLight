# TCP connection attempt failure delta の targeted read-only 診断

`Tcp.AttemptFails` の増加を、operator が保存した `/proc/net/snmp` baseline と現在値の差分で確認する一次診断です。TCP connection establishment が成立せず CLOSED へ戻った活動を host / network namespace 単位で把握できますが、特定の IRLight Session、配信先、service の障害を単独で確定するものではありません。

## 使い方

baseline と current は **同じ host、同じ network namespace、同じ boot / counter generation** のものを使用します。baseline は監視・運用側で管理し、この checker は baseline を作成・更新・削除しません。

```bash
bash scripts/check-tcp-snmp-attempt-fails.sh \
  /proc/net/snmp \
  /run/irlight-monitor/proc-net-snmp.baseline
```

環境変数でも path を指定できます。

```bash
IRLIGHT_TCP_SNMP_PATH=/proc/net/snmp \
IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-tcp-snmp-attempt-fails.sh
```

baseline の取得は operator の責任で行います。再起動や network namespace の再作成など counter generation が変わったと判断した場合のみ、新しい安定時 snapshot を明示的に baseline として採用してください。診断結果を理由に自動で baseline を更新してはいけません。

## Network egress aggregate への opt-in

通常の `check-network-egress-health.sh` は baseline を持たない既存環境の stdout / exit code を変えないため、connection attempt failure delta を既定では含めません。同じ一次切り分けに含める場合だけ `IRLIGHT_TCP_ATTEMPT_FAILS_MODE=enabled` と baseline を明示します。

```bash
IRLIGHT_TCP_ATTEMPT_FAILS_MODE=enabled \
IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current は既定 `/proc/net/snmp` を使い、fixture や明示した network namespace の snapshot だけ `IRLIGHT_TCP_SNMP_PATH` で上書きします。aggregate は checker と同じ bounded component timeout の配下で実行し、baseline/current の欠損・破損、counter reset、timeout、不正 mode は `tcp_attempt_fails_status=UNKNOWN` として fail-closed します。attempt-failure delta 増加は `WARNING` です。

有効時だけ `tcp_attempt_fails_status` を optional status registry の末尾に追加します。現在の固定順は `interface_errors_status` → `udp_snmp_errors_status` → `tcp_snmp_retransmits_status` → `tcp_listen_pressure_status` → `tcp_established_resets_status` → `tcp_attempt_fails_status` です。aggregate severity は既存どおり `CRITICAL > UNKNOWN > WARNING > OK` のため、link / route / NIC の確定 `CRITICAL` を attempt-failure の `WARNING` / `UNKNOWN` が隠しません。

aggregate 自身も baseline を作成・更新・削除せず、current/baseline path、counter 生値、IP、port、credential、secret を stdout に追加しません。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK tcp_attempt_fails_status=WARNING family=dual
```

## Status 契約

差分がない場合:

```text
IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=OK reason=none attempt_fails_delta=0
```

`AttemptFails` が増加した場合:

```text
IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=WARNING reason=tcp_connection_attempt_failures attempt_fails_delta=3
```

baseline/current の欠損・破損、対象 counter の不正値、counter reset の場合:

```text
IRLIGHT_TCP_SNMP_ATTEMPT_FAILS status=UNKNOWN reason=<reason>
```

exit code は `0=OK`、`1=WARNING`、`3=UNKNOWN` です。

`WARNING` は TCP connection establishment failure の増加を示す host / network namespace 全体の signal です。外向き connect の拒否・timeout だけでなく、host 上の別 TCP workload や listener 側の状態も影響し得るため、この checker 単独では `CRITICAL` にせず、特定 Session / destination の障害へ自動帰属しません。

## Fail-closed と互換性

- `Tcp:` header/value pair が一意でない、列数が一致しない、`AttemptFails` が欠損・重複・非数値・負数・対応範囲外の場合は `UNKNOWN` にします。
- Linux で通常見られる `MaxConn=-1` のような **無関係 counter** は検証対象にしません。
- kernel が将来追加した無関係 counter も header/value の対応が保たれていれば受理します。
- current が baseline より小さい場合は reboot / namespace replacement / counter generation change 等を区別できないため `counter_reset` として `UNKNOWN` にします。
- path、IP、port、destination、credential、secret は stdout に出しません。

## 安全境界

この checker は `/proc/net/snmp` と指定された baseline を読み取るだけです。socket、route、firewall、sysctl、service、process、Session、provider resource、baseline file を変更しません。`WARNING` / `UNKNOWN` を理由に自動 restart、route 変更、firewall 変更、Session stop を行いません。

aggregate へ opt-in した場合も同じ read-only 境界を維持します。aggregate は baseline を作成・更新・削除せず、attempt-failure signal を特定 Session / destination / service の確定障害へ自動帰属しません。

## 切り分け

attempt failure delta が増えた場合は、同じ観測期間について次を相関させます。

- `check-tcp-snmp-retransmits.sh` の retransmit delta
- `check-tcp-snmp-established-resets.sh` の established reset delta
- `check-network-interface-errors.sh` の NIC error/drop delta
- `check-network-egress-health.sh` の link/default-route と optional network signals
- egress event の connect / reconnect / disconnect / reason code
- 複数 destination で同時発生しているか、単一 destination に偏っているか

複数 destination で同時に attempt failure が増え、route/NIC/retransmit 側にも異常がある場合は Node/network 共通要因を疑います。単一 destination に偏る場合は destination 側や個別経路も候補ですが、host-wide counter だけで原因を断定しません。

## 検証

focused regression と shell syntax check:

```bash
python -m unittest discover -s tests -p 'test_tcp_snmp_attempt_fails_check.py' -v
python -m unittest discover -s tests -p 'test_network_egress_tcp_attempt_fails.py' -v
python -m unittest discover -s tests -p 'test_tcp_attempt_fail_runbook.py' -v
bash -n scripts/check-tcp-snmp-attempt-fails.sh
bash -n scripts/check-network-egress-health.sh
```

通常の repository CI と Dependency audit も merge gate とします。
