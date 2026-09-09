# State / provider ownership reconciliation

Issue #90 の復旧手順で、復元した Session authority と provider 側に残る IRLight-managed resource の所有関係を照合するための read-only 手順。

この手順は **削除・修復・reconcile を自動実行しない**。結果が `REVIEW_REQUIRED` でも、resource を消す根拠として単独では使わない。Session lifecycle、cleanup lease、reaper の進行状況を確認してから人間が判断する。

## 事前条件

1. Control Plane の Session writer、provisioner、reaper を停止または fencing し、検査中に `sessions.json` や provider resource が変化しない状態にする。
2. 復元候補を本番 writer が使う state directory に直接上書きしない。保護した restore directory を指定する。
3. `sessions.json` と `.sessions.json.initialized` の両方を用意する。CLI は marker や lock file を作らず、欠落・symlink・不正 JSON・不正 Session record を `SESSION_AUTHORITY_UNAVAILABLE` として fail-closed に扱う。
4. ConoHa を読む場合は、既存の `CONOHA_*` credential を shell history やログへ出さない。CLI は managed-resource の一覧取得だけを行い、create/delete API は呼ばない。

## 実行

Control Plane image には `provider_state_reconcile_cli.py` が含まれる。復元した state directory を read-only bind mount するなど、live authority と分離した状態で実行する。

実 provider を照合する場合:

```bash
python /app/provider_state_reconcile_cli.py \
  --state-dir /path/to/protected-restored-state \
  --provider-mode conoha
```

file-backed fake provider を照合する場合:

```bash
python /app/provider_state_reconcile_cli.py \
  --state-dir /path/to/protected-restored-state \
  --provider-mode fake \
  --fake-state-file /path/to/fake-provider.json
```

provider mode は必須指定で、暗黙に実 provider へ接続しない。終了コードは `0=MATCH`、`2=authority/provider inventory を安全に取得できない`、`3=REVIEW_REQUIRED`。

## 判定内容

CLI は lifecycle state から「resource が存在すべきか」を推測しない。`PROVISIONING` で volume だけがある、terminal Session が cleanup 中で resource をまだ持つ、といった状態はこのツールだけでは異常扱いしない。

照合するのは次の客観的な対応関係だけである。

- Session の `provider_volume_id` / `provider_server_id` が provider inventory に存在するか。
- 同じ provider resource の `irlight-session-id` が Session ID と一致するか。
- provider resource の `irlight-user-id` が Session の `user_id` と一致するか。
- provider 側の managed resource が、存在する Session から同じ kind / provider ID で参照されているか。
- provider ID の重複、未知 resource kind、所有 metadata の欠落がないか。

出力は provider の `details`、public IPv4、credential、request endpoint、内部 exception を含めず、固定 `reason_code` と照合に必要な Session/resource ID だけを出す。

主な reason code:

- `STATE_PROVIDER_RESOURCE_MISSING`: state が参照する provider resource が inventory にない。
- `PROVIDER_RESOURCE_SESSION_MISSING`: provider resource の Session ID が state に存在しない。
- `PROVIDER_RESOURCE_NOT_REFERENCED_BY_SESSION`: provider resource は Session を名乗るが、その Session が同じ kind の resource ID を参照していない。
- `PROVIDER_SESSION_OWNERSHIP_MISMATCH`: state が参照する resource の provider-side Session metadata が一致しない。
- `PROVIDER_USER_OWNERSHIP_MISMATCH`: provider-side user metadata が Session authority と一致しない。
- `PROVIDER_SESSION_METADATA_MISSING` / `PROVIDER_USER_METADATA_MISSING`: managed resource の所有 metadata が不足している。
- `PROVIDER_RESOURCE_DUPLICATE`: 同じ kind/provider ID が inventory に重複している。

## 復旧判断

`MATCH` は「取得した2つの snapshot の参照関係が一致した」という意味に限定する。バックアップ世代、credential fencing、Node の実稼働状態、provider API の完全な point-in-time consistency まで保証しない。

`REVIEW_REQUIRED` の場合は resource を即時削除せず、Session event、cleanup lease、Node heartbeat、provider console/API の現在状態を照合する。特に `PROVIDER_RESOURCE_SESSION_MISSING` は、古い state を restore した結果なのか、正常な orphan cleanup の途中なのかを区別してから cleanup を再開する。

検査後に writer/reaper を再開する前には、`/readyz`、state restore comparison、credential fencing 方針を含む `docs/operations/state-restore-drill.md` の手順も完了させる。
