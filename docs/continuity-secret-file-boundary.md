# Continuity secret-file input boundary

Continuity は `INPUT_URI_FILE` / `EGRESS_URL_FILE` などの `*_FILE` 設定を優先して読み込み、値そのものを process arguments や通常ログへ展開しない。

## Reader contract

`apps/continuity/secret_files.py` の file reader は次を保証する。

- 読み込み対象は open 後の file descriptor が regular file であることを確認する。FIFO / device などは拒否する。
- `O_NONBLOCK` が利用可能な環境では付与し、誤って FIFO 等を指定した場合に startup が read で無期限 block しないようにする。
- 1 secret file は最大 64 KiB とし、上限を超える入力を丸ごと memory へ読み込まない。
- UTF-8 として解釈できない内容は controlled `RuntimeError` にする。
- operator-local pathname や生の `OSError` detail を通常の exception message / chained cause へ含めない。
- secret manager / projected volume で使われることがある symlink-to-regular-file は互換性のため維持する。symlink 自体を trust root とはせず、open 後の FD が regular file かを判定する。
- `IRLIGHT_SECRET_WAIT_SECONDS` は従来どおり missing file / empty file の startup race を吸収するために使い、0〜300 秒へ clamp する。非有限値は既定値へ戻す。

この境界は secret value の正当性や destination URL policy を決めるものではない。それらは既存の URL / session / destination validation が担当する。

## Failure behavior

configured file が期限内に読めない、regular file でない、64 KiB を超える、UTF-8 でない、または空のまま期限に達した場合は env fallback へ silently downgrade せず fail closed とする。`*_FILE` が未設定の場合だけ従来どおり environment value / default を使う。

## Regression coverage

`tests/test_continuity_secret_files.py` で regular file 優先、symlink compatibility、FIFO rejection、size bound、invalid UTF-8、missing-file path redaction、non-finite wait configuration と URL redaction を固定する。
