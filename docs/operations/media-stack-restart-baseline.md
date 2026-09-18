# Media stack restart baseline の安全な入力境界

`media_stack_inspect_cli.py --restart-baseline` は、直前に保存した同 inspector の redacted JSON を使って service ごとの restart count delta を計算するための read-only 診断入力である。baseline は authority ではなく診断証跡であり、読み込み失敗時に現在値を推測したり、自動復旧へ進んだりしない。

## ファイル契約

baseline は最大 1 MiB の regular file に限る。final symlink、FIFO、device などの non-regular input は拒否し、利用可能な環境では `O_NOFOLLOW` / `O_NONBLOCK` を付けて開く。read は上限 + 1 byte に制限し、open 前後と read 前後で device / inode / size / mtime / ctime を比較する。read 完了後にも final pathname を `lstat` し、同じ regular-file identity を指していることを確認する。

したがって、baseline の pathname が検査後に別ファイルへ差し替えられた場合、同じ inode が読み込み中に変更された場合、または読み込み後に pathname が別 identity へ置き換わった場合は `UNAVAILABLE` として fail-closed になる。JSON nesting が parser recursion limit を超える場合も traceback を外へ出さず同じ controlled failure とする。

## 運用上の注意

baseline は operator / monitoring 側で保存・ローテーションし、inspector 自身は作成・更新・削除しない。secret、credentialed URL、container environment、command line を baseline に混ぜず、同じ `egress_mode` と expected service 集合の redacted inspector JSON だけを使う。

baseline が `UNAVAILABLE` になった場合、ファイルの置換・破損・権限・容量・形式を read-only で確認する。診断入力が読めないことを理由に container restart、recreate、volume 削除、provider 操作へ自動的に進まない。基本の crash-loop 切り分けは [session-process-crash-loop.md](session-process-crash-loop.md) を参照する。
