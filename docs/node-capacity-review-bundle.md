# Node capacity review bundle

`max_sessions` の proposal は「どの coverage manifest から導いた値か」をレビューしやすくするための派生 JSON です。ただし proposal 内の `coverage_manifest` と proposal 自身の path は参照先を示すだけで、同じ path の bytes が後から変わっていないことまでは証明しません。

実測値を scheduler / Node inventory へ反映する判断を残すときは、proposal を canonical validator で再検証したうえで、proposal と coverage manifest の exact bytes を SHA-256 で固定した review bundle を保存できます。

## 生成

```bash
python3 scripts/render-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/max-sessions-proposal.json \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567 \
  > docs/evidence/node-capacity/review-bundle.json
```

renderer は read-only です。proposal を `validate-node-capacity-max-sessions-proposal.py` と同じ契約で再検証し、proposal とその coverage manifest を symlink-free な repository file として安定読取したうえで、次を保存します。

- proposal の repository-relative path と SHA-256
- coverage manifest の repository-relative path と SHA-256
- Node profile と software revision
- candidate `max_sessions`
- measured recommendation

proposal / coverage の bytes が検証中に変わった場合は生成しません。load test、provider API、Node 作成、scheduler / inventory 更新は行いません。

## 再検証

```bash
python3 scripts/validate-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/review-bundle.json \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

validator は bundle が参照する proposal から canonical bundle を再生成して完全一致を要求します。このため、JSON の意味が同じでも whitespace や key formatting を含め proposal / coverage manifest の bytes が変われば古い bundle は失効します。Node profile や software revision が異なる場合も fail-closed です。

## レビュー境界

review bundle は「この測定 evidence と proposal の exact bytes をこの deployment identity 向けにレビューした」という provenance を残すためのものです。署名、timestamp authority、artifact registry、production configuration の authority ではありません。

したがって次は自動化しません。

- candidate `max_sessions` を production へ反映すること
- safety margin、合否 threshold、media profile mix を決めること
- scheduler / Node inventory を変更すること
- provider resource を作成・変更すること
- evidence が実機測定から正しく生成されたこと自体を暗号学的に証明すること

実際の capacity 変更は、bundle・proposal・coverage manifest を同じレビュー可能な revision に保存し、既存の deployment / change-review 手順で別途承認してください。
