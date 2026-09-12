# Resource pressure check implementation contract

IRLight の read-only resource pressure checks は、観測対象ごとの意味を保ったまま、数値入力の検証と threshold 計算を共通化します。この文書は運用時の出力契約と、実装を変更するときに崩してはいけない境界をまとめます。

## 共通 status / exit code

- `OK`: exit `0`
- `WARNING`: exit `1`
- `CRITICAL`: exit `2`
- `UNKNOWN`: exit `3`

診断不能、破損した入力、契約外の値は正常と推測せず `UNKNOWN` へ fail closed します。ただし、観測対象の仕様上「有効だが headroom がない」「一時的に上限超過が成立し得る」状態は wrapper 固有の policy に従い `CRITICAL` とする場合があります。

## Host aggregate

`scripts/check-host-pressure.sh` は host-level component を一つの registry に登録し、その同じ registry から overall status と `<component>_status` の出力を生成します。新しい component を追加するときは aggregation 条件と出力 format を別々に更新せず、registry へ一度だけ追加します。

Overall severity は `CRITICAL > UNKNOWN > WARNING > OK` です。既知の `CRITICAL` は別 component の診断不能 (`UNKNOWN`) で隠しません。一方、既知の critical がない場合は `UNKNOWN` を `WARNING` / `OK` より優先し、診断不能を正常側へ丸めません。個々の component status は overall status に関係なく出力し、複数 severity が同時に発生したときも原因の切り分けに使えるよう維持します。

## Scalar checks

`scripts/lib/scalar-pressure-common.sh` は次の機械的な処理だけを担当します。

- unsigned signed-64-bit 範囲の整数検証と先頭ゼロの10進正規化
- warning / critical threshold の検証と10進正規化
- `current <= maximum` の有限比率から usage percent を計算

観測 path、environment variable namespace、出力 prefix、reason code、`max` / `unlimited` sentinel、zero-headroom / over-limit の意味は各 wrapper が保持します。たとえば cgroup の有限上限超過は有効な critical state になり得ますが、conntrack の `count > max` は telemetry contract の矛盾として `UNKNOWN` です。共通 helper に domain policy を押し込まないでください。

現在この scalar contract を使う checks は cgroup PID、cgroup memory、conntrack、system file handles、system-wide tasks、target process file descriptors です。

## PSI checks

Linux PSI は record parsing、basis-point conversion、`some` / `full` aggregation が scalar ratio と異なるため、`scripts/lib/psi-pressure-common.sh` の独立 contract を使います。scalar helper と PSI helper を無理に統合しません。

## 安全性

これらの checks は read-only diagnostics です。process kill / restart、cgroup や sysctl の変更、conntrack flush、filesystem cleanup、cache drop、Docker prune などの復旧操作を helper や wrapper へ追加しません。変更操作が必要な場合は対応する runbook と明示的な運用判断へ分離します。

実装を変更するときは既存 wrapper の output fields / reason code / exit code を維持し、各 wrapper の境界テストと shared-contract test の両方を通してください。
