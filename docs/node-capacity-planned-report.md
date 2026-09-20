# Plan-bound Node capacity report assembly

Issue #13 の Node capacity evidence では、raw trial の schema が正しいだけでなく、承認済み canonical load plan にある負荷レベルをすべて測定したこと、その raw trial が実際にその plan/profile で runner から生成されたこと、さらに測定対象の Node profile / software revision が負荷実行時に固定されていたことを report 生成前に確認します。

従来の `assemble-node-capacity-report.py` は raw trial と report 自体の厳密な schema を検証しますが、意図的に load plan や測定 Node identity を入力 provenance として持ちません。そのため、harness が途中で停止して `[1, 2, 4]` だけが残った場合でも、その raw trial だけを単独の report として組み立てることは可能でした。また raw trial schema 自体には media profile、Node profile、software revision がないため、同じ load ladder を持つ別 profile の plan や別 Node/revision へ trial を付け替えても raw JSONL 単体からは検出できませんでした。

`run-node-capacity-scenario.py --run-manifest` は、負荷開始前に明示した Node profile / software revision と、検証済み plan、normalized raw trials の SHA-256、media profile/scenario、planned/tested levels、failure policy、completion state を exclusive sidecar に保存します。plan-bound assembler はこの sidecar まで一致した complete evidence だけを report に昇格し、Node profile / software revision を sidecar から report へ引き継ぎます。

## 推奨コマンド

まず `run-node-capacity-scenario.py --failure-policy continue --run-manifest ... --node-profile ... --software-revision ...` で planned level 全件を採取します。その後、report 生成時には Node profile / revision を再入力しません。

```bash
python3 scripts/assemble-node-capacity-planned-report.py \
  --plan artifacts/node-capacity-plan.json \
  --trials-jsonl artifacts/normal-input.complete.trials.jsonl \
  --run-manifest artifacts/normal-input.complete.run.json \
  --scenario normal-input \
  --run-id 11111111-2222-4333-8444-555555555555 \
  --safety-margin-percent 20 \
  --output docs/evidence/node-capacity/normal-input.json
```

この entry point は以下を fail-closed で確認します。

- `--plan` が canonical Issue #13 load plan と完全一致すること
- `--run-manifest` が stable regular JSON file で exact schema を満たすこと
- run manifest の plan digest / media profile / scenario が現在の canonical plan と一致すること
- run manifest の raw-trial digest / tested levels / boundary state が現在の normalized raw trials と一致すること
- run manifest に負荷実行時の有効な Node profile / lowercase 40-character software revision があること
- run manifest が `completed_plan=true` であること
- `stop` policy と measured failure boundary の組み合わせが runner の挙動として整合すること
- raw trials が既存の型・順序・pass/fail 契約を満たすこと
- raw trials の `concurrent_sessions` が scenario の planned ladder と **完全一致**すること
- report の `scenario` provenance を既存 coverage validator と同じ `profile=<profile_label>; scenario=<scenario_id>;` contract から自動生成すること
- report の Node profile / software revision を run manifest から引き継ぎ、report 生成時の再ラベルを許さないこと
- report 自体が既存の Node capacity report validator を通ること
- `--output` が既に存在する場合は上書きしないこと

つまり、`stop` policy で失敗境界を探索した途中 evidence、harness failure 後に残った manifest のない partial JSONL、別 media profile の plan への raw trial 付け替え、計測後の raw trial 改変、report 生成時の Node profile / software revision の付け替えは、そのまま complete scenario report へ昇格しません。再試行では新しい raw evidence / run-manifest path を使い、plan 全件を採取してから report を生成します。

## 既存 assembler との関係

`assemble-node-capacity-report.py` は lower-level schema assembler として残します。手動調査や plan にまだ束縛しない intermediate evidence を扱う用途があるためです。Issue #13 の complete coverage chain へ進める report では、原則として `assemble-node-capacity-planned-report.py` を使用します。

その後の evidence chain は変更ありません。

```text
run-node-capacity-scenario.py
  -> *.trials.jsonl + *.run.json
  -> assemble-node-capacity-planned-report.py
  -> measured scenario report
  -> render-node-capacity-coverage-manifest.py
  -> render-node-capacity-max-sessions-proposal.py
  -> write-node-capacity-review-bundle.py
```

この変更は operator が明示した既存 harness 以外の負荷を追加せず、provider API、課金リソース、production scheduler、credential、acceptance threshold、safety margin の決定にも触れません。
