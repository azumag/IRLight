# Egress runtime timer configuration

Egress Gateway の接続監視・状態更新に使う runtime timer は、production entrypoint で pipeline や Destination 接続処理を始める前に検証する。

## Finite timer boundary

次の設定は、明示する場合は有限の数値でなければならない。

| 設定 | 既定値 | 用途 |
| --- | ---: | --- |
| `EGRESS_CONNECT_TIMEOUT_SECONDS` | 15 | 初回 publish 成立待ち |
| `EGRESS_STATUS_HEARTBEAT_SECONDS` | 5 | 接続中 status heartbeat 間隔 |
| `EGRESS_CONNECT_STABILITY_SECONDS` | 3 | publish 成立後の安定確認時間 |
| `EGRESS_OUTPUT_STALL_TIMEOUT_SECONDS` | 5 | 接続成立後の output progress 停止検知 |

Python の `float()` は `NaN`、`Infinity`、`-Infinity` を通常の変換成功として扱うため、比較や `max(0.0, value)` だけでは安全な上限にならない。Egress Gateway はこれらの非有限値と malformed value を起動時に拒否し、実際の Egress pipeline、DNS guard、Destination 接続を開始しない。

この境界では既存の有限値の range semantics を変更しない。0 や負値を新たに別の意味へ変更せず、各 runtime class が従来どおり clamp / disable semantics を適用する。

設定エラーの通常ログには raw 環境変数値、Destination URL、stream key を含めない。

## Retry policy との関係

`EGRESS_RETRY_INITIAL_SECONDS`、`EGRESS_RETRY_MAX_SECONDS`、`EGRESS_RETRY_MULTIPLIER`、`EGRESS_RETRY_JITTER_RATIO`、`EGRESS_MAX_RETRY_SECONDS` は `ReconnectPolicy` が有限値と既存の値域を検証する。今回の runtime timer 境界はそれを置き換えず、connect / heartbeat / stability / stall の残っていた timer を同じ fail-closed 方針へ揃える。

## Verification

変更時は少なくとも以下を回帰確認する。

- 4 timer それぞれで既定値と通常の有限値が従来どおり使える。
- `NaN` / `Infinity` / `-Infinity` をすべて拒否する。
- malformed value の元文字列を exception chain や startup log へ露出させない。
- Egress Gateway image が validator module を含み、production CMD が validator を通る entrypoint を起動する。
- 通常 CI と RTMPS / SRT / disconnect recovery E2E を弱めず成功させる。
