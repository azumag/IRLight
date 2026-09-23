# Preflight-gated Node capacity scenario runner

Issue #13 の実測 Node-capacity run を開始する前に、PR #594 で追加した read-only host preflight を必ず通したい場合は `scripts/run-node-capacity-preflighted-scenario.py` を使用します。

この wrapper は最初に `check-node-capacity-host-preflight.py` と同じ検査を実行し、Linux、logical CPU、`/proc/meminfo`、local Unix-socket Docker endpoint、Docker server、Docker Compose の prerequisites が確認できた場合だけ、既存の `run-node-capacity-scenario.py` を起動します。preflight が失敗した場合、load harness は一度も起動されません。

既存 runner の引数はそのまま渡します。必要なら wrapper 専用の `--preflight-json PATH` を追加すると、検証済み host snapshot を load harness 起動前に exclusive/atomic な JSON evidence として保存します。この引数だけは既存 runner へ渡されません。

たとえば complete coverage 用 run は次の形です。

```bash
python3 scripts/run-node-capacity-preflighted-scenario.py \
  --preflight-json artifacts/normal-input.complete.host-preflight.json \
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

`--preflight-json` の出力先は既存ファイルを上書きしません。親 directory が存在しない、既存 path がある、snapshot の publish に失敗した、といった場合は load harness を起動せず exit status `2` で停止します。evidence file は mode `0600` で publish されます。

保存される schema-v1 snapshot は hostname、Docker endpoint、環境変数、credential、provider 情報を含みません。現在の項目は platform system / machine / kernel、logical CPU、total memory、Docker server version、Docker Compose version です。wrapper はこの schema を load 開始前に深く検証し、未知の top-level / nested field、非 Linux platform、空・control-character を含む識別値、bool/文字列/0以下の resource count を拒否します。これにより collector の将来の変更や破損が、意図せず追加 host metadata を evidence に混入させたり、型の壊れた snapshot を「preflight 済み」として残したりしません。schema を拡張する場合は明示的な version/validator 更新が必要です。

保存後・レビュー時には、保存された bytes 自体も standalone validator で再確認できます。

```bash
python3 scripts/validate-node-capacity-host-preflight.py \
  artifacts/normal-input.complete.host-preflight.json
```

この validator は load を実行せず、symlink、oversize、duplicate JSON key、NaN/Infinity、未知 field、壊れた scalar を fail-closed で拒否します。これにより「実行時には正しかった in-memory snapshot」と「後でレビューする persisted evidence」を区別して検証できます。

## Safety boundary

この wrapper は `--preflight-json` を指定した場合だけ preflight snapshot を保存します。既存 run manifest schema、raw trial schema、report schema、coverage manifest、`node_profile` の意味は変更しません。また、この snapshot は run manifest へ暗号学的に bind されません。同じ run の証跡として扱う場合は、衝突しない共通 run 名で `*.host-preflight.json`、`*.trials.jsonl`、`*.run.json` を保管してください。

preflight の成功は「このローカル host で最低限の実行 prerequisites が確認できた」ことだけを示し、production Node class と同一であること、capacity、acceptance threshold、safety margin、media mix、production `max_sessions` を証明しません。

preflight は provider API、外部配信先、credential、課金リソース、container の start/stop、scheduler inventory を操作しません。実際の負荷を発生させるのは operator が明示した既存 scenario harness だけです。harness 自体が provider や外部サービスを利用する場合は、この wrapper の成功とは別に、その実行に必要な承認・credential・安全手順が必要です。

preflight failure は exit status `2` で fail-closed になり、wrapper は raw Docker endpoint や underlying diagnostic をそのまま出力しません。ローカル setup の詳細を確認する必要がある場合は、負荷を開始せず standalone preflight を実行してください。

```bash
python3 scripts/check-node-capacity-host-preflight.py
```

測定後の evidence chain は従来どおりです。`*.trials.jsonl` と `*.run.json` を plan-bound report に組み立て、全 scenario coverage、candidate `max_sessions` proposal、review bundle の順に検証します。詳細は [`node-capacity-scenario-runner.md`](node-capacity-scenario-runner.md) と [`node-capacity-host-preflight.md`](node-capacity-host-preflight.md) を参照してください。
