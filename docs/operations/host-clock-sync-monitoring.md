# Host clock synchronization monitoring

Issue #11 の timestamp / correlation 信頼性を補完するため、Linux host の systemd time synchronization 状態を副作用なく確認する targeted check を定義する。

Session event、認証期限、TLS 証明書時刻、障害時系列の相関では host clock の大きなずれが診断を難しくする。一方、IRLight が NTP daemon や provider の時刻同期方針を勝手に変更するべきではないため、この check は状態を読むだけとし、既定の `check-host-pressure.sh` aggregate には自動追加しない。

## Check

```bash
python3 scripts/check-host-clock-sync.py
```

checker は `timedatectl show --property=NTPSynchronized --value` を実行し、`yes` / `no` だけを受理する。子 process には既定 3 秒の timeout を設け、`IRLIGHT_CLOCK_SYNC_TIMEOUT_SECONDS` で 1〜60 秒の範囲に変更できる。

テストや明示的な deployment wrapper では `IRLIGHT_TIMEDATECTL_BIN` で実行ファイルを指定できる。通常運用では既定の `timedatectl` を使用する。

### Result

- `OK` / exit `0`: `NTPSynchronized=yes`。
- `WARNING` / exit `1`: `NTPSynchronized=no`。reason は `ntp_unsynchronized`。
- `UNKNOWN` / exit `3`: command 不在、timeout、command failure、予期しない出力、timeout 設定不正。状態を推測して `OK` にしない。

例:

```text
IRLIGHT_HOST_CLOCK_SYNC status=OK reason=none ntp_synchronized=yes
IRLIGHT_HOST_CLOCK_SYNC status=WARNING reason=ntp_unsynchronized ntp_synchronized=no
IRLIGHT_HOST_CLOCK_SYNC status=UNKNOWN reason=timedatectl_timeout
```

## Safety boundary

- read-only diagnostic であり、NTP の enable/disable、時刻補正、daemon restart、`timedatectl set-ntp`、provider 設定変更を行わない。
- `timedatectl` の stderr、実行ファイル path、内部例外を通常出力へ転記しない。
- `ntp_unsynchronized` 単独で Session 障害や provider 障害を断定しない。event timestamp の不整合、TLS/auth の時刻依存エラー、node heartbeat 等と相関して判断する。
- systemd / `timedatectl` を使わない host は `UNKNOWN` になり得るため、deployment ごとの time-sync mechanism を確認してから監視へ組み込む。
- この checker 自身は remediation を実行しない。NTP source、chrony/systemd-timesyncd/provider agent 等の選択は運用判断として別途行う。

## Operator response

`WARNING` の場合は、まず read-only に現在時刻、同期 daemon の状態、provider 側の時刻同期要件、関連する Session/event timestamp を確認する。意図した time source が決まっていない環境で checker を根拠に daemon を切り替えない。

`UNKNOWN` の場合は command の有無・権限・timeout と、その host が systemd time synchronization を利用する設計かを確認する。診断不能を「同期済み」とみなさない。

Refs #11
