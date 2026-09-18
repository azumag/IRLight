# Node Agent runtime secret-file boundary

Node Agent が runtime で読む secret file は、公開 HTTP 入力ではなく operator-controlled configuration である。それでも誤設定された FIFO/device、巨大ファイル、読み取り中の置換や更新をそのまま受け入れると、起動停止・メモリ消費・診断情報へのローカル path 混入につながるため、読み取り境界を明示的に制限する。

## 現在の保証

`apps/node-agent/runtime_secret_file.py` の reader は次を保証する。

- resolved target は regular file のみ許可する。FIFO、device、directory は content read 前に拒否する。
- `O_NONBLOCK` と `O_CLOEXEC` が利用可能な platform では open flag に付ける。
- 最大 64 KiB とし、`limit + 1` までの bounded read で inspection 後の増大も検出する。
- UTF-8 として decode できない内容は controlled error にする。
- open 前の target metadata、open 済み FD、read 後の FD、read 後に path が解決する target を比較し、inode の差し替えや通常の in-place mutation を fail-closed にする。
- error message と chained exception に secret value、operator-local path、生の `OSError` を載せない。

Docker/Kubernetes の projected secret volume との互換性を保つため、**symlink から regular file への解決は許可する**。final symlink 自体を一律拒否する境界ではない。

## `NODE_BOOTSTRAP_TOKEN_FILE` の互換性

`NODE_BOOTSTRAP_TOKEN_FILE` が設定されている場合は file を優先する。安全に読めない configured file は env へ黙って fallback せず fail-closed にする。一方、file 自体は安全に読めるが trim 後に空である場合は、従来どおり `NODE_BOOTSTRAP_TOKEN` へ fallback する。

この変更は token rotation、KMS/envelope encryption、bootstrap token の発行・消費 semantics を変更しない。

## `NODE_INGEST_SAMPLE_URL_FILE` の互換性

`NODE_INGEST_SAMPLE_URL_FILE` が設定されている場合、ingest quality sampler は sample 実行時に同じ bounded reader を使って authenticated RTSP URL を読む。configured file を安全に読めない場合は `NODE_INGEST_SAMPLE_URL` へ fallback せず、従来どおり `MEDIA_SAMPLE_FAILED` として fail-closed にする。trim 後に空の file も従来どおり failure であり、fallback はしない。

URL の値は ffprobe の argv には渡さず stdin の ffconcat input にだけ渡す既存契約を維持する。この変更は ingest 認証、sampling threshold、DEGRADED 判定、bootstrap lifecycle を変更しない。

## 残件

Issue #418 のうち、この境界を適用したのは Node Agent の bootstrap token と authenticated ingest sample URL reader までである。Egress Gateway の destination/runtime secret と Control API の admin token reader は、個別の既存 fallback semantics と image packaging を維持しながら順次移行する。
