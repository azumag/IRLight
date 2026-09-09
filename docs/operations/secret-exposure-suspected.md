# Secret 漏えい疑い runbook

Issue #11 の「secret 漏えい疑い」に対する初期 runbook です。対象は Media Node 上の stream key、内部 media credential、egress destination credential、Node token 等です。

## 原則

- secret 本文を `cat`、`docker inspect`、shell trace、ticket、chat、ログへコピーしない。
- 調査のために credentialed URL や token を再表示しない。
- この runbook の inspector はファイル本文を読まず、`lstat(2)` 相当の metadata だけを見る。
- credential rotation、Session 強制停止、Node 再構築は利用者影響を伴うため、漏えいが確認または強く疑われた時に運用判断として実施する。この inspector 自体は変更操作を行わない。

## 検知例

- secret / stream key がログ、issue、chat、monitoring payload に含まれている。
- runtime secret file が group/world readable になっている。
- runtime secret file または直上 directory が symlink に差し替わっている。
- credential の不審利用、想定外 publish、認証失敗急増がある。

## 1. ファイル metadata を確認する

Node Agent image には read-only inspector を同梱する。現在の Session で存在する secret file を明示して実行する。

```sh
python3 /opt/irlight/secret_file_inspect_cli.py \
  --path /run/irlight/media-secrets/media_input_uri \
  --path /run/irlight/media-secrets/media_publish_uri \
  --path /run/irlight/relay-secrets/media_relay_uri
```

DIRECT_PUSH で egress secret が生成されている場合は追加する。

```sh
python3 /opt/irlight/secret_file_inspect_cli.py \
  --path /run/irlight/egress-secrets/egress_url
```

exit code は `0=OK`、`2=PROBLEM`、inspector 自体の実行不能は `1`。JSON には path、file/parent mode、fixed reason code だけを出し、secret 内容や file size は出さない。

主な reason:

- `permissions_too_open`: file に group/other permission がある。
- `parent_permissions_too_open`: 直上 directory に group/other permission がある。
- `symlink` / `parent_symlink`: symlink を検出した。inspector は target を追わない。
- `not_regular_file` / `parent_not_directory`: 想定した file type ではない。
- `missing`: 指定した active secret が存在しない。
- `file_unavailable` / `parent_unavailable`: metadata を安全に取得できない。
- `file_changed` / `parent_changed`: 検査中に pathname の identity または mode/type が変化した。書込処理が落ち着いた後に再実行し、想定外の差替えなら原因を調査する。

`missing` は Session mode や lifecycle によって正常な場合もあるため、存在すべき active secret だけを `--path` へ渡す。

## 2. 影響範囲を確認する

secret 本文ではなく次を記録する。

- Session ID / Node ID
- secret 種別（ingest / internal media / egress / node access 等）
- 最初に露出を確認した時刻と媒体
- 露出先のアクセス範囲
- 不審 publish / auth failure / egress failure の有無
- inspector の fixed reason と file mode
- 直近 deploy / config change の有無

ログ調査では credentialed URL 全体ではなく既存の redacted / whitelisted field を使う。

## 3. 封じ込め

漏えいが確認または強く疑われる場合は、影響と復旧手段を確認してから次を選ぶ。

1. 露出した credential の rotation / revoke。
2. 不正利用が継続している場合のみ該当 Session / Node を隔離。
3. runtime secret directory の mode / mount / ownership 異常がある場合は新規 Session の割当を止め、Node 再構築を検討。
4. 漏えい媒体（公開ログ、issue、chat 等）のアクセス制限または削除を、監査証跡を壊さない手順で実施。

原因未確認のまま全 Node や全 Session を一括停止しない。

## 4. 復旧確認

- replacement credential で正常な ingest / egress が成立する。
- inspector が対象 active secret すべてで `OK`。
- Node heartbeat、media-stack inspector、ingest / egress inspector が独立に正常。
- 旧 credential が利用不能であることを、secret 自体をログへ出さずに確認する。

## 5. 事後作業

- 露出原因と時間帯、影響 Session を記録する。
- ログ / UI / exception / CI artifact など露出経路に再発防止テストを追加する。
- runtime secret file が想定外 mode / type になった原因を修正する。
- rotation により利用者影響が出た場合は障害記録・案内・補填判断へ接続する。
