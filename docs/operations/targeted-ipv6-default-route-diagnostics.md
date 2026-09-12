# Targeted IPv6 default route diagnostics

Issue #11 の network diagnostics を IPv6 egress まで補完する read-only check。host pressure や link `operstate=up` が正常でも、production interface に generic IPv6 default route がなければ IPv6 destination への egress は成立しないため、対象 interface を operator が明示した場合だけ評価する。

## Check

```bash
bash scripts/check-ipv6-default-route.sh <interface>
```

既定では Linux の `/proc/net/ipv6_route` を read-only で読む。fixture / container namespace などで route table を明示する場合だけ第2引数または `IRLIGHT_IPV6_ROUTE_TABLE` を使える。interface は `IRLIGHT_NETWORK_INTERFACE` でも指定できる。

Linux の `/proc/net/ipv6_route` record は destination、destination prefix length、source、source prefix length、next hop、metric、refcount、use、flags、interface の順に出力される。この check は対象 interface のうち次をすべて満たす record を generic default route として扱う。

- destination が `::` 相当の 32桁 zero hex
- destination prefix length が `00` (`/0`)
- source が `::` 相当の 32桁 zero hex
- source prefix length が `00` (`/0`)
- `RTF_UP` が立っている
- `RTF_REJECT` が立っていない

source-specific route は任意の local source に対する default egress を保証しないため、この check の generic default route には数えない。

## Status contract

- 利用可能な generic default route が1件以上ある: `OK`, exit `0`
- generic default entry はあるが down / reject: `CRITICAL` (`default_route_unusable`), exit `2`
- generic default entry がない: `CRITICAL` (`default_route_missing`), exit `2`
- target 不正、route table 読取不能、対象 record の構文不正、または壊れた record の所属 interface を安全に判定できない: `UNKNOWN`, exit `3`

`/proc/net/ipv6_route` には IPv4 の `/proc/net/route` のような header がないため、空 table は構文エラーではなく「対象 default route がない」として `CRITICAL` にする。有効な target default route を確認できた場合は、その route の存在という一点は成立するため、別の壊れた record があっても `OK` を返す。

出力に interface 名、next hop、route table path は反射しない。

```text
IRLIGHT_IPV6_DEFAULT_ROUTE status=OK route=default
```

## Scope and safety

この check は IPv6 generic default route の存在だけを確認する。実 packet の疎通、policy routing、network namespace の選択、firewall、DNS、NAT64、remote RTMP endpoint の到達性、packet loss、RTT は証明しない。policy-routing-only / source-routing-only deployment に機械的に適用せず、その topology に合った end-to-end signal を別途用意する。

診断は read-only で、`ip -6 route`、interface、sysctl、network namespace、firewall を変更しない。自動 route 追加・削除や network restart を復旧策として実行しない。

## Verification

```bash
python -m unittest discover -s tests -p 'test_ipv6_default_route.py' -v
bash -n scripts/check-ipv6-default-route.sh
```

IPv6 default route がある Linux host / network namespace では、production interface を明示した read-only probe も追加で確認する。IPv6 default route が存在しない環境では、その事実を `CRITICAL default_route_missing` として返すこと自体が期待動作であり、監視対象へ組み込むかは deployment topology に応じて決める。
