# Persisted Node capacity report provenance validation

PR #472 で、Node capacity の raw trials は canonical load plan と run manifest に束縛して report 化できるようになりました。ただし report JSON 単体は、その schema が正しくても、どの raw trials / run manifest から生成されたかを downstream で証明できません。

`scripts/validate-node-capacity-planned-report.py` は、persisted report を canonical plan・raw trial JSONL・run manifest から再構成し、JSON value が完全一致する場合だけ受理する read-only validator です。

```bash
python3 scripts/validate-node-capacity-planned-report.py \
  artifacts/normal-input.report.json \
  --plan artifacts/node-capacity-plan.json \
  --trials-jsonl artifacts/normal-input.trials.jsonl \
  --run-manifest artifacts/normal-input.run.json \
  --scenario normal-input \
  --json
```

検証では最初に report 自体を canonical schema-v1 validator に通し、その後 `assemble-node-capacity-planned-report.py` を再利用して同じ report を再構成します。run manifest の plan/trials digest、media profile/scenario、Node profile、software revision、planned/tested levels、completion state、failure policy の検証もそのまま再実行されます。

report の `run_id`、`safety_margin_percent`、`notes` は report assembly 時に明示する metadata / policy input なので、persisted report から再構成器へ渡します。一方、測定 trial、media profile/scenario、Node profile、software revision は run provenance 側から導出されます。したがって report 内の trial metric や measured identity だけを書き換えても、raw trials / run manifest と一致しなければ fail-closed になります。

persisted report 自体は bounded stable regular-file read で読み、final symlink / special file、oversize、read 中の pathname replacement や same-inode mutation を拒否します。この validator は「その report 内容が提示された canonical run provenance と整合する」ことを検証するもので、外部からの暗号学的な真正性を付与するものではありません。

## Safety boundary

validator は provider API、network、subprocess、credential、課金リソース、production scheduler / `max_sessions` に触れず、負荷試験も実行しません。CPU / memory / network / media quality の合否閾値や safety margin も決定しません。

## 次段

現在の durable coverage manifest は load plan と report path を束縛しますが、raw trials / run manifest path はまだ schema に含みません。また現行の digest-pinned review bundle が固定する evidence closure も coverage manifest・load plan・reports までで、raw trials / run manifest はまだ含みません。この validator を primitive として、次段では coverage / release acceptance / review bundle の永続 evidence を run provenance まで閉じる必要があります。その schema migration は既存 schema-v1 との互換性を明示して別変更として扱います。
