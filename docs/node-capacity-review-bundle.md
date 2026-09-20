# Node capacity review bundle

`max_sessions` の proposal は「どの coverage manifest から導いた値か」をレビューしやすくするための派生 JSON です。ただし proposal 内の path、coverage manifest 内の evidence path は参照先を示すだけで、同じ path の bytes が後から変わっていないことまでは証明しません。

実測値を scheduler / Node inventory へ反映する判断を残すときは、proposal を canonical validator で再検証したうえで evidence closure 全体を SHA-256 で固定した review bundle を保存できます。schema-v1 coverage の場合は従来どおり **proposal → coverage manifest → load plan + measured reports** を固定します。schema-v2 の provenance-bound coverage の場合は review bundle も schema v2 となり、さらに **各 scenario の raw trials + run manifest** まで exact bytes を固定します。

## 永続化する review bundle の生成

既存の review bundle を更新する場合は `write-node-capacity-review-bundle.py` を使います。proposal と evidence closure の検証・digest 収集がすべて成功してから、一時ファイルを同じディレクトリへ書き、最後に atomic rename で置き換えます。検証失敗や書き込み失敗で既存の known-good bundle を先に truncate しません。

```bash
python3 scripts/write-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/max-sessions-proposal.json \
  --output docs/evidence/node-capacity/review-bundle.json \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

`--output` は repository-relative path に限定され、親ディレクトリは事前に存在している必要があります。symlink の出力先・親ディレクトリや、bundle が固定対象にしている proposal / coverage manifest / load plan / measured report を出力先に指定した場合は fail-closed です。schema-v2 bundle では raw trials / run manifest も固定対象なので、それらを `--output` に指定することも拒否します。

stdout へ一時的に出力して別処理へ渡したい場合は、従来どおり renderer を直接利用できます。

```bash
python3 scripts/render-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/max-sessions-proposal.json \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

永続 artifact の更新では `renderer > review-bundle.json` を使わないでください。shell redirection は renderer が validation を始める前に既存ファイルを truncate するため、validation が失敗しただけでも直前の known-good artifact を失う可能性があります。

renderer は stdout 以外を変更せず、writer が変更するのも明示した review bundle artifact だけです。どちらも proposal を `validate-node-capacity-max-sessions-proposal.py` と同じ契約で再検証し、coverage manifest の canonical validator も通したうえで、次を保存します。

- proposal の repository-relative path と SHA-256
- coverage manifest の repository-relative path と SHA-256
- load plan の repository-relative path と SHA-256
- 各 scenario report の scenario ID、repository-relative path、SHA-256
- schema-v2 coverage の場合、各 scenario の raw trials path / SHA-256 と run manifest path / SHA-256
- Node profile と software revision
- candidate `max_sessions`
- measured recommendation

schema-v1 coverage から生成した既存形式は review bundle schema v1 のまま維持します。schema-v2 coverage から生成した bundle は schema v2 になり、provenance closure を省略した schema marker の downgrade では validator の canonical再生成と一致しないため fail-closed です。

proposal / manifest / plan / report / raw provenance は symlink-free な repository file として扱い、canonical validation と digest collection を2回行って evidence closure が途中で差し替わっていないことも確認します。load test、provider API、Node 作成、scheduler / inventory 更新は行いません。

## 再検証

```bash
python3 scripts/validate-node-capacity-review-bundle.py \
  docs/evidence/node-capacity/review-bundle.json \
  --node-profile 'linux-x86_64 4 vCPU 8 GiB' \
  --software-revision 0123456789abcdef0123456789abcdef01234567
```

validator は bundle が参照する proposal から canonical bundle を再生成して完全一致を要求します。このため、JSON の意味が同じでも whitespace や key formatting を含め、固定対象のいずれか1 byte でも変われば古い bundle は失効します。schema-v2 では raw trials / run manifest の byte-only change も失効対象です。Node profile や software revision が異なる場合も fail-closed です。

## レビュー境界

review bundle は「この測定 evidence closure の exact bytes と proposal を、この deployment identity 向けにレビューした」という provenance を残すためのものです。署名、timestamp authority、artifact registry、production configuration の authority ではありません。

したがって次は自動化しません。

- candidate `max_sessions` を production へ反映すること
- safety margin、合否 threshold、media profile mix を決めること
- scheduler / Node inventory を変更すること
- provider resource を作成・変更すること
- evidence が実機測定から正しく生成されたこと自体を暗号学的に証明すること

実際の capacity 変更は、bundle とその参照する proposal / coverage manifest / load plan / reports、schema-v2 では raw trials / run manifests を同じレビュー可能な revision に保存し、既存の deployment / change-review 手順で別途承認してください。
