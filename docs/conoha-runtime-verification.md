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

Control Plane を切り替える前に、同じ `CONOHA_*` と Control Plane と同じ
`STATE_DIR` を設定したシェルで managed resource 一覧を確認します。

```bash
cd /opt/irlight
STATE_DIR=/opt/irlight/state \
python3 -m provider.admin_cli list
```

ここで認証・endpoint が正しく、既存 IRLight resource の一覧取得ができることを
確認します。`provider.admin_cli` は Control Plane HTTP API とは別経路で
`ConohaClient` を直接叩くため、`/v1` のセッション認証（後述）を必要としません。

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

### 3.1 `STATE_DIR` 共有の確認（「初期化済み state」まで確認する）

reaper の orphan sweep は、共有した `STATE_DIR` に **初期化済みの session
state** が無いと何もしません。`reaper._cleanup_orphans` は
`store.authoritative_snapshot()` を呼び、`sessions.json` がまだ一度も書かれて
いない場合は `SessionStateError`（"missing or uninitialized"）となり、
`skipping orphan cleanup` をログして `orphan_cleanup=0` で抜けます。

したがって「同じディレクトリを指している」だけでは不十分で、少なくとも次を
確認します。host の Python 3 と、journal を読める sudo 権限を使います。
このブロックは reaper を実行するため、既存の resource cleanup が発生します。
実 ConoHa の実行許可を得た検証時だけ実施してください。

```bash
(
set -eu
# 同じ STATE_DIR を Control Plane と reaper が実際に読む
sudo docker compose -f /opt/irlight/docker-compose.poc.yml exec -T control-ui \
     sh -lc 'echo "$STATE_DIR"; ls -la "$STATE_DIR"'

# 存在確認だけでは破損を検出できない。失敗したらここで停止する。
sudo docker compose -f /opt/irlight/docker-compose.poc.yml exec -T control-ui \
     sh -lc 'ls -la "$STATE_DIR/sessions.json" "$STATE_DIR/.sessions.json.initialized"'

python3 - <<'PY'
import ast
import json
import subprocess

UNIT = "irlight-reaper.service"
START = "ExecMainStartTimestampMonotonic"
PROPERTIES = ("Result", "ExecMainCode", "ExecMainStatus", START,
              "ExecMainExitTimestampMonotonic", "ActiveState")


def fail(reason):
    raise SystemExit("STATE_DIR は未確認です: " + reason)


def run(*args):
    try:
        result = subprocess.run(["sudo", *args], capture_output=True,
                                text=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired):
        fail(args[0] + " を実行できません")
    if result.returncode != 0 or (args[0] == "journalctl" and result.stderr.strip()):
        fail(args[0] + " が失敗、または journal の完全性を確認できません")
    return result.stdout


def properties():
    output = run("systemctl", "show", UNIT, "--property=" + ",".join(PROPERTIES))
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if set(values) != set(PROPERTIES):
        fail("service の実行結果が不足しています")
    return values


before = run("systemctl", "show", UNIT, "--property=" + START, "--value").strip()
if not before.isdecimal():
    fail("前回の実行時刻を取得できません")
# 実行前の cursor を固定し、古い警告や成功ログを判定に使わない。
try:
    cursor = json.loads(run("journalctl", "-n", "1", "-o", "json", "--no-pager"))["__CURSOR"]
    if not isinstance(cursor, str) or not cursor.strip():
        fail("journal cursor が空です")
except (ValueError, KeyError, TypeError):
    fail("journal cursor を取得できません")

run("systemctl", "start", UNIT)  # Type=oneshot の完了まで待つ。--no-block は使わない。
state = properties()
if (state["Result"] != "success" or state["ExecMainCode"] != "1"
        or state["ExecMainStatus"] != "0" or state["ActiveState"] != "inactive"):
    fail("今回の service が正常終了していません")
if not all(state[key].isdecimal() for key in (START, "ExecMainExitTimestampMonotonic")):
    fail("今回の実行時刻が不足しています")
started = int(state[START])
if started <= int(before) or int(state["ExecMainExitTimestampMonotonic"]) < started:
    fail("新しい service 実行の完了を証明できません")

journal = run("journalctl", "-b", "-u", UNIT, "--after-cursor=" + cursor,
              "-o", "json", "--no-pager")
# timer 等による次の実行が始まった場合も、混在した証拠で成功扱いしない。
if properties() != state:
    fail("ログ取得中に service の実行状態が変わりました")
messages = []
try:
    for line in journal.splitlines():
        entry = json.loads(line)
        if int(entry["__MONOTONIC_TIMESTAMP"]) >= started:
            message = entry["MESSAGE"]
            if not isinstance(message, str):
                fail("journal の本文が文字列ではありません")
            messages.append(message)
except (ValueError, KeyError, TypeError):
    fail("今回の journal を解釈できません")
if not messages:
    fail("今回の実行ログが空です")
if any("skipping orphan cleanup" in message.lower() for message in messages):
    fail("orphan sweep が省略されました。STATE_DIR の初期化・破損を確認してください")

# reaper_cli.py の既存 flat-dict 出力を安全に読み、正の完了証拠を要求する。
# orphan_cleanup=0 は正常（削除対象が無い場合もある）。eval は使わない。
summaries = []
for message in messages:
    for line in message.splitlines():
        if line.startswith("{"):
            try:
                summaries.append(ast.literal_eval(line))
            except (ValueError, SyntaxError):
                fail("reaper の集計を解釈できません")
if len(summaries) != 1 or not isinstance(summaries[0], dict):
    fail("今回の reaper 完了集計を一意に確認できません")
summary = summaries[0]
count = summary.get("orphan_cleanup")
if (type(count) is not int or count < 0
        or summary.get("auth_session_gc_status") != "ok"):
    fail("reaper 完了集計に成功の証拠がありません")
print("STATE_DIR は初期化済み（今回の service 正常終了と orphan sweep 集計を確認）")
PY
)
```

この確認は、service / journal 読取失敗、空ログ、成功集計の欠落、実行世代の
変化をすべて「未確認」として停止します。全履歴の警告の有無だけで初期化を
判定しません。journal が欠落・遅延していても、初期化成功とは扱いません。

Control Plane が起動して Session を 1 件でも永続化していれば `sessions.json`
と初期化マーカーができます。まだ無い空の `STATE_DIR` では orphan sweep は
必ず no-op なので、「reaper が orphan を消してくれない」と誤判定しないこと。

## 4. Issue #25 実機確認チェックリスト

### 4.0 API を叩く前の前提（認証 / CSRF / destination）

`/v1` の Session API は匿名 curl では通りません。実測で確認した契約は次の
とおりです（`apps/control-api/auth_api.py`, `apps/control-api/session_api.py`）。

- `POST /v1/sessions/{id}/prepare` は `require_user`（`irlight_session` cookie）
  と `require_csrf`（`X-CSRF-Token` ヘッダ）の両方が必須。
  - cookie 無し: **401** `not authenticated`
  - cookie あり・CSRF 無し/不一致: **403** `missing or invalid CSRF token`
- cookie は既定で `Secure`。平文 HTTP のローカル/検証環境では Control Plane に
  `COOKIE_INSECURE=1` を設定しないと curl / ブラウザが cookie を送りません。
- `IRLIGHT_PROVIDER=conoha` では `_destination_required()` が True になり、
  `destination_id` が必須。さらに destination は `verification_status=VERIFIED`
  かつ `secret_ref` の secret を復号できる必要があります。無い/未 VERIFIED なら
  **409**（`destination_id is required` / `destination must be verified before prepare`）。
  - lifecycle の確認だけが目的で egress 先を用意しない場合に限り、
    `IRLIGHT_REQUIRE_DESTINATION=0` で destination 必須を解除できます。これは
    検証用の逃げ道であり本番契約ではないため、実機の本番相当確認では
    VERIFIED destination を使ってください。

認証済みの prepare 例:

```bash
API=http://<control-plane>:8080      # 実機の到達先
EMAIL=...; PASSWORD=...
DEST=<VERIFIED な destination id>
SID=$(uuidgen | tr 'A-Z' 'a-z')
JAR=$(mktemp)

# 1. login: irlight_session / irlight_csrf を取得
# JSON serializer で quote / backslash / 改行等を保持する。秘密値を argv に載せない。
LOGIN_JSON=$(EMAIL="$EMAIL" PASSWORD="$PASSWORD" python3 -c \
  'import json, os; print(json.dumps({"email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}))') || exit 1
printf '%s' "$LOGIN_JSON" | curl -sS --fail -c "$JAR" -X POST "$API/v1/auth/login" \
  -H 'Content-Type: application/json' --data-binary @- || exit 1
unset LOGIN_JSON
CSRF=$(awk '$6=="irlight_csrf"{print $7}' "$JAR")

# 2. prepare: cookie + X-CSRF-Token + Idempotency-Key + destination
curl -sS -b "$JAR" -X POST "$API/v1/sessions/$SID/prepare" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: issue25-double-prepare' \
  -d "{\"environment\":\"dev\",\"destination_id\":\"$DEST\",\"egress_mode\":\"DIRECT_PUSH\"}"
```

### A. double prepare

1. 新しい Session ID で prepare する（上記 4.0 の認証済みリクエスト）。
   成功時 `status=READY_WAIT_INGEST`、`provider_server_id` / `provider_volume_id`
   を記録する。
2. 同じ Session ID / 同じ `Idempotency-Key` でもう一度 prepare する。2 回目は
   同じ server / volume を返す replay になる。同じ `Idempotency-Key` で
   `destination_id` や `egress_mode` を変えると 409（binding 済み）。
3. `python3 -m provider.admin_cli list` で対象 session の server / volume が各1個だけであることを確認する。

### B. provisioning 中 stop

1. 新しい Session を prepare する。
2. provider resource の作成途中で stop（`POST /v1/sessions/{id}/stop`、CSRF 必須）
   を発行する。
3. stop または次回 reaper 後に対象 session の resource が残っていないことを確認する。

### C. server 手動削除後の orphan volume

1. prepare 済み Session の server ID / volume ID を記録する（Session は
   `READY_WAIT_INGEST` のまま）。
2. ConoHa 側で server だけを削除し、volume を残す。
3. `sudo systemctl start irlight-reaper.service` で reaper を即時実行する。
   **この時点では orphan volume は削除されない。** `reaper._orphan_delete_allowed`
   は Session が store に存在し、かつ status が終端状態
   (`STOPPED` / `FINISHED` / `FAILED`) でない限り orphan 削除を拒否する
   （`apps/control-api/reaper.py`）。`READY_WAIT_INGEST` の Session の volume は
   `orphan_cleanup=0` のまま残る（実測）。
4. Session を終端状態にする。次のいずれか。
   - `POST /v1/sessions/{id}/stop`（CSRF 必須）で停止する。
   - または stop せず放置し、no-ingest timeout（既定 3600 秒）の到達を待つ。
     `READY_WAIT_INGEST` のまま `ready_at` から 3600 秒を超えた Session は
     reaper の `deadline_stops` として停止・cleanup される。
5. `provider.admin_cli list` で対象 session の resource が 0 件になることを
   確認する。

補足: reaper の orphan sweep は backstop であり、終端状態の Session（または
store に Session が無い）の resource だけを対象にします。さらに
`orphan_grace_seconds`（既定 300 秒、resource の作成時刻から計測）を経過して
いる必要があり、同一 Session の server がまだ残っている volume はスキップ
されます。終端化した直後は次の 5 分周期 sweep を待つか、
`sudo systemctl start irlight-reaper.service` を再実行してください。

### D. 最終残骸確認

prepare 済みのまま放置した Session は server / volume を保持し続けます
（stop か no-ingest timeout で回収）。検証に使った全 Session を stop してから
次を実行し、対象 Session の resource が 0 件であることを確認します。

```bash
python3 -m provider.admin_cli list
```

検証に使った Session ID、実行時刻、各ステップ前後の managed-resource 一覧、
reaper の journal を Issue #25 に貼り、秘密値は必ず伏せます。

## 5. 実機ヘルパー

`STATE_DIR` / 認証契約 / 上記の stop を挟む手順をそのまま実行できる実機検証
ヘルパー `verify-conoha-issue25.sh` を用意しています（リポジトリ内ではなく
Issue #25 の検証作業に添付）。provider 層の double prepare・reaper（別
プロセス、共有 `STATE_DIR`）・残骸確認と、host 側の systemd install/verify
（本 §3 と同じコマンド＋初期化済み STATE_DIR の確認）を出力します。
destructive（課金）工程は `CONFIRM_CONOHA_BILLING=1` を明示するまで実行しません。
