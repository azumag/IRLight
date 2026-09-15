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
- `dual` を自動推測しません。address-family policy は deployment ごとに operator が明示します。
- component timeout を無効化して unbounded execution に切り替える設定はありません。
- `CRITICAL` を見ても自動修復せず、provider / OS / network の状態と進行中 Session への影響を確認してから復旧操作を判断します。

## 切り分け

`link_status=CRITICAL` なら、まず interface / virtual NIC / provider network の状態を確認します。link が `OK` で route のみ `CRITICAL` なら、対象 family の default route と network configuration の変更履歴を read-only で確認します。`interface_errors_status=WARNING|CRITICAL` の場合は [network-interface-error-monitoring.md](network-interface-error-monitoring.md) で delta の意味と baseline generation を確認し、host / hypervisor / provider network、driver、queue、MTU、帯域飽和などを read-only で追加確認します。`udp_snmp_errors_status=WARNING` の場合は [udp-snmp-error-monitoring.md](udp-snmp-error-monitoring.md) で baseline generation と各 delta の意味を確認し、socket buffer pressure、checksum error、host-wide UDP activity を read-only で追加確認します。特定 Session への影響は Session / process / destination 側の証跡で別途確認します。`UNKNOWN` の場合は proc/sysfs の欠落・権限・record 破損に加えて component timeout、baseline lifecycle、`timeout` utility の有無も確認し、「route がない」「NIC が壊れた」「UDP が詰まった」と推測して設定を書き換えないでください。

DNS 名前解決、宛先固有の疎通、RTMPS/TLS、実 publish の成否はこの check の範囲外です。egress 障害時は [egress-widespread-failure.md](egress-widespread-failure.md) と、必要に応じて Destination verification の診断を併用します。
