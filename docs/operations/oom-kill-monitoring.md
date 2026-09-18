# Host OOM kill delta monitoring

Linux host 全体で OOM killer が実際に process を終了した事実を、`/proc/vmstat` の `oom_kill` counter 差分から read-only に検出するための targeted diagnostic です。Issue #11 の「OOM は Critical」を、cgroup 単位の memory event だけでは見落とし得る host-wide signal でも確認します。

## 実行

同じ host・boot generation で採取した過去の `/proc/vmstat` snapshot を operator-managed baseline として渡します。

```bash
scripts/check-oom-kill-delta.sh /proc/vmstat /var/lib/irlight-observer/vmstat.baseline
```

path は positional argument のほか、`IRLIGHT_VMSTAT_PATH` と `IRLIGHT_VMSTAT_BASELINE_PATH` でも指定できます。baseline は checker が作成・更新・削除しません。採取・世代管理・rotation は監視基盤側で行ってください。

## Host aggregate への opt-in

baseline の世代管理を行える deployment では、host aggregate に OOM kill signal を明示的に追加できます。

```bash
IRLIGHT_HOST_OOM_KILL_MODE=enabled \
IRLIGHT_VMSTAT_BASELINE_PATH=/var/lib/irlight-observer/vmstat.baseline \
bash scripts/check-host-pressure.sh
```

既定値は `IRLIGHT_HOST_OOM_KILL_MODE=disabled` であり、既存の `IRLIGHT_HOST_PRESSURE` 出力契約は変えません。`enabled` の場合だけ `oom_kill_status=OK|CRITICAL|UNKNOWN` を追加し、aggregate の既存 severity ordering `CRITICAL > UNKNOWN > WARNING > OK` に参加させます。

`enabled` なのに baseline が未設定・読取不能・別 generation 由来で counter reset が起きた場合は checker の `UNKNOWN` をそのまま aggregate へ伝播します。不正な mode 値も `UNKNOWN reason=invalid_oom_kill_mode` に fail closed します。aggregate 自身は baseline を作成・更新・削除しません。

## status 契約

- `OK` / exit `0`: `oom_kill` counter に増加がない。
- `CRITICAL` / exit `2`: baseline から `oom_kill` が 1 以上増加した。`reason=oom_kill_detected` と `oom_kill_delta` を返す。
- `UNKNOWN` / exit `3`: current / baseline を読めない、`oom_kill` が欠落・重複・不正、counter が減少した、baseline 未設定などで比較を保証できない。

OOM kill は既に workload が kernel によって終了された事実なので、checker 単独で `WARNING` には丸めず `CRITICAL` にします。`UNKNOWN` を `OK` と解釈しないでください。

## baseline の世代

`oom_kill` は boot 中に単調増加する counter です。baseline と current は同じ host と boot generation のものを使います。counter が減少した場合は `counter_reset` として `UNKNOWN` に fail closed します。ただし reboot 後に counter が baseline と偶然同値以上になるケースは counter 値だけでは generation を証明できないため、監視基盤は boot change を検知したら baseline を明示的に更新してください。

baseline を更新するときは、更新前の `CRITICAL` event と関連ログを保持し、単に snapshot を上書きして障害証跡を消さないでください。

## 一次対応

`CRITICAL` の場合は直ちに同じ時間帯の kernel log、system journal、host memory / PSI、cgroup `memory.events`、対象 process / Session の crash や restart を照合します。OOM の原因を確認せず service restart、cache drop、swap 設定変更、cgroup limit 変更、process kill、node drain を自動実行しません。

この checker 自体は `/proc/vmstat` と operator-managed baseline の読み取りだけを行い、process、service、cgroup、sysctl、swap、filesystem、provider resource を変更しません。
