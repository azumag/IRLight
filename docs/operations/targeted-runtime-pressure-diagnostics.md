# Targeted runtime pressure diagnostics

Issue #11 の host-level pressure 監視を補完するため、監視対象 workload / network link / route を明示できる場合だけ使う read-only companion checks を提供する。

`check-host-pressure.sh` の `OK` は個別 cgroup / process の上限や、特定の production network interface・その IPv4 egress route が利用可能であることを意味しない。この手順では cgroup v2 の PSI stall、単一 process の file descriptor soft limit への接近、明示した Linux network interface の operational state、同 interface の IPv4 default route を別々に確認する。

## cgroup v2 PSI

対象 cgroup の `cpu.pressure` / `memory.pressure` / `io.pressure` を読み取る。

```bash
bash scripts/check-cgroup-psi-pressure.sh /sys/fs/cgroup/<target>
```

対象 directory は `IRLIGHT_CGROUP_PSI_DIR` でも指定できる。既定閾値は host PSI と同じく、`some avg10` が 25% / 50%、memory/io の `full avg10` が 5% / 20% で warning / critical。必要なら次で個別調整する。

- `IRLIGHT_CGROUP_PSI_SOME_WARNING_PERCENT`
- `IRLIGHT_CGROUP_PSI_SOME_CRITICAL_PERCENT`
- `IRLIGHT_CGROUP_PSI_FULL_WARNING_PERCENT`
- `IRLIGHT_CGROUP_PSI_FULL_CRITICAL_PERCENT`

出力例:

```text
IRLIGHT_CGROUP_PSI_PRESSURE status=WARNING cpu_some_avg10=27.50 memory_some_avg10=1.00 memory_full_avg10=0.00 io_some_avg10=1.00 io_full_avg10=0.00 some_warning_percent=25 some_critical_percent=50 full_warning_percent=5 full_critical_percent=20
```

CPU は `some`、memory/io は `some` と `full` を評価する。`avg10` だけで判定するが、同じ record の `avg60` / `avg300` も構文・範囲検証し、NaN / Infinity / 100%超過 / 重複 record / 読取不能は `UNKNOWN` に fail-closed する。

この check は指定した cgroup の stall time を観測するだけで、原因 process、host 全体の余裕、parent cgroup の制約、`memory.max` / `pids.max` の headroom を推測しない。対象 cgroup が不明確な環境では root/current cgroup を機械的に選ばず、監視対象を運用側で決める。

## Process file descriptor pressure

system-wide `file-nr` が正常でも、個別 process の `RLIMIT_NOFILE` soft limit に近づくと、その process だけが socket/file open に失敗し得る。対象 PID を明示し、その `/proc/<pid>/fd` と `/proc/<pid>/limits` を指定する。

```bash
PID=<target-pid>
bash scripts/check-process-fd-pressure.sh \
  "/proc/${PID}/fd" \
  "/proc/${PID}/limits"
```

path は `IRLIGHT_PROCESS_FD_DIR` / `IRLIGHT_PROCESS_LIMITS_PATH` でも指定できる。既定閾値は 80% / 90% で、`IRLIGHT_PROCESS_FD_WARNING_PERCENT` / `IRLIGHT_PROCESS_FD_CRITICAL_PERCENT` で変更できる。

有限 soft limit では `open fd count / soft limit` を評価する。soft limit `0` は新規 descriptor headroom がないため `CRITICAL` (`NO_HEADROOM`)。limit を実行中に引き下げた場合などに fd count が soft limit を超えていても telemetry corruption と推測せず `CRITICAL` (`OVER_LIMIT`) とする。soft limit が `unlimited` なら local percentage を発明せず `OK` (`usage_percent=NA`) とするが、system-wide file handle や cgroup / service manager 側の別制約まで安全という意味ではない。

`Max open files` record の欠落・重複・型不正・soft > hard、対象の読取不能、非数値 fd entry は `UNKNOWN` にする。対象 process が列挙中に消えた場合も正常扱いしない。

出力例:

```text
IRLIGHT_PROCESS_FD_PRESSURE status=WARNING usage_percent=82 open_fds=820 soft_limit=1000 hard_limit=4096 warning_percent=80 critical_percent=90
```

## Linux network link operational state

host 全体の CPU / memory / PSI が正常でも、配信経路に使う NIC や VLAN の operational state が `down` / `lowerlayerdown` なら通信は成立しない。対象 interface は環境依存なので自動選択せず、`/sys/class/net/<interface>` を明示する。

```bash
bash scripts/check-network-link-health.sh /sys/class/net/<interface>
```

`IRLIGHT_NETWORK_INTERFACE_DIR` でも対象 directory を指定できる。Linux kernel が公開する `operstate` を read-only で読み、次の固定 contract で評価する。

- `up`: `OK`
- `dormant` / `testing`: `WARNING`。L1 が存在しても通常 traffic を流せる状態とは断定しない
- `down` / `lowerlayerdown` / `notpresent`: `CRITICAL`
- `unknown`: kernel/driver が operational state を確定できていないため `UNKNOWN`
- 欠落、複数行、未知の token、読取不能: `UNKNOWN`

`unknown` は「link down」と同義ではないため Critical に格上げしない。一方で監視側から `OK` と推測もしない。bridge、bond、VLAN、virtual device などでは意味のある監視対象を operator が明示し、interface 名や path を output に反射しない。

出力例:

```text
IRLIGHT_NETWORK_LINK_HEALTH status=OK operstate=up
```

この check は link state の一点観測であり、帯域飽和、packet loss、RTT、route/DNS、remote ingest reachability、firewall policy を証明しない。それらは別の signal / end-to-end probe と組み合わせる。

## Targeted IPv4 default route

link が `up` でも、配信先へ出る interface に IPv4 default route がなければ IPv4 destination への egress は成立しない。対象 interface は自動選択せず、production の routing 設計で利用する名前を明示する。

```bash
bash scripts/check-ipv4-default-route.sh <interface>
```

既定では Linux の `/proc/net/route` を read-only で読む。fixture / container namespace 等で route table を明示する場合だけ第2引数または `IRLIGHT_IPV4_ROUTE_TABLE` を使える。interface は `IRLIGHT_NETWORK_INTERFACE` でも指定できる。

判定は対象 interface の `Destination=00000000` / `Mask=00000000` の entry に限定し、`RTF_UP` が立ち `RTF_REJECT` が立っていない default route が1件以上あれば `OK` とする。default entry が存在しても down / reject なら `CRITICAL` (`default_route_unusable`)、default entry 自体がなければ `CRITICAL` (`default_route_missing`)。対象 interface の route record が壊れていて安全に不存在を断定できない場合、table header 不正、読取不能、target 不正は `UNKNOWN` に fail-closed する。有効な default route が確認できた場合は、別の壊れた target record があっても「利用可能 route の存在」というこの check の一点だけは成立するため `OK` とする。

出力例:

```text
IRLIGHT_IPV4_DEFAULT_ROUTE status=OK route=default
```

この check は IPv4 default route の存在だけを確認する。route が実際に packet を通すこと、policy routing / network namespace / firewall / NAT / DNS / IPv6 route / remote RTMP endpoint の到達性は証明しない。IPv6-only または policy-routing-only の deployment に機械的に適用せず、その topology 用の別 signal を定義する。gateway、interface 名、route table path は output に反射しない。

## Exit code

4つの check とも同じ status / exit contract を使う。

| exit | status | meaning |
| ---: | --- | --- |
| 0 | `OK` | 対象 signal は正常範囲、明示された local limit が unlimited、または必要な route が確認できた |
| 1 | `WARNING` | warning 閾値以上 critical 未満、または link が transitional state |
| 2 | `CRITICAL` | critical 閾値以上、headroom がない / over-limit、明示した link が利用不能、または必要な IPv4 default route がない / unusable |
| 3 | `UNKNOWN` | 対象を安全に読み取り・評価できない |

## Safety

すべて診断専用で、cgroup control file、rlimit、process、socket、network interface、route、authority stateを変更しない。kill/restart、fd close、`prlimit`、`ip link set`、`ip route`、sysctl、cache drop、cleanupを自動実行しない。結果はまず alert / 診断へ接続し、復旧は対象 Session / process / interface / route / state ownership を確認した runbook の明示操作として行う。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_psi_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_process_fd_pressure_check.py' -v
python -m unittest discover -s tests -p 'test_network_link_health.py' -v
python -m unittest discover -s tests -p 'test_ipv4_default_route.py' -v
bash -n \
  scripts/check-cgroup-psi-pressure.sh \
  scripts/check-process-fd-pressure.sh \
  scripts/check-network-link-health.sh \
  scripts/check-ipv4-default-route.sh
```
