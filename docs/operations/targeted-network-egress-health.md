# Targeted network egress health diagnostic

Media Node の特定 interface について、link state と default route の最低限の成立性を read-only でまとめて確認する診断です。Issue #11 の障害切り分け用であり、route、interface、DNS、firewall、provider resource を変更しません。

## 目的

`operstate` だけが正常でも default route が失われていれば外向き配信は成立しません。一方、IPv4-only / IPv6-only の環境で利用しない address family の default route を必須にすると誤検知になります。このため、診断対象の address family は operator が `ipv4` / `ipv6` / `dual` のいずれかとして明示します。

この診断は「外部配信先へ到達できる」ことを保証しません。DNS、firewall、NAT、provider 側障害、宛先固有の経路、TLS、RTMP publish 認可は別の確認です。

## 実行

IPv4 のみを必要とする node:

```bash
bash scripts/check-network-egress-health.sh eth0 ipv4
```

IPv6 のみ:

```bash
bash scripts/check-network-egress-health.sh eth0 ipv6
```

両方を運用要件にする場合:

```bash
bash scripts/check-network-egress-health.sh eth0 dual
```

環境変数でも指定できます。

```bash
IRLIGHT_NETWORK_INTERFACE=eth0 \
IRLIGHT_NETWORK_ADDRESS_FAMILY=dual \
bash scripts/check-network-egress-health.sh
```

通常は `/sys/class/net/<interface>/operstate`、`/proc/net/route`、`/proc/net/ipv6_route` を読みます。fixture や限定診断では `IRLIGHT_NETWORK_INTERFACE_DIR`、`IRLIGHT_IPV4_ROUTE_TABLE`、`IRLIGHT_IPV6_ROUTE_TABLE` を明示できます。

各 component は既定 10 秒で打ち切ります。`IRLIGHT_NETWORK_COMPONENT_TIMEOUT_SECONDS` で 1〜300 秒の整数に変更できます。

```bash
IRLIGHT_NETWORK_COMPONENT_TIMEOUT_SECONDS=5 \
bash scripts/check-network-egress-health.sh eth0 ipv4
```

この制限は、proc/sysfs 読み取りや下位 check が異常に停止した場合に aggregate 自体が無期限に停止することを防ぐためです。Linux の GNU `timeout` を利用し、timeout 設定が不正、`timeout` utility が利用不能、または component が制限時間を超過した場合、その component は fail-closed で `UNKNOWN` になります。制限を外して無期限実行へフォールバックはしません。

## NIC error/drop delta の opt-in

既定では NIC の累積 error/drop counter は aggregate に含めません。既存利用者の stdout / exit code 契約を維持し、baseline を持たない環境で誤って `UNKNOWN` にしないためです。

PR #309 で追加した `check-network-interface-errors.sh` を同じ interface に対して使う場合だけ、`IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE=enabled` と baseline directory を明示します。

```bash
IRLIGHT_NETWORK_INTERFACE_ERRORS_MODE=enabled \
IRLIGHT_NETWORK_STATS_BASELINE_DIR=/run/irlight-monitor/eth0.statistics.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current counter は aggregate に渡した `IRLIGHT_NETWORK_INTERFACE_DIR`、または通常の `/sys/class/net/<interface>` の直下にある `statistics` directory を使います。別 interface の current path は指定できません。baseline は [network-interface-error-monitoring.md](network-interface-error-monitoring.md) の契約どおり、同じ interface generation から operator / monitoring 側が用意し、aggregate 自身は作成・更新・削除しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、baseline 欠損・不正、counter reset、component timeout は `interface_errors_status=UNKNOWN` として fail-closed します。drop-only は `WARNING`、error 増加は `CRITICAL` です。集約優先順位は他 component と同じ `CRITICAL > UNKNOWN > WARNING > OK` なので、例えば route parse が `UNKNOWN` でも NIC error が確認済みなら総合 `CRITICAL` を維持します。

無効時の出力は従来形式のままです。有効時だけ `interface_errors_status` を追加します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK interface_errors_status=WARNING family=dual
```

interface 名、current/baseline path、MAC/IP、gateway、counter の生値は aggregate 出力へ追加しません。

## UDP SNMP error delta の opt-in

既定では `/proc/net/snmp` の UDP 累積 error counter を aggregate に含めません。これらは host / network namespace 全体の signal であり、baseline generation を operator 側で管理する必要があるためです。特定 interface や特定 Session の障害と自動的に結び付けません。

PR #316 で追加した `check-udp-snmp-errors.sh` を同じ一次切り分けに含める場合だけ、`IRLIGHT_UDP_SNMP_ERRORS_MODE=enabled` と baseline file を明示します。

```bash
IRLIGHT_UDP_SNMP_ERRORS_MODE=enabled \
IRLIGHT_UDP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current は既定 `/proc/net/snmp` を読みます。fixture や明示した network namespace の snapshot を検査する場合だけ `IRLIGHT_UDP_SNMP_PATH` で current path を上書きできます。baseline は [udp-snmp-error-monitoring.md](udp-snmp-error-monitoring.md) の契約どおり、同じ host / network namespace / boot generation から operator / monitoring 側が用意し、aggregate 自身は作成・更新・削除しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、baseline/current 欠損・不正、counter reset、component timeout は `udp_snmp_errors_status=UNKNOWN` として fail-closed します。`InErrors` / `RcvbufErrors` / `SndbufErrors` / `InCsumErrors` の新規増加は `WARNING` です。host-wide signal だけでは IRLight Session の確定障害を証明できないため、この component 単独では `CRITICAL` にしません。集約優先順位は `CRITICAL > UNKNOWN > WARNING > OK` のままで、link / route の確定 `CRITICAL` は UDP `UNKNOWN` / `WARNING` に隠されません。

UDP mode 未指定または `disabled` の場合、既存 stdout / exit code 形式を変更しません。有効時だけ `udp_snmp_errors_status` を追加します。NIC error/drop opt-in と同時に有効化した場合は両方の status を表示します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK udp_snmp_errors_status=WARNING family=dual
```

current/baseline path、IP、port、socket、counter の生値は aggregate 出力へ追加しません。

## TCP SNMP retransmit delta の opt-in

既定では `/proc/net/snmp` の TCP retransmission counter を aggregate に含めません。`OutSegs` / `RetransSegs` は host / network namespace 全体の signal であり、特定 Session や destination の障害へ自動帰属できないためです。また baseline generation を operator 側で管理できない環境の stdout / exit code を変えません。

`check-tcp-snmp-retransmits.sh` を一次切り分けに含める場合だけ、`IRLIGHT_TCP_SNMP_RETRANSMITS_MODE=enabled` と baseline file を明示します。

```bash
IRLIGHT_TCP_SNMP_RETRANSMITS_MODE=enabled \
IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current は既定 `/proc/net/snmp` を読みます。fixture や明示した network namespace の snapshot を検査する場合だけ `IRLIGHT_TCP_SNMP_PATH` で上書きできます。baseline は [tcp-snmp-retransmit-monitoring.md](tcp-snmp-retransmit-monitoring.md) の契約どおり、同じ host / network namespace / boot generation から operator / monitoring 側が用意し、aggregate 自身は baseline を作成・更新・削除しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、baseline/current 欠損・破損、counter reset、component timeout は `tcp_snmp_retransmits_status=UNKNOWN` として fail-closed します。`RetransSegs` の新規増加は `WARNING` です。host-wide signal 単独では Session の確定障害を証明できないため、この component 単独では `CRITICAL` にしません。link / route / NIC の確定 `CRITICAL` は TCP `WARNING` / `UNKNOWN` に隠されません。

TCP mode 未指定または `disabled` の場合、既存 stdout / exit code 形式を変更しません。有効時だけ `tcp_snmp_retransmits_status` を追加します。NIC、UDP、TCP を同時に有効化した場合は、その固定順で status field を表示します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK tcp_snmp_retransmits_status=WARNING family=dual
```

current/baseline path、IP、port、socket、counter の生値は aggregate 出力へ追加しません。

## TCP listener pressure delta の opt-in

既定では `/proc/net/netstat` の `TcpExt.ListenOverflows` / `ListenDrops` を aggregate に含めません。これらは host / network namespace 全体の signal であり、特定 Session、port、service の障害へ自動帰属できません。また baseline generation を operator 側で管理できない環境の stdout / exit code を変えません。

`check-tcp-listen-overflows.sh` を一次切り分けに含める場合だけ、`IRLIGHT_TCP_LISTEN_PRESSURE_MODE=enabled` と baseline file を明示します。

```bash
IRLIGHT_TCP_LISTEN_PRESSURE_MODE=enabled \
IRLIGHT_TCP_NETSTAT_BASELINE_PATH=/run/irlight-monitor/netstat.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current は既定 `/proc/net/netstat` を読みます。fixture や明示した network namespace の snapshot を検査する場合だけ `IRLIGHT_TCP_NETSTAT_PATH` で上書きできます。baseline は [tcp-listen-overflow-monitoring.md](tcp-listen-overflow-monitoring.md) の契約どおり、同じ host / network namespace / boot generation から operator / monitoring 側が用意し、aggregate 自身は baseline を作成・更新・削除しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、baseline/current 欠損・破損、counter reset、component timeout は `tcp_listen_pressure_status=UNKNOWN` として fail-closed します。`ListenOverflows` / `ListenDrops` の新規増加は `WARNING` です。host-wide signal 単独では Session / port / service の確定障害を証明できないため、この component 単独では `CRITICAL` にしません。link / route / NIC の確定 `CRITICAL` は listener `WARNING` / `UNKNOWN` に隠されません。

listener mode 未指定または `disabled` の場合、既存 stdout / exit code 形式を変更しません。有効時だけ `tcp_listen_pressure_status` を追加します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK tcp_listen_pressure_status=WARNING family=dual
```

current/baseline path、IP、port、socket、counter の生値は aggregate 出力へ追加しません。

## TCP established reset delta の opt-in

既定では `/proc/net/snmp` の `Tcp.EstabResets` を aggregate に含めません。この counter は host / network namespace 全体の signal であり、接続済み TCP reset の増加だけでは特定 Session、destination、service の障害や原因を確定できません。

`check-tcp-snmp-established-resets.sh` を一次切り分けに含める場合だけ、`IRLIGHT_TCP_ESTABLISHED_RESETS_MODE=enabled` と baseline file を明示します。

```bash
IRLIGHT_TCP_ESTABLISHED_RESETS_MODE=enabled \
IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current は既定 `/proc/net/snmp` を読み、fixture や明示した network namespace の snapshot だけ `IRLIGHT_TCP_SNMP_PATH` で上書きします。baseline は [tcp-established-reset-monitoring.md](tcp-established-reset-monitoring.md) の契約どおり、同じ host / network namespace / boot generation から operator / monitoring 側が用意します。aggregate 自身は baseline を作成・更新・削除しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、baseline/current 欠損・破損、counter reset、component timeout は `tcp_established_resets_status=UNKNOWN` として fail-closed します。`EstabResets` の新規増加は `WARNING` です。この component 単独では `CRITICAL` にせず、link / route / NIC の確定 `CRITICAL` を `WARNING` / `UNKNOWN` が隠しません。

mode 未指定または `disabled` の場合、既存 stdout / exit code 形式を変更しません。有効時だけ `tcp_established_resets_status` を追加します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK tcp_established_resets_status=WARNING family=dual
```

current/baseline path、IP、port、destination、credential、counter の生値は aggregate 出力へ追加しません。

## TCP connection attempt failure delta の opt-in

既定では `/proc/net/snmp` の `Tcp.AttemptFails` を aggregate に含めません。この counter は TCP connection establishment が成立せず CLOSED へ戻った activity を host / network namespace 単位で示しますが、外向き connect の失敗だけでなく別 TCP workload や listener 側の状態でも増え得るため、特定 Session、destination、service の確定障害へ自動帰属しません。

`check-tcp-snmp-attempt-fails.sh` を一次切り分けに含める場合だけ、`IRLIGHT_TCP_ATTEMPT_FAILS_MODE=enabled` と baseline file を明示します。

```bash
IRLIGHT_TCP_ATTEMPT_FAILS_MODE=enabled \
IRLIGHT_TCP_SNMP_BASELINE_PATH=/run/irlight-monitor/proc-net-snmp.baseline \
bash scripts/check-network-egress-health.sh eth0 dual
```

current は既定 `/proc/net/snmp` を読み、fixture や明示した network namespace の snapshot だけ `IRLIGHT_TCP_SNMP_PATH` で上書きします。baseline は [tcp-connection-attempt-failure-monitoring.md](tcp-connection-attempt-failure-monitoring.md) の契約どおり、同じ host / network namespace / boot generation から operator / monitoring 側が用意します。aggregate 自身は baseline を作成・更新・削除しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、baseline/current 欠損・破損、counter reset、component timeout は `tcp_attempt_fails_status=UNKNOWN` として fail-closed します。`AttemptFails` の新規増加は `WARNING` です。この component 単独では `CRITICAL` にせず、link / route / NIC の確定 `CRITICAL` を `WARNING` / `UNKNOWN` が隠しません。

mode 未指定または `disabled` の場合、既存 stdout / exit code 形式を変更しません。有効時だけ `tcp_attempt_fails_status` を追加します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK tcp_attempt_fails_status=WARNING family=dual
```

current/baseline path、IP、port、destination、credential、counter の生値は aggregate 出力へ追加しません。

## Conntrack pressure の opt-in

既定では host / network namespace 全体の conntrack 使用率を aggregate に含めません。`nf_conntrack_count` / `nf_conntrack_max` は外向き通信障害と相関し得ますが、この signal だけで特定 Session、interface、destination の障害原因を確定できないためです。また既存利用者の stdout / exit code 契約を維持します。

`check-conntrack-pressure.sh` を同じ一次切り分けに含める場合だけ、`IRLIGHT_CONNTRACK_PRESSURE_MODE=enabled` を明示します。

```bash
IRLIGHT_CONNTRACK_PRESSURE_MODE=enabled \
bash scripts/check-network-egress-health.sh eth0 dual
```

checker は既定で `/proc/sys/net/netfilter/nf_conntrack_count` と `/proc/sys/net/netfilter/nf_conntrack_max` を read-only で読みます。fixture や明示した network namespace の snapshot を検査する場合は `IRLIGHT_CONNTRACK_COUNT_PATH` / `IRLIGHT_CONNTRACK_MAX_PATH` で上書きできます。warning / critical threshold は既存 checker 契約の `IRLIGHT_CONNTRACK_WARNING_PERCENT` / `IRLIGHT_CONNTRACK_CRITICAL_PERCENT` を再利用します。aggregate 自身は conntrack table や sysctl を変更しません。

mode は `disabled`（既定）または `enabled` のみです。不正 mode、count/max の欠損・破損、不正 threshold、component timeout、checker の異常 exit は `conntrack_pressure_status=UNKNOWN` として fail-closed します。使用率が critical threshold 以上なら `CRITICAL`、warning threshold 以上なら `WARNING`、それ未満なら `OK` です。集約優先順位は `CRITICAL > UNKNOWN > WARNING > OK` のままで、確認済み conntrack 枯渇を別 component の `UNKNOWN` が隠しません。

mode 未指定または `disabled` の場合、既存 stdout / exit code 形式を変更しません。有効時だけ `conntrack_pressure_status` を追加します。

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=WARNING link_status=OK ipv4_route_status=OK ipv6_route_status=OK conntrack_pressure_status=WARNING family=dual
```

conntrack は host / network namespace 全体の signal として扱い、特定 Session / interface / destination へ自動帰属しません。count/max、path、IP、port、credential の生値は aggregate 出力へ追加しません。

## Opt-in status field の保守契約

optional component の status field は、component ごとの出力分岐を組み合わせるのではなく、固定順の registry へ登録して最後に一度だけ stdout を組み立てます。現在の順序は `interface_errors_status` → `udp_snmp_errors_status` → `tcp_snmp_retransmits_status` → `tcp_listen_pressure_status` → `tcp_established_resets_status` → `tcp_attempt_fails_status` → `conntrack_pressure_status` です。disabled の component は registry に登録せず、legacy 出力を byte-for-byte 維持します。

新しい opt-in component を追加する場合は、component の exit code と status field を同じ登録処理へ渡し、status 表示と `CRITICAL > UNKNOWN > WARNING > OK` の severity merge を同時に更新します。個別の `printf` 分岐を増やしたり、field 登録だけ・severity merge だけを別々に追加しません。回帰テストでは all-disabled、各 component 単独、複数 component 同時有効の field 名・順序・exit code を固定します。

この整理は保守性のための内部契約であり、既存 mode、checker invocation、timeout、baseline/current path、安全境界、公開 stdout field 名・順序を変更するものではありません。

## Status 契約

exit code と status は既存の resource / network diagnostics と合わせます。

| exit | status | 意味 |
| ---: | --- | --- |
| 0 | `OK` | 必須 link / route がすべて利用可能 |
| 1 | `WARNING` | link が `dormant` / `testing` など注意状態、または opt-in 診断で新規 warning activity を検出 |
| 2 | `CRITICAL` | link down、必須 family の default route が欠落・利用不能、または opt-in component が確定 critical |
| 3 | `UNKNOWN` | target / mode / proc・sysfs record を安全に解釈できない、または component を制限時間内に評価できない |

集約優先順位は `CRITICAL > UNKNOWN > WARNING > OK` です。確認済みの障害を別 component の parse failure や timeout が隠さないためです。

出力例:

```text
IRLIGHT_NETWORK_EGRESS_HEALTH status=OK link_status=OK ipv4_route_status=OK ipv6_route_status=NOT_REQUIRED family=ipv4
```

非対象 family は `NOT_REQUIRED` と表示され、総合判定には入りません。`dual` は IPv4 と IPv6 の両方を明示的な運用要件として選ぶモードなので、片方だけ欠けても `CRITICAL` です。

## 安全境界

- interface 名、route table path、gateway address は結果へ出しません。
- secret、stream key、SRT passphrase、destination URL は扱いません。
- `ip route add/del`、link up/down、sysctl、service restart、Docker 操作は行いません。
- NIC error/drop opt-in でも sysfs / baseline を変更せず、baseline lifecycle は monitoring 側に残します。
- UDP SNMP opt-in でも current / baseline / proc / sysctl / socket を変更せず、baseline lifecycle は monitoring 側に残します。
- UDP SNMP counter は host / network namespace 全体の signal として扱い、特定 interface / Session へ自動帰属しません。
- TCP SNMP opt-in でも current / baseline / proc / sysctl / socket を変更せず、baseline lifecycle は monitoring 側に残します。
- TCP retransmission counter は host / network namespace 全体の signal として扱い、特定 interface / Session / destination へ自動帰属しません。
- TCP listener pressure opt-in でも current / baseline / proc / sysctl / socket / backlog を変更せず、baseline lifecycle は monitoring 側に残します。
- TCP listener pressure counter は host / network namespace 全体の signal として扱い、特定 Session / port / service へ自動帰属しません。
- TCP established reset opt-in でも current / baseline / proc / sysctl / socket / route / service を変更せず、baseline lifecycle は monitoring 側に残します。
- `EstabResets` は host / network namespace 全体の signal として扱い、特定 Session / destination / service へ自動帰属しません。
- TCP attempt failure opt-in でも current / baseline / proc / sysctl / socket / route / service を変更せず、baseline lifecycle は monitoring 側に残します。
- `AttemptFails` は host / network namespace 全体の signal として扱い、特定 Session / destination / service へ自動帰属しません。
- Conntrack pressure opt-in でも proc/sys の current 値、conntrack table、sysctl、socket、route、firewall を変更しません。
- Conntrack pressure は host / network namespace 全体の signal として扱い、特定 Session / interface / destination へ自動帰属しません。
- `dual` を自動推測しません。address-family policy は deployment ごとに operator が明示します。
- component timeout を無効化して unbounded execution に切り替える設定はありません。
- `CRITICAL` を見ても自動修復せず、provider / OS / network の状態と進行中 Session への影響を確認してから復旧操作を判断します。

## 切り分け

`link_status=CRITICAL` なら、まず interface / virtual NIC / provider network の状態を確認します。link が `OK` で route のみ `CRITICAL` なら、対象 family の default route と network configuration の変更履歴を read-only で確認します。`interface_errors_status=WARNING|CRITICAL` の場合は [network-interface-error-monitoring.md](network-interface-error-monitoring.md) で delta の意味と baseline generation を確認し、host / hypervisor / provider network、driver、queue、MTU、帯域飽和などを read-only で追加確認します。

`udp_snmp_errors_status=WARNING` の場合は [udp-snmp-error-monitoring.md](udp-snmp-error-monitoring.md) で baseline generation と各 delta の意味を確認し、socket buffer pressure、checksum error、host-wide UDP activity を read-only で追加確認します。`tcp_snmp_retransmits_status=WARNING` の場合は [tcp-snmp-retransmit-monitoring.md](tcp-snmp-retransmit-monitoring.md) で baseline generation を確認し、host-wide retransmission activity と Session / destination 側の reconnect、RTT、packet loss などの証跡を突き合わせます。`tcp_listen_pressure_status=WARNING` の場合は [tcp-listen-overflow-monitoring.md](tcp-listen-overflow-monitoring.md) で baseline generation と `ListenOverflows` / `ListenDrops` の意味を確認し、同時刻の connection failure、process FD、CPU / PSI、conntrack、listener backlog、service log などの証跡を突き合わせます。

`tcp_established_resets_status=WARNING` の場合は [tcp-established-reset-monitoring.md](tcp-established-reset-monitoring.md) で baseline generation を確認し、同じ観測期間の retransmit、NIC error/drop、egress reconnect/disconnect、複数 destination での同時発生を相関させます。`tcp_attempt_fails_status=WARNING` の場合は [tcp-connection-attempt-failure-monitoring.md](tcp-connection-attempt-failure-monitoring.md) で baseline generation を確認し、established reset、retransmit、route/NIC、connect/reconnect reason、destination ごとの偏りを相関させます。

`conntrack_pressure_status=WARNING|CRITICAL` の場合は [host-pressure-monitoring.md](host-pressure-monitoring.md) の threshold 契約を確認し、同時刻の connection churn、NAT/firewall、reconnect、provider/network の証跡を read-only で相関させます。conntrack pressure だけで特定 Session / destination の障害原因を断定せず、sysctl を自動調整したり conntrack table を flush したりしません。

TCP warning だけで特定 Session、port、service、destination の障害や経路異常を断定しません。特定 Session への影響は Session / process / destination 側の証跡で別途確認します。`UNKNOWN` の場合は proc/sysfs の欠落・権限・record 破損に加えて component timeout、baseline lifecycle、conntrack path / threshold、`timeout` utility の有無も確認し、「route がない」「NIC が壊れた」「UDP が詰まった」「TCP 経路が壊れた」「listener backlog が不足した」「接続先が reset した」「外向き connect が失敗した」「conntrack が枯渇した」と推測して設定を書き換えないでください。

DNS 名前解決、宛先固有の疎通、RTMPS/TLS、実 publish の成否はこの check の範囲外です。egress 障害時は [egress-widespread-failure.md](egress-widespread-failure.md) と、必要に応じて Destination verification の診断を併用します。
