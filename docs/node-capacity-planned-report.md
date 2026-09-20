# Plan-bound Node capacity report assembly

Issue #13 の Node capacity evidence では、raw trial の schema が正しいだけでなく、承認済み canonical load plan にある負荷レベルをすべて測定したことを report 生成前に確認します。

従来の `assemble-node-capacity-report.py` は raw trial と report 自体の厳密な schema を検証しますが、意図的に load plan を入力に持ちません。そのため、harness が途中で停止して `[1, 2, 4]` だけが残った場合でも、その raw trial だけを単独の report として組み立てることは可能でした。後段の coverage manifest validator は不足を拒否しますが、途中 report が complete evidence に見える余地を減らすため、canonical plan と同時に検証する entry point を追加します。

## 推奨コマンド

`run-node-capacity-scenario.py --failure-policy continue` で planned level 全件を採取した後、次のように report を生成します。

```bash
python3 scripts/assemble-node-capacity-planned-report.py \
  --plan artifacts/node-capacity-plan.json \
  --trials-jsonl artifacts/normal-input.complete.trials.jsonl \
  --scenario normal-input \
  --run-id 11111111-2222-4333-8444-555555555555 \
  --node-profile 'example-node-shape' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --safety-margin-percent 20 \
  --output docs/evidence/node-capacity/normal-input.json
```

この entry point は以下を fail-closed で確認します。

- `--plan` が canonical Issue #13 load plan と完全一致すること
- `--scenario` がその plan に含まれること
- raw trials が既存の型・順序・pass/fail 契約を満たすこと
- raw trials の `concurrent_sessions` が scenario の planned ladder と **完全一致**すること
- report の `scenario` provenance を既存 coverage validator と同じ `profile=<profile_label>; scenario=<scenario_id>;` contract から自動生成すること
- report 自体が既存の Node capacity report validator を通ること
- `--output` が既に存在する場合は上書きしないこと

つまり、`stop` policy で失敗境界を探索した途中 evidence や、harness failure 後に残った partial JSONL は、そのまま complete scenario report へ昇格しません。また profile/scenario の provenance は operator が別文字列として再入力せず、検証済み plan から生成するため、後段 coverage validator との binding drift を避けます。再試行では新しい raw evidence path を使い、plan 全件を採取してから report を生成します。

## 既存 assembler との関係

`assemble-node-capacity-report.py` は lower-level schema assembler として残します。手動調査や plan にまだ束縛しない intermediate evidence を扱う用途があるためです。Issue #13 の complete coverage chain へ進める report では、原則として `assemble-node-capacity-planned-report.py` を使用します。

その後の evidence chain は変更ありません。

```text
run-node-capacity-scenario.py
  -> *.trials.jsonl
  -> assemble-node-capacity-planned-report.py
  -> measured scenario report
  -> render-node-capacity-coverage-manifest.py
  -> render-node-capacity-max-sessions-proposal.py
  -> write-node-capacity-review-bundle.py
```

この変更は負荷を実行せず、provider API、課金リソース、production scheduler、credential、acceptance threshold、safety margin の決定にも触れません。
