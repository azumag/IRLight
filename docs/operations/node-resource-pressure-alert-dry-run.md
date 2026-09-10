# Media Node resource-pressure alert dry-run

`NODE_RESOURCE_PRESSURE` は、Media Node が resource pressure 状態へ近づいている可能性を検知する Warning alert です。Issue #11 の運用 alert を、Node 再起動、Session 停止、provider 操作、scale-up、外部通知を行わず read-only で確認するための dry-run を提供します。

## 対象契約

alert catalog の次の契約と一致する場合だけ評価します。

- alert ID: `NODE_RESOURCE_PRESSURE`
- severity: `warning`
- signal: `media_nodes.resource_pressure`
- trigger: `threshold` / `operations.node_resource_pressure`
- runbook: `docs/operations/media-node-heartbeat-stopped.md`

catalog がこの契約からずれている場合は `INVALID_CATALOG` とし、推測で評価を継続しません。

## resource_pressure の意味と単位

repository は `media_nodes.resource_pressure` の具体的な metric、単位、aggregation window、production threshold を決めません。CPU 使用率、memory pressure、disk I/O、network、load average などをこの evaluator 側で勝手に合成・換算すると、collector と alert の意味がずれて誤警告や見逃しにつながるためです。

この dry-run に渡す `resource_pressure` は、deployment / collector 側で事前に定義・集計済みの有限な非負 scalar とします。`--threshold` は必ず同じ metric・同じ単位で指定してください。異なる metric や単位の observation を同じ batch に混在させないでください。

metric の定義や単位を production 契約として標準化する場合は、collector、dashboard、alert routing、runbook を同時に更新する別判断として扱い、この dry-run だけで暗黙に決定しません。

## 入力

collector 等で事前に集計した scalar を JSONL で標準入力へ渡します。1 record は次の exact field だけを受理します。

```json
{"resource_pressure":11}
```

`node_id`、`session_id`、`reason_code`、host/provider ID、process 情報、raw metric、credential などの追加 field は受理しません。この dry-run は threshold contract の read-only 検証用であり、catalog の dedup/routing key を入力・出力する alert dispatcher ではありません。

入力 record は 4 KiB を上限とし、invalid UTF-8、duplicate JSON key、`NaN` / `Infinity`、負値、bool、文字列、null、不正 JSON を fail-closed に扱います。batch 内に不正 record が1件でもある場合、先に一致した record があっても部分結果を alert として返しません。

## Threshold

production threshold は repository では決めません。通常時・高負荷時の実測、collector の aggregation window、Node shape、workload、ユーザー影響を確認した上で deployment / operator が同一単位の `--threshold` を明示します。

```bash
printf '%s\n' '{"resource_pressure":11}' | \
  python apps/control-api/operations_node_resource_pressure_alerts.py \
    --repo-root . \
    --threshold 10
```

判定は `resource_pressure > threshold` です。境界値と同値は一致させません。検証用に threshold `0` を指定しても、pressure `0` の observation を Warning と誤分類しません。

## 出力

出力は固定 alert ID と aggregate 件数に限定します。

```json
{"matched_alerts":{"NODE_RESOURCE_PRESSURE":1},"records":1,"status":"MATCHED","unmatched_records":0,"violations":{}}
```

入力した scalar 値、Node / Session / host / provider 識別子、reason code、credential は echo しません。

## 安全境界

この dry-run は host resource、cgroup、Docker stats、Node Agent、provider API を直接 probe しません。また、次の操作を行いません。

- Node restart / drain / stop / recreate
- Session の停止、再配置、二重割当
- provider resource の作成・削除・shape 変更・scale-up
- 課金を伴う capacity 増強
- process kill、cache/drop、filesystem cleanup
- alert / paging / webhook 等の外部通知

Warning が一致しても、resource の種類や根因をこの aggregate scalar だけから推測しません。まず既存の `media-node-heartbeat-stopped.md` にある read-only な Node / host / Control Plane 切り分けを優先し、resource 固有の対応は実測に基づいて判断します。provider shape 変更や Node 増設など費用・capacity に影響する操作は自動実行しません。

## 検証観点

回帰テストでは最低限、次を固定します。

- threshold より大きい observation だけが一致すること
- threshold と同値、および zero/zero が誤警告にならないこと
- production threshold や metric unit を repository が暗黙に決めないこと
- invalid UTF-8、duplicate key、non-finite、負値、oversized record を fail-closed にすること
- Node / Session / reason code 等の追加 field を受理しないこと
- invalid batch では先行一致を抑制すること
- alert catalog の severity / signal / trigger / runbook drift を拒否すること
- 出力に入力識別子や raw metric を再表示しないこと

この dry-run の成功は実 Node の resource 正常性を保証しません。production 導入時は collector metric の意味・単位・window・threshold と、実際の Node resource evidence が一致していることを別途確認します。
