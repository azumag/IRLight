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
  "max_active_sessions_per_user": 3,
  "sessions_active": 12,
  "sessions_expired": 3,
  "sessions_total": 15,
  "status": "OK",
  "users_with_active_sessions": 8,
  "users_with_multiple_active_sessions": 3
}
```

`gc_runs_required` は既定の GC batch size 1,000 件、または `--max-delete` で指定した 1〜10,000 件を使って、現在の期限切れ件数を回収するのに最低何 run 必要かを示す見積りである。CLI 自体は削除を実行しない。

`users_with_active_sessions`、`users_with_multiple_active_sessions`、`max_active_sessions_per_user` は、ユーザー別 Session 上限を決める前に active Session の集中度を把握するための **識別子なし aggregate** である。期限切れ Session はこの3値へ含めない。ユーザー ID ごとの件数や上位ユーザー一覧は出力しないため、この CLI 単体から特定ユーザーの Session 保有状況は取得できない。

実際の回収は、状態と運用タイミングを確認したうえで既存 CLI を使う。

```bash
python apps/control-api/auth_session_gc.py --dry-run
python apps/control-api/auth_session_gc.py --max-delete 1000
```

## Read-only 境界

inspector は auth store の通常 lock helper を呼ばない。現在の lock helper は lock file / directory を作成できるため、診断だけで filesystem を変化させないためである。auth authority writer は一時ファイルから `os.replace()` するため、inspector は開いた regular-file inode の byte snapshot を検査する。

inspector は authority を開く前に effective inspection clock も検証する。明示された inspection time と既定の system clock は同じ有限値境界を通り、`NaN` / `Infinity` / `-Infinity` を拒否する。異常な clock 値で active / expired の集計を推測せず、authority file に触れる前に fail-closed にする。

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

この CLI の aggregate count は、期限切れ Session が継続的に蓄積していないか、1 回の bounded GC で backlog を処理し切れるか、active Session が少数ユーザーへ偏っているかを見るための入力として使える。ただし repository 内では次を勝手に決めない。

- production で何件を warning / critical とするか。
- GC の実行周期。
- ユーザー別 active Session 上限。
- alert の通知先や paging policy。

これらは実利用量と deployment policy を確認して決める。inspector の結果だけを理由に有効 Session を失効させたり、既存配信を停止したりしない。特に `max_active_sessions_per_user` は強制上限ではなく、上限値を決めるための観測値にすぎない。

## 回帰テスト

`tests/test_auth_session_retention_inspect.py` で以下を固定する。

- expiry 境界 `expires_at <= now` の集計。
- bounded GC run 数の算出。
- active Session のユーザー集中度 aggregate と、期限切れ Session を除外すること。
- 成功出力にユーザー ID を含めず、aggregate だけを返すこと。
- inspection 前後で authority の content / mtime / directory entries が変わらないこと。
- duplicate key、非有限値、不正 record の fail-closed。
- default system clock が非有限値なら authority access 前に拒否すること。
- symlink state の拒否。
- 成功・失敗出力に token hash、user ID、CSRF token、入力 path を出さないこと。
