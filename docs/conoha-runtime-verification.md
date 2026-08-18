# ConoHa runtime wiring and Issue #25 verification

## 1. Control Plane を実 ConoHa provider に切り替える

既定は従来どおり fake provider です。実機確認時だけ `control-ui` コンテナに
以下を設定します。

```text
IRLIGHT_PROVIDER=conoha
CONOHA_IDENTITY_ENDPOINT=...
CONOHA_COMPUTE_ENDPOINT=...
CONOHA_VOLUME_ENDPOINT=...
CONOHA_USERNAME=...
CONOHA_PASSWORD=...
CONOHA_TENANT_NAME=...
CONOHA_REGION=tyo1
CONOHA_IMAGE_REF=...
CONOHA_FLAVOR_REF=...
```

`IRLIGHT_PROVIDER=conoha` の場合、Session prepare / stop と reaper は同じ
`ConohaClient` 実装を使います。認証情報は URL やログへ埋め込まず、環境変数
または運用側の secret injection で渡します。

ConoHa の endpoint / image / flavor は対象アカウントで実際に利用できる値を
指定してください。実機検証では VPS と volume の作成・削除が発生し、料金が
発生する可能性があります。

## 2. provider 単体の疎通確認

Control Plane を切り替える前に、同じ `CONOHA_*` を設定したシェルで managed
resource 一覧を確認します。

```bash
cd /opt/irlight
python3 -m provider.admin_cli list
```

ここで認証・endpoint が正しく、既存 IRLight resource の一覧取得ができることを
確認します。

## 3. reaper を 5 分周期で実行する

`deploy/systemd/irlight-reaper.service` は稼働中の `control-ui` コンテナ内で
`/app/reaper_cli.py` を実行します。そのため Control Plane と同じ `STATE_DIR`
volume、および同じ `IRLIGHT_PROVIDER` / `CONOHA_*` 環境をそのまま共有できます。

unit 内の `/opt/irlight` と compose file は実機の配置に合わせて変更してから
インストールします。

```bash
sudo cp deploy/systemd/irlight-reaper.service /etc/systemd/system/
sudo cp deploy/systemd/irlight-reaper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now irlight-reaper.timer
sudo systemctl list-timers irlight-reaper.timer
```

手動実行とログ確認:

```bash
sudo systemctl start irlight-reaper.service
sudo journalctl -u irlight-reaper.service -n 100 --no-pager
```

タイマーは boot 後 2 分で初回実行し、その後 5 分ごとに実行します。

## 4. Issue #25 実機確認チェックリスト

### A. double prepare

1. 新しい Session ID で prepare する。
2. 同じ Session ID / Idempotency-Key でもう一度 prepare する。
3. `python3 -m provider.admin_cli list` で対象 session の server / volume が各1個だけであることを確認する。

### B. provisioning 中 stop

1. 新しい Session を prepare する。
2. provider resource の作成途中で stop を発行する。
3. stop または次回 reaper 後に対象 session の resource が残っていないことを確認する。

### C. server 手動削除後の orphan volume

1. prepare 済み Session の server ID / volume ID を記録する。
2. ConoHa 側で server だけを削除し、volume を残す。
3. `sudo systemctl start irlight-reaper.service` で reaper を即時実行する。
4. `provider.admin_cli list` で orphan volume が削除されたことを確認する。

### D. 最終残骸確認

```bash
python3 -m provider.admin_cli list
```

検証に使った Session ID の resource が0件であることを確認します。Issue #25 には
Session ID、実行時刻、各ステップ前後の managed-resource 一覧、reaper の journal
を貼り、秘密値は必ず伏せます。
