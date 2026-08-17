# ConoHa provider spike: cleanup proof

## 目的

PR 1 (ConoHa provider spike) の完了条件を満たすため、実際の ConoHa API を使い、
以下を証明する。

- API token を取得できる
- boot volume を作成できる
- VPS を作成できる
- `irlight-*` metadata が両 resource に付く
- public IPv4 を取得できる
- server を削除できる
- volume を削除できる
- managed resource 一覧に残骸がない

この PR ではインターネットへの ingest 公開は行わない。

## 準備

ConoHa API credential はリポジトリ・ログ・Issue へ置かない。
API sub-user を作成し、必要な Compute / Volume 権限だけを付与する。

```bash
export CONOHA_IDENTITY_ENDPOINT="https://identity.tyo1.conoha.io/v2.0"
export CONOHA_COMPUTE_ENDPOINT="https://compute.tyo1.conoha.io/v2/<tenant-id>"
export CONOHA_VOLUME_ENDPOINT="https://block-storage.tyo1.conoha.io/v2/<tenant-id>"
export CONOHA_USERNAME="<api-sub-user>"
export CONOHA_PASSWORD="<api-sub-user-password>"
export CONOHA_TENANT_NAME="<tenant-name>"
# 任意
export CONOHA_IMAGE_REF="<image-id>"
export CONOHA_FLAVOR_REF="<flavor-id>"
```

## 手順

```bash
# 1. テスト用 session id を用意する
SESSION_ID="$(uuidgen)"

# 2. 作成（volume -> server の順）
python3 -m provider.admin_cli --environment dev \
  --user-id deadbeef --session-id "$SESSION_ID" \
  --size-gb 20 --delete-after-hours 6 create

# 3. 一覧で volume と server の両方を確認
python3 -m provider.admin_cli list

# 4. 削除（server -> volume の順）
python3 -m provider.admin_cli --session-id "$SESSION_ID" delete

# 5. 一覧が空であることを確認
python3 -m provider.admin_cli list
```

## 完了条件チェック

- [ ] test 用 VPS を作成できる
- [ ] server と volume を削除できる
- [ ] provider 一覧に残骸がない
- [ ] 同じ Session ID で二度 create しても resource を重複作成しない
- [ ] credential がコマンド出力・ログへ出ない

## clean room 確認

削除後に ConoHa 管理画面または API 上で、以下を確認する。

- VPS 一覧: テスト用 server が無い
- Volume 一覧: boot volume が無い
- 課金対象: server / volume が残っていない（作成直後の課金開始から削除後の課金停止までを記録）

resource ID や public IP は PR 本文へ伏せて記録する。
