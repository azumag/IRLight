# Control Plane state restore drill

Issue #90 の復旧作業を、live authority を上書きせず検証するための最小手順を定義する。この手順は **restore の実行コマンドではない**。バックアップ取得方式、世代 fencing、provider reconciliation、credential 再発行は deployment / security semantics を伴うため、対象環境の運用設計と承認を別途必要とする。

## 安全境界

- live の `STATE_DIR` / `NODE_STATE_DIR` へ restore を直接試さない。
- Control Plane、外部 reaper、その他 state writer を quiesce した整合点からバックアップを取得する。
- restore 先は sandbox、detached volume、または read-only に再マウントできる隔離ディレクトリとする。
- `docker compose down -v`、volume prune、marker 削除、空 JSON 作成を復旧策として使わない。
- provider resource を「state に見えない」という理由だけで削除しない。
- raw state、token、Destination secret、provider identity を Issue / CI log へ貼らない。

## 1. protected reference を固定する

バックアップから検証用の **protected reference copy** を用意する。以後の比較コマンドは reference も candidate も read-only で開くが、OS / volume 側でも誤操作を防げるよう書込禁止にすることを推奨する。

`STATE_DIR` と `NODE_STATE_DIR` が別 volume の場合は、同じ整合点から取得した組を使用する。片方だけ別時刻の snapshot と混ぜない。

## 2. candidate を隔離領域へ restore する

本番切替前に、同じ backup を sandbox / detached volume へ restore する。ここでは provider API、Node 作成・削除、credential rotation を実行しない。

restore ツール自体のログにも secret を出さない。ファイル owner / mode / mount generation は deployment 固有のチェックリストで確認する。

## 3. startup authority を read-only 比較する

Control Plane image には次の診断 CLI が含まれる。

```bash
python /app/state_restore_compare_cli.py \
  --source-state-dir /mnt/reference/control \
  --candidate-state-dir /mnt/restored/control \
  --source-node-state-dir /mnt/reference/node \
  --candidate-node-state-dir /mnt/restored/node
```

Node authority が各 `STATE_DIR` と同じ場所なら `--*-node-state-dir` は省略できる。source / candidate の snapshot root は実ディレクトリを直接指定し、symlink を比較元・restore 先の代用にしない。CLI は root 自体に加えて、その直上の parent directory も `O_NOFOLLOW` 付きで検査する。root は検証済み parent directory fd から相対 open し、その **検証済み root directory fd を比較終了まで保持**する。authority と initialization marker も保持した root fd から相対 open し、読み取った authority と marker の fd を validator 完了まで pin する。validation 中に通常 writer や誤操作が authority / marker を atomic replace した場合は、同一内容への置換であっても stale inode を比較結果として採用せず `UNAVAILABLE` に fail-closed する。legacy bootstrap-token ledger についても、marker が存在する場合は同じ identity 保護を行い、検査開始時に marker が存在しなかった場合でも検査中の出現を見逃さない。final symlink、直上 parent の symlink、root を安全に open するまでの root / parent identity race は fail-closed にし、root を open 済みの後に pathname が別 directory へ差し替えられても比較対象を新しい path へ付け替えない。より上位の deployment 固有 mount / path identity は下記の運用記録でも確認する。source / candidate のいずれかの root が同じ directory identity を指す場合は `SOURCE_CANDIDATE_NOT_DISTINCT`、個別 authority が hard link 等で同じ inode を共有する場合は `SOURCE_CANDIDATE_AUTHORITY_NOT_DISTINCT` として拒否する。bind mount 等の deployment 固有 identity もあるため、運用記録でも mount identity を確認する。

CLI は `/readyz` と同じ startup authority (`control`, `catalog`, `users`, `auth_sessions`, `nodes`) を検証し、その **検証済み byte snapshot** が一致するかだけを比較する。legacy bootstrap-token ledger は file / initialization marker の存在と内容を別途比較する。digest は内部比較にだけ使い、出力しない。

終了コードは `0=MATCH`, `2=MISMATCH`, `3=UNAVAILABLE`。JSON には overall status / reason code と authority label、`MATCH / MISMATCH / UNAVAILABLE`、固定 reason code だけを出し、path、raw JSON、hash、credential、parser detail は出さない。

比較中に writer と競合して `UNAVAILABLE` になった場合、CLI は retry のために marker や authority を作成・修復しない。reference / candidate を保全し、writer を quiesce した整合点を取り直してから再検証する。

## 4. candidate 単体の readiness を確認する

比較が `MATCH` でも、隔離した candidate に対して `state_inspect_cli.py` を実行し、startup authority が現在の validator で `OK` になることを確認する。

```bash
STATE_DIR=/mnt/restored/control \
NODE_STATE_DIR=/mnt/restored/node \
python /app/state_inspect_cli.py
```

`STATE_DIR` / `NODE_STATE_DIR` には restore された実ディレクトリを直接指定し、symlink を代用にしない。`state_inspect_cli.py` と `/readyz` は各 state root を `lstat` と `O_NOFOLLOW|O_DIRECTORY` 付き open + `fstat` で固定し、その root fd から authority / marker を相対 open する。root 自体が symlink の場合、または検査中に configured pathname が別 directory へ差し替わった場合は `UNAVAILABLE` として fail-closed にする。診断のために root、marker、lock、JSON を作成・修復しない。

`UNAVAILABLE` を marker 作成や空 state 生成で回避しない。reference を保全したまま restore 方法・対象世代を調べ直す。

## 5. provider ownership を read-only 照合する

startup authority / readiness の検証後、`docs/operations/state-provider-reconciliation.md` の手順で、復元した Session authority が参照する provider volume/server と provider 側の Session/user metadata を read-only 照合する。これは自動 cleanup の許可判定ではなく、snapshot 間の所有関係の不一致を `REVIEW_REQUIRED` として列挙する診断である。

provider inventory の取得は point-in-time transaction ではないため、Control Plane writer / provisioner / reaper を fencing した状態で実施する。不一致を理由にこの診断 CLI から resource を削除・修復する機能は提供しない。

## 6. この drill で証明しないもの

この比較は PostgreSQL 等への将来移行、lazy な Session / entitlement / Destination-secret / ingest authority、object storage、provider 側 resource の現在の lifecycle が安全に cleanup 可能であることを証明しない。また古い snapshot の token / credential が安全に再利用可能であることも意味しない。

本番復旧前には、少なくとも次を別の承認済み手順で決める必要がある。

- backup generation / restore epoch / fencing と writer 再開順序
- provider ownership 診断で `REVIEW_REQUIRED` になった resource の lifecycle / cleanup 判断
- 古い auth / bootstrap / ingest credential の失効・再発行方針
- lazy authority と object storage の整合点
- rollback 後に新しい writer が古い state を再導入しない保証

これらが未決のままなら、比較が `MATCH` でも本番 restore / provider 操作へ進めない。

## 7. 記録する証跡

秘密を含まない範囲で、backup generation ID、reference/candidate の mount identity、比較 CLI の overall status と reason code、`state_inspect_cli.py` の status、provider ownership 診断の overall status / reason code、実施者、対象 version、未解決の reconciliation 項目を記録する。ファイル内容や digest を証跡として公開しない。
