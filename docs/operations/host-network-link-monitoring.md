# Host network link monitoring

Issue #11 の host-level monitoring で、production egress に使う Linux network interface の operational state を `check-host-pressure.sh` の aggregate に明示的に参加させるための手順を定義する。

## Default contract

network interface は physical NIC、bond、bridge、VLAN、virtual device など deployment ごとに異なり、誤った interface を自動選択すると正常性を誤判定する。そのため `IRLIGHT_HOST_NETWORK_LINK_MODE` の既定値は `disabled` とし、通常の `check-host-pressure.sh` 出力には `network_link_status` を追加しない。

standalone check は次で実行できる。

```bash
bash scripts/check-network-link-health.sh /sys/class/net/<production-interface>
```

## Aggregate opt-in

監視対象を運用側で確定できる場合だけ、interface の sysfs directory を明示して有効化する。

```bash
IRLIGHT_HOST_NETWORK_LINK_MODE=enabled \
IRLIGHT_NETWORK_INTERFACE_DIR=/sys/class/net/<production-interface> \
  bash scripts/check-host-pressure.sh
```

有効化すると aggregate に `network_link_status=OK|WARNING|CRITICAL|UNKNOWN` が追加され、全体 status は既存 contract の `CRITICAL > UNKNOWN > WARNING > OK` で決まる。

`IRLIGHT_HOST_NETWORK_LINK_MODE` は `enabled` / `disabled` のみを受け付ける。未知値は silent disable せず、aggregate 自体を `UNKNOWN reason=invalid_network_link_mode` にする。

`enabled` なのに `IRLIGHT_NETWORK_INTERFACE_DIR` が空、directory/`operstate` を読めない、または値を安全に解釈できない場合は standalone checker の `UNKNOWN` をそのまま伝播する。aggregate は interface を推測・探索しない。

## Status contract

standalone checker の read-only contract をそのまま利用する。

- `up`: `OK`
- `dormant` / `testing`: `WARNING`
- `down` / `lowerlayerdown` / `notpresent`: `CRITICAL`
- `unknown`: `UNKNOWN`
- target 欠落、読取不能、複数行、未知 token: `UNKNOWN`

この signal は link operational state の一点観測であり、帯域飽和、packet loss、RTT、route、DNS、firewall、NAT、remote ingest reachability を証明しない。必要に応じて targeted route/resolver checks や end-to-end destination probe と組み合わせる。

## Safety

aggregate と standalone checker は sysfs の `operstate` を読むだけで、`ip link set`、bond/bridge/VLAN 設定変更、route 変更、NetworkManager/systemd-networkd restart、service restart、Node drain、provider migration、instance resize、failover を実行しない。

link failure を検知しても自動復旧へ直結させず、まず対象 interface と network topology、別経路の有無、配信 Session への影響を確認する。外部課金や topology 変更を伴う対応は operator の明示判断に残す。

## Verification

```bash
python -m unittest discover -s tests -p 'test_network_link_health.py' -v
python -m unittest discover -s tests -p 'test_host_network_link_aggregate.py' -v
python -m unittest discover -s tests -p 'test_host_pressure_opt_in_matrix.py' -v
bash -n scripts/check-network-link-health.sh scripts/check-host-pressure.sh
```
