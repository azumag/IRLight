# Control API admin token file boundary

Issue #418 の runtime secret-file hardening のうち、Control API の `NODE_INTERNAL_ADMIN_TOKEN_FILE` を扱う境界を定義する。

## 対象

`NODE_INTERNAL_ADMIN_TOKEN_FILE` は `/internal/nodes` の一覧取得や Node stop など、内部管理 API の bearer token をファイルから供給するために使う。token 本文は通常ログ、HTTP detail、例外 chain、Issue / PR へ出さない。

## 読み込み契約

- 解決後の対象は regular file に限る。FIFO、device、directory は content read 前に拒否する。
- 最大サイズは 64 KiB。読み込みは `limit + 1` までに制限し、途中で拡大した場合も拒否する。
- 利用可能な platform では `O_NONBLOCK` と `O_CLOEXEC` を付ける。
- inspection、open 後、read 後で device / inode / mode / size / mtime / ctime を照合し、pathname の差し替えや同一 inode の更新を fail-closed にする。
- UTF-8 として解釈できない内容は拒否する。
- Docker / Kubernetes の projected secret volume 互換のため、symlink が regular file に解決される既存ケースは維持する。検査中に symlink target が切り替わった場合は、その一回の request を安全側で拒否する。

## 既存 semantics の維持

この hardening では admin auth policy を変更しない。

- `NODE_INTERNAL_ADMIN_TOKEN_FILE` が設定されていて、missing / unreadable / special file / oversized / invalid UTF-8 / unstable の場合は HTTP 503 `node admin authentication is unavailable`。`NODE_INTERNAL_ADMIN_TOKENS` へ黙って fallback しない。
- token file が存在するが空白だけの場合は値を追加しない。既存どおり、development 用の `NODE_INTERNAL_ADMIN_TOKENS` が別に設定されていればその値は利用可能。
- 有効な file token と environment token が両方ある場合は、既存どおり両方を許可する。
- token の SHA-256 digest 比較、Node admin endpoint、Node authority、RBAC / network exposure の契約は変更しない。

## エラー境界

reader の失敗理由は外部へ細分化しない。公開 HTTP detail は固定 503 とし、operator-local pathname、token 値、生の `OSError` を chained cause として保持しない。詳細な原因調査が必要な場合も secret file 本文を表示せず、mount / file type / permission / deployment configuration を別の read-only 診断で確認する。

## 残件

Issue #418 では Egress Gateway の destination/input runtime secret reader が引き続き残る。この変更だけで destination URL、stream key、secret rotation / KMS policy を完了扱いにはしない。
