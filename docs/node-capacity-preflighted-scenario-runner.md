# Preflight-gated Node capacity scenario runner

Issue #13 の実測 Node-capacity run を開始する前に、PR #594 で追加した read-only host preflight を必ず通したい場合は `scripts/run-node-capacity-preflighted-scenario.py` を使用します。

この wrapper は最初に `check-node-capacity-host-preflight.py` と同じ検査を実行し、Linux、logical CPU、`/proc/meminfo`、local Unix-socket Docker endpoint、Docker server、Docker Compose の prerequisites が確認できた場合だけ、既存の `run-node-capacity-scenario.py` を起動します。preflight が失敗した場合、load harness は一度も起動されません。

既存 runner の引数はそのまま渡します。たとえば complete coverage 用 run は次の形です。

```bash
python3 scripts/run-node-capacity-preflighted-scenario.py \
  --plan artifacts/node-capacity-plan.json \
  --scenario normal-input \
  --trials-jsonl artifacts/normal-input.complete.trials.jsonl \
  --run-manifest artifacts/normal-input.complete.run.json \
  --node-profile 'approved-node-shape' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  --timeout-seconds 900 \
  --failure-policy continue \
  --runner /opt/irlight/bin/node-capacity-harness \
  --json
```

## Safety boundary

この wrapper は preflight snapshot を evidence file や run manifest へ自動保存しません。また、既存 run manifest schema、raw trial schema、report schema、coverage manifest、`node_profile` の意味を変更しません。preflight の成功は「このローカル host で最低限の実行 prerequisites が確認できた」ことだけを示し、production Node class と同一であること、capacity、acceptance threshold、safety margin、media mix、production `max_sessions` を証明しません。

preflight は provider API、外部配信先、credential、課金リソース、container の start/stop、scheduler inventory を操作しません。実際の負荷を発生させるのは operator が明示した既存 scenario harness だけです。harness 自体が provider や外部サービスを利用する場合は、この wrapper の成功とは別に、その実行に必要な承認・credential・安全手順が必要です。

preflight failure は exit status `2` で fail-closed になり、wrapper は raw Docker endpoint や underlying diagnostic をそのまま出力しません。ローカル setup の詳細を確認する必要がある場合は、負荷を開始せず standalone preflight を実行してください。

```bash
python3 scripts/check-node-capacity-host-preflight.py
```

測定後の evidence chain は従来どおりです。`*.trials.jsonl` と `*.run.json` を plan-bound report に組み立て、全 scenario coverage、candidate `max_sessions` proposal、review bundle の順に検証します。詳細は [`node-capacity-scenario-runner.md`](node-capacity-scenario-runner.md) と [`node-capacity-host-preflight.md`](node-capacity-host-preflight.md) を参照してください。
