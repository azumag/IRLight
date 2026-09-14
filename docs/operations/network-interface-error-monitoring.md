# Network interface error/drop delta monitoring

Media Node の network interface で、前回の正常な監視基準点以降に増えた packet error / drop counter を read-only で切り分けるための診断です。Issue #11 の `CPU/memory/disk/network` 監視と egress 障害の一次切り分けを補助します。

この診断は `/sys/class/net/<interface>/statistics` の累積 counter を直接「異常値」と判定しません。interface が長期間稼働している場合、過去の error/drop が counter に残り続けるため、同一 interface generation の baseline と current の差分だけを評価します。

## 対象 counter

`check-network-interface-errors.sh` は次の4 counter を確認します。

- `rx_errors`: 受信 error
- `tx_errors`: 送信 error
- `rx_dropped`: 受信 drop
- `tx_dropped`: 送信 drop

error の新規増加は通信品質の明確な異常として `CRITICAL`、drop のみの新規増加は混雑・queue・driver 等の追加確認が必要な兆候として `WARNING` にします。counter が増えていない場合は `OK` です。

## Baseline 契約

baseline は監視側が script 実行前に作成・更新します。script 自身は sysfs も baseline も変更しません。

例として `eth0` の baseline を監視専用 directory に保存する場合:

```bash
interface=eth0
baseline=/run/irlight-monitor/${interface}.statistics.baseline
install -d -m 0700 "$baseline"
for counter in rx_errors tx_errors rx_dropped tx_dropped; do
  cat "/sys/class/net/$interface/statistics/$counter" > "$baseline/$counter"
done
```

baseline は **同じ interface generation** に対してだけ再利用します。VM / NIC の再作成、interface の再生成、driver reset 等で counter が小さくなった場合、診断は `UNKNOWN reason=counter_reset` に fail-closed します。その状態を「改善」と解釈せず、interface generation と変更履歴を確認してから新しい baseline を取得してください。

baseline 更新は監視周期の設計に依存します。異常検知直後に自動で baseline を上書きすると継続中の障害を隠すため、alert の acknowledgement / recovery 契約と分離してください。

## 実行

```bash
bash scripts/check-network-interface-errors.sh \
  /sys/class/net/eth0/statistics \
  /run/irlight-monitor/eth0.statistics.baseline
```

環境変数でも指定できます。

```bash
IRLIGHT_NETWORK_STATS_DIR=/sys/class/net/eth0/statistics \
IRLIGHT_NETWORK_STATS_BASELINE_DIR=/run/irlight-monitor/eth0.statistics.baseline \
bash scripts/check-network-interface-errors.sh
```

出力例:

```text
IRLIGHT_NETWORK_INTERFACE_ERRORS status=WARNING reason=interface_drop_activity rx_errors_delta=0 tx_errors_delta=0 rx_dropped_delta=3 tx_dropped_delta=0
```

interface 名や path は出力しません。監視側は実行対象との対応を自身の inventory で保持してください。

## Status 契約

| exit | status | 意味 |
| ---: | --- | --- |
| 0 | `OK` | error/drop counter の新規増加なし |
| 1 | `WARNING` | `rx_dropped` / `tx_dropped` の新規増加あり、error の増加なし |
| 2 | `CRITICAL` | `rx_errors` / `tx_errors` の新規増加あり |
| 3 | `UNKNOWN` | current/baseline を安全に読めない、record が不正、または counter reset を検出 |

`CRITICAL` は `WARNING` より優先します。例えば drop と error が同時に増えた場合は `CRITICAL` です。

counter は非負10進整数として厳密に読み、Bash の安全な整数演算範囲として signed 64-bit 最大値までを受け付けます。欠損、複数行、負数、非数値、範囲外は `UNKNOWN` にします。

## 安全境界

- `/sys/class/net/.../statistics` と明示 baseline の読み取りだけを行います。
- interface up/down、route、qdisc、sysctl、firewall、service、container、provider resource は変更しません。
- baseline file の作成・更新・削除は行いません。
- interface 名、filesystem path、MAC/IP address、gateway、destination URL、stream key 等を結果へ出しません。
- `CRITICAL` / `WARNING` でも自動 restart、route 変更、NIC 再作成などの復旧操作は行いません。

## 切り分け

error/drop delta が増えた場合は、[targeted-network-egress-health.md](targeted-network-egress-health.md) で link state と必須 default route を合わせて確認します。link/route が正常でも counter が増える場合は、host / hypervisor / provider network、driver、queue、MTU、帯域飽和などを read-only で追加確認します。

この診断だけでは DNS、firewall/NAT、宛先固有の route、TLS、RTMP/SRT publish、provider 側到達性を証明しません。egress 全体の失敗では [egress-widespread-failure.md](egress-widespread-failure.md) の手順を優先してください。
