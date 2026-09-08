# Production Media Node deploy preflight

Issue #11 の deploy / rollback 手順を安全に具体化するため、production Compose を**起動せずに**検査する preflight を用意する。

この文書と `scripts/validate-production-compose.py` は、実際の deploy、クラウド操作、Session 停止、volume 削除を行わない。production 変更を実行する権限や maintenance 判断を置き換えるものではない。

## 目的

`docker compose config` で実際に適用する overlay 群を解決し、Media Node の既存 security contract が設定差分で崩れていないことを deploy 前に確認する。

検査する主な条件:

- production service がローカル `build:` に戻っていない
- `:latest` image、`privileged`、host network / PID / IPC namespace を使っていない
- Continuity / Egress Gateway / Node Agent の秘密配送が `/run/...` の file reference のまま
- `node-auth` / `node-control` が `internal: true` のまま
- Session secret 用 volume が tmpfs + `mode=700` のまま
- Node Agent の Docker socket bind が read-only のまま

この検査は container image digest pinning の代替ではない。digest pinning は #12 の別残件として扱う。

## 実行例

実際の deploy と同じ順序で Compose file を渡す。

```bash
python3 scripts/validate-production-compose.py \
  -f docker-compose.node.yml \
  -f docker-compose.node.public.yml \
  -f docker-compose.node.rtmps.yml
```

RTMPS overlay を使う場合は、通常の `docker compose config` と同様に証明書・鍵の path を環境へ設定してから実行する。preflight は秘密ファイルの内容を表示しない。

成功時:

```text
OK: rendered Compose passed read-only production safety validation (... services)
```

終了コード:

- `0`: Compose の render と安全条件の検査に成功
- `1`: render はできたが安全条件に違反
- `2`: file 不在、timeout、Docker / Compose エラー、JSON 解析失敗などで検査不能

`1` と `2` はどちらも deploy を進める根拠にしない。

## Deploy 前の記録

実運用で deploy を行う場合は、変更前に少なくとも以下を記録する。

- 対象 Git commit / release
- 適用する Compose file の順序
- 現在稼働中の image reference / image ID
- `docker compose ps` 相当の service 状態
- active Session の有無と drain 方針
- rollback 先の既知正常 image reference

進行中 Session に影響する変更は、#11 の方針どおり drain または maintenance window を使う。preflight 成功だけを理由に進行中 Session を再起動しない。

## Rollback guardrails

rollback は「Compose を消して作り直す」操作ではなく、保持すべき state / secret 境界を維持したまま既知正常 image へ戻す操作として扱う。

- `docker compose down -v`、`docker volume prune`、state volume の削除を rollback 手順に含めない
- 直前に動いていた image reference / image ID を記録してから更新する
- DB / authority state の schema 互換性が不明な場合は image だけを戻さない。#90 の recovery/readiness 設計と合わせて判断する
- rollback 後は liveness と業務上の状態確認を分ける。`/healthz` が成功しただけで authority/readiness が正常と断定しない
- Secret や stream key を診断出力、Issue、PR、shell history へ転記しない

## 非目標

この preflight は以下を自動実行しない。

- container pull / start / recreate / stop
- production deploy / rollback
- cloud resource の作成・削除
- active Session の drain / stop
- TLS の外部到達性確認
- authority state の修復

runtime readiness は #90、image digest pinning / release signing は #12 で追跡する。これらが未完了でも preflight 自体は、設定退行を deploy 前に止める独立した guard として利用できる。
