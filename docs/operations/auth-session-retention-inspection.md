# Authentication Session retention inspection

Issue #86 の期限切れ認証 Session 回収を、authority を変更せずに観測するための read-only CLI を用意する。

## 目的

`auth_session_gc.py` は期限切れ Session を有界件数ずつ実際に削除する。一方、通常の監視や障害調査では、まず現在の保持量と GC backlog を **書き込みなし** で確認したい。`auth_session_retention_inspect_cli.py` はその用途に限定する。

```bash
python apps/control-api/auth_session_retention_inspect_cli.py
```

正常時は次の aggregate 値だけを JSON で返す。

```json
{
  "gc_runs_required": 1,
  "sessions_active": 12,
  "sessions_expired": 3,
  "sessions_total": 15,
  "status": "OK"
}
```

`gc_runs_required` は既定の GC batch size 1,000 件、または `--max-delete` で指定した 1〜10,000 件を使って、現在の期限切れ件数を回収するのに最低何 run 必要かを示す見積りである。CLI 自体は削除を実行しない。

実際の回収は、状態と運用タイミングを確認したうえで既存 CLI を使う。

```bash
python apps/control-api/auth_session_gc.py --dry-run
python apps/control-api/auth_session_gc.py --max-delete 1000
```

## Read-only 境界

inspector は auth store の通常 lock helper を呼ばない。現在の lock helper は lock file / directory を作成できるため、診断だけで filesystem を変化させないためである。auth authority writer は一時ファイルから `os.replace()` するため、inspector は開いた regular-file inode の byte snapshot を検査する。

さらに次を fail-closed にする。

- `auth_sessions.json` が regular file ではない、または symlink である。
- open 前後で inode identity が変わった。
- invalid UTF-8 / invalid JSON。
- duplicate JSON object key。
- `NaN` / `Infinity` / `-Infinity`。
- token hash、CSRF token、timestamp 等が auth authority の現在の保存契約を満たさない。

異常時の標準出力は固定値だけで、state path、token hash、user ID、CSRF token、raw JSON、parser error は返さない。

```json
{
  "reason_code": "AUTH_SESSION_STATE_UNAVAILABLE",
  "status": "UNAVAILABLE"
}
```

exit code は `2`。壊れた authority を空 state とみなしたり、marker / lock / JSON を自動作成・修復したりしない。

## 監視への利用

この CLI の aggregate count は、期限切れ Session が継続的に蓄積していないか、1 回の bounded GC で backlog を処理し切れるかを見るための入力として使える。ただし repository 内では次を勝手に決めない。

- production で何件を warning / critical とするか。
- GC の実行周期。
- ユーザー別 active Session 上限。
- alert の通知先や paging policy。

これらは実利用量と deployment policy を確認して決める。inspector の結果だけを理由に有効 Session を失効させたり、既存配信を停止したりしない。

## 回帰テスト

`tests/test_auth_session_retention_inspect.py` で以下を固定する。

- expiry 境界 `expires_at <= now` の集計。
- bounded GC run 数の算出。
- inspection 前後で authority の content / mtime / directory entries が変わらないこと。
- duplicate key、非有限値、不正 record の fail-closed。
- symlink state の拒否。
- 成功・失敗出力に token hash、user ID、CSRF token、入力 path を出さないこと。
