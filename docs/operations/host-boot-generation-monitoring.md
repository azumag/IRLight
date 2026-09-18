# Host boot generation monitoring

Linux の累積 counter を baseline と比較する診断では、host 再起動後に counter が小さくなることがある。`scripts/check-host-boot-generation.sh` は `/proc/sys/kernel/random/boot_id` を operator-managed baseline と比較し、baseline が現在の boot generation と同じかを read-only で確認する targeted diagnostic である。

この診断は reboot 自体の異常判定ではない。planned maintenance、kernel update、provider 側の再起動、host reprovision でも boot ID は変わる。目的は、OOM / softnet / NIC / TCP / UDP 等の累積 counter baseline を「同じ boot の値」と誤認しないための補助 signal を提供することにある。

## 実行

baseline は checker が作成・更新しない。現在の boot を監視開始時点として明示的に採用する場合だけ operator が保存する。

```bash
install -d -m 0700 /var/lib/irlight-monitor
cat /proc/sys/kernel/random/boot_id \
  > /var/lib/irlight-monitor/boot_id.baseline
chmod 0600 /var/lib/irlight-monitor/boot_id.baseline
```

その後、次のように比較する。

```bash
bash scripts/check-host-boot-generation.sh \
  /proc/sys/kernel/random/boot_id \
  /var/lib/irlight-monitor/boot_id.baseline
```

または baseline だけを環境変数で指定できる。

```bash
IRLIGHT_BOOT_ID_BASELINE_PATH=/var/lib/irlight-monitor/boot_id.baseline \
  bash scripts/check-host-boot-generation.sh
```

再起動をまたいで検知したい場合、baseline は `/run` のような reboot 時に消える場所へ置かない。逆に ephemeral host で generation を跨いだ比較が意味を持たない deployment では、この checker を無理に有効化しない。

## Status contract

| exit | status | reason | 意味 |
| ---: | --- | --- | --- |
| 0 | `OK` | `same_boot` | current と baseline が同じ boot generation |
| 1 | `WARNING` | `boot_generation_changed` | boot generation が変わった、または別 host / 別 generation の baseline を参照している |
| 3 | `UNKNOWN` | `current_boot_id_unavailable` / `baseline_boot_id_unavailable` / `invalid_current_boot_id` / `invalid_baseline_boot_id` | current / baseline を安全に比較できない |

boot ID 本文や file path は通常出力しない。値そのものは credential ではないが、不要な host identity 情報を監視イベントへ増やさず、固定 reason code だけで運用判断できるようにする。

## WARNING 時の判断

`boot_generation_changed` を検知したら、まず Node heartbeat、進行中 Session、ingest / continuity / egress の状態を確認し、意図した maintenance/reboot か、予期しない host restart / reprovision かを切り分ける。

同時に、次のような累積 counter baseline は stale とみなす。

- `/proc/vmstat` の `oom_kill`
- `/proc/net/softnet_stat`
- `/sys/class/net/<iface>/statistics/*`
- `/proc/net/snmp` / `/proc/net/netstat` の TCP / UDP counter

予期した reboot であることと workload の復旧を確認した後に限り、各 diagnostic の baseline と boot ID baseline を同じ確認時点で取り直す。checker 自身は baseline を自動更新しない。自動更新すると、予期しない reboot の証拠を正常化してしまうためである。

## Safety boundary

- read-only diagnostic であり、reboot、service restart、sysctl、network 設定、counter reset、baseline 更新を行わない。
- baseline 未設定・読取不能・形式不正を `OK` に丸めない。
- `boot_generation_changed` は host 異常を単独で断定しない。planned reboot でも発生する。
- deployment policy と persistent baseline が必要なため、既定の `check-host-pressure.sh` aggregate へ自動追加しない。
- 差分 counter の `counter_reset` を見たときは、boot generation の変更と合わせて判断し、単純に baseline を上書きして原因を消さない。
