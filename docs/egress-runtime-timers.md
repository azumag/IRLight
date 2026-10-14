# Egress runtime timer configuration

Egress Gateway の接続監視・状態更新に使う runtime timer は、production entrypoint で pipeline や Destination 接続処理を始める前に検証する。

## Finite timer boundary

次の設定は、明示する場合は有限の数値でなければならない。

| 設定 | 既定値 | 用途 |
| --- | ---: | --- |
| `EGRESS_CONNECT_TIMEOUT_SECONDS` | 15 | 初回 publish 成立待ち |
| `EGRESS_STATUS_HEARTBEAT_SECONDS` | 5 | 接続中 status heartbeat 間隔 |
| `EGRESS_CONNECT_STABILITY_SECONDS` | 3 | publish 成立後の連続した安定確認時間 |
| `EGRESS_OUTPUT_STALL_TIMEOUT_SECONDS` | 5 | 接続成立後の output progress 停止検知 |

`EGRESS_CONNECT_STABILITY_SECONDS` は最初に一度 ready を観測してからの単純な経過時間ではない。`CONNECTED` 確定前に sink が not-ready へ戻った場合は stability window を破棄し、次に ready へ戻った時点から全期間を数え直す。malformed / missing な GStreamer counter は ready として補完せず、安定確認時間にも算入しない。production default の `rtmpsink` と opt-in `rtmp2sink` の双方で同じ連続性契約を使う。

Python の `float()` は `NaN`、`Infinity`、`-Infinity` を通常の変換成功として扱うため、比較や `max(0.0, value)` だけでは安全な上限にならない。Egress Gateway はこれらの非有限値と malformed value を起動時に拒否し、実際の Egress pipeline、DNS guard、Destination 接続を開始しない。

この境界では既存の有限値の range semantics を変更しない。0 や負値を新たに別の意味へ変更せず、各 runtime class が従来どおり clamp / disable semantics を適用する。

設定エラーの通常ログには raw 環境変数値、Destination URL、stream key を含めない。

## Retry policy との関係

`EGRESS_RETRY_INITIAL_SECONDS`、`EGRESS_RETRY_MAX_SECONDS`、`EGRESS_RETRY_MULTIPLIER`、`EGRESS_RETRY_JITTER_RATIO`、`EGRESS_MAX_RETRY_SECONDS` は `ReconnectPolicy` が有限値と既存の値域を検証する。今回の runtime timer 境界はそれを置き換えず、connect / heartbeat / stability / stall の残っていた timer を同じ fail-closed 方針へ揃える。

## legacy process isolation の liveness window との関係

`EGRESS_ISOLATED_LIVENESS_TIMEOUT_SECONDS`（既定 20）は `EGRESS_LEGACY_PROCESS_ISOLATION_CANARY=1` の legacy `rtmpsink` だけに効く。CONNECTED 後の child が出せる唯一の liveness evidence は status heartbeat の `progress` なので、child は既存の 250 ms sink poll でのみ report するため、この window は `EGRESS_STATUS_HEARTBEAT_SECONDS` を 250 ms 単位で切り上げた nominal report 間隔より厳密に広くなければならない。例えば heartbeat 0.1 秒なら 0.25 秒、0.3 秒なら 0.5 秒を超える window が必要になる。この nominal 間隔は scheduler / IPC delay の上限を保証しない。等しいか狭い場合、2 回の正常 report の間に deadline が切れて healthy attempt を fence し、短縮対象の `RECONNECTING` ループを延々と回すことになる。

canary 有効時にこの関係を満たさない設定は起動時に fail-closed で拒否する。`EGRESS_STATUS_HEARTBEAT_SECONDS=0`（heartbeat 無効）では child に報告経路がなく、window は arm されない。`EGRESS_ISOLATED_LIVENESS_TIMEOUT_SECONDS=0` は従来どおり非 bounded のままなので、この制約の対象外である。どちらの設定値もログへ出さない。

## Verification

変更時は少なくとも以下を回帰確認する。

- 4 timer それぞれで既定値と通常の有限値が従来どおり使える。
- `NaN` / `Infinity` / `-Infinity` をすべて拒否する。
- malformed value の元文字列を exception chain や startup log へ露出させない。
- ready → not-ready → ready では最後の ready から stability window を数え直し、連続 ready が規定時間続いた場合だけ接続成立へ昇格する。
- Egress Gateway image が validator module を含み、production CMD が validator を通る entrypoint を起動する。
- 通常 CI と RTMPS / SRT / disconnect recovery E2E を弱めず成功させる。
