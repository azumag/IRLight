# Deploy / rollback runbook

Issue #11 の deploy / rollback 要件を、進行中 Session と authority を壊さずに判断・実施するための運用手順です。この文書は本番変更を自動実行するものではなく、変更を行う担当者が事前条件・影響範囲・rollback 条件を確認するためのチェックリストです。

## 原則

- deploy 前に `docs/production-deploy-preflight.md` の read-only 検査を実行し、失敗または検査不能なら進めない。
- active Session がある場合は、drain / maintenance window / 変更延期のいずれかを明示的に選ぶ。preflight 成功だけを理由に Session を再起動しない。
- rollback 先は「直前に動いていた既知正常な immutable image reference」とし、`:latest` や未記録の local build を使わない。
- DB / authority schema の互換性が不明なら image だけを戻さない。`state-readiness.md` と `state-restore-drill.md` の境界を優先する。
- stream key、SRT passphrase、Authorization header、token、password、provider credential を shell history、Issue、PR、通常ログへ残さない。
- `docker compose down -v`、volume prune、state volume 削除、marker 削除、空 state 作成を rollback 手段にしない。

## Deploy 前確認

変更前に、少なくとも次を記録します。secret の値そのものは記録しません。

1. 対象 Git commit / release と変更理由
2. 適用する Compose file と順序
3. 現在稼働中の image reference / image ID
4. rollback 先の既知正常 image reference / image ID
5. `docker compose ps` 相当の service 状態
6. active Session の有無、対象 Session、drain 方針
7. authority readiness と Node heartbeat の状態
8. 変更後に確認する ingest / egress / heartbeat / readiness の成功条件

production Compose は実際の overlay 順序で read-only 検査します。

```bash
python3 scripts/validate-production-compose.py \
  -f docker-compose.node.yml \
  -f docker-compose.node.public.yml \
  -f docker-compose.node.rtmps.yml
```

exit `0` 以外は deploy を進める根拠にしません。RTMPS 用の証明書・鍵 path が必要な場合も、ファイル内容を出力しません。

## Deploy 実施条件

以下をすべて満たした場合だけ、実際の deploy 操作へ進みます。

- preflight が成功している
- rollback 先が記録されている
- active Session への扱いが決まっている
- schema / authority 互換性に未解決の疑義がない
- 変更対象外 service / volume を触らない実行計画になっている
- 追加課金、provider resource 作成、secret rotation が必要なら別途明示判断済みである

この runbook は本番の `pull` / `up` / `restart` を自動化しません。環境ごとの service manager / Compose 運用に従い、対象 service を必要最小限に限定します。

## Deploy 後確認

変更直後の単発成功だけで完了にしません。最低限、次を確認します。

1. 対象 service が期待した image reference / image ID で起動している
2. `/healthz` 相当の liveness が正常
3. authority を使う構成では readiness が正常で、欠損・破損・誤 mount がない
4. Node heartbeat が複数周期にわたり更新される
5. 対象 Session の ingest / egress が期待状態へ収束する
6. restart loop、OOM、disk pressure、接続拒否、backlog 増加が発生していない
7. 変更対象外 Session / service に退行がない

`/healthz` の成功だけで「配信可能」「authority 正常」と断定しません。

## Rollback 判定

次のいずれかが変更直後から再現し、設定修正より既知正常版へ戻す方が安全と判断できる場合を rollback 候補とします。

- readiness が継続して非 ready
- Node heartbeat 停止または session process crash loop
- ingest / egress の広範な失敗
- rollback で解消可能と判断できる設定・image regression
- security boundary の退行が検出された

一方、authority corruption、DB migration 非互換、provider 障害、secret 漏えい、容量不足を image rollback だけで解決しようとしません。それぞれ専用 runbook / Issue の判断へ切り替えます。

## Rollback 手順

1. 新規変更を止め、影響 Session と現在の実状態を read-only に記録する。
2. rollback 先の image reference / image ID が deploy 前記録と一致することを確認する。
3. schema / authority が rollback 先と互換であることを確認する。不明なら停止して `state-readiness.md` / `state-restore-drill.md` へ進む。
4. active Session がある場合は drain / maintenance 判断を再確認する。
5. 対象 service だけを既知正常 image へ戻す。volume / authority / secret を削除しない。
6. Deploy 後確認と同じ liveness / readiness / heartbeat / ingest / egress 確認を行う。
7. regression が消えず原因が不明なら再 deploy を繰り返さず、障害 runbook へ切り替える。

## 復旧確認

rollback または修正版 deploy 後、次を満たしてから復旧完了とします。

- readiness と heartbeat が継続的に正常
- 対象 Session が期待状態へ収束し、新たな FAILED / cleanup backlog が増えていない
- ingest / egress の失敗率が変更前の基準へ戻っている
- authority / volume / secret の境界を rollback 操作で変更していない
- 変更対象外 service と Session に追加影響がない

## 事後作業

インシデントまたは変更記録には、secret を含めず次を残します。

- deploy / rollback の開始・終了時刻
- 対象 Git commit と image reference
- 変更前後の service / readiness / heartbeat 状態
- 影響 Session と利用者影響
- rollback を判断した固定 reason / 観測事実
- 実行した検証と結果
- 原因、恒久修正、追加すべき回帰テスト / alert / runbook 改善

同じ種類の regression が再発可能なら、手順書だけで閉じず自動テストまたは static preflight の follow-up issue に分離します。
