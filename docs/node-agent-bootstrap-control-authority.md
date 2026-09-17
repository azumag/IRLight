# Node Agent bootstrap control authority

Node Agent は Control Plane の bootstrap response から、Continuity が読む初期 `control.json` を作成する。既存の `control.json` がある場合は bootstrap 値で上書きせず、operator / Control Plane による既存 authority を保持する。

## 現行の互換方針

rolling deployment で旧 Control Plane と新 Node Agent が一時的に混在できるよう、**フィールド欠落だけ**は legacy fallback として当面維持する。

- `audio_mode` 欠落: `LIVE`
- `audio_version` 欠落: `0`
- `audio_updated_at` 欠落: seed 時点の Node Agent の wall-clock time
- `audio_command_id` 欠落: `null`
- `audio_idempotency_key` 欠落: `null`

この fallback は「bootstrap payload の malformed 値を補正する」機能ではない。フィールドが存在する場合は fixed control-state schema と同じ境界で検証し、不正値を authority として永続化しない。

## present field の schema

- `audio_mode`: string の `LIVE` または `MUTED`
- `audio_version`: bool ではない 0 以上の integer
- `audio_command_id`: `null` または有効な UUID string
- `audio_idempotency_key`: `null` または 1〜200 文字の string
- `audio_updated_at`: bool ではない finite かつ 0 以上の number

list / dict 等の malformed `audio_mode`、empty idempotency key、negative / non-finite timestamp などは controlled `RuntimeError` とし、`control.json` を作らない。JSON serialization 失敗時も atomic temporary file から publish しない。

## strict schema へ移行するとき

`audio_mode` / `audio_version` / `audio_updated_at` を必須化して legacy fallback を削除する変更は mixed-version rollout の互換性を壊し得る。Control Plane bootstrap response の version/capability で全稼働系が complete schema を提供すると確認できた時点で、別の protocol/versioning 判断として行う。

したがって、現行契約では **missing は versioned compatibility、present malformed は fail-closed** とする。missing と malformed を同じ扱いに変更しない。
