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

## Status 契約

exit code と status は既存の resource / network diagnostics と合わせます。

| exit | status | 意味 |
| ---: | --- | --- |
| 0 | `OK` | 必須 link / route がすべて利用可能 |
| 1 | `WARNING` | link が `dormant` / `testing` など注意状態 |
| 2 | `CRITICAL` | link down、または必須 family の default route が欠落・利用不能 |
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
- `dual` を自動推測しません。address-family policy は deployment ごとに operator が明示します。
- component timeout を無効化して unbounded execution に切り替える設定はありません。
- `CRITICAL` を見ても自動修復せず、provider / OS / network の状態と進行中 Session への影響を確認してから復旧操作を判断します。

## 切り分け

`link_status=CRITICAL` なら、まず interface / virtual NIC / provider network の状態を確認します。link が `OK` で route のみ `CRITICAL` なら、対象 family の default route と network configuration の変更履歴を read-only で確認します。`UNKNOWN` の場合は proc/sysfs の欠落・権限・record 破損に加えて component timeout の発生や `timeout` utility の有無も確認し、「route がない」と推測して設定を書き換えないでください。

DNS 名前解決、宛先固有の疎通、RTMPS/TLS、実 publish の成否はこの check の範囲外です。egress 障害時は [egress-widespread-failure.md](egress-widespread-failure.md) と、必要に応じて Destination verification の診断を併用します。
