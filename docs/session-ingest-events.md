# Node / Session ingest event integration

IRLight のNode Agentが観測するingest状態は、Node単体の診断情報だけでなく、ユーザーが所有するSessionの正式なevent streamへ関連付けます。

## Node bootstrap時のSession割当

Node Agentはbootstrap requestで `provider_server_id` を送ります。Control PlaneはSessionStoreから同じ `provider_server_id` を持つactive Sessionを検索し、1件だけ一致した場合にそのSessionへNodeをbindします。

Sessionには以下を保存します。

- `node_id`
- `node_boot_id`
- `node_registered_at`

Node側にも `session_id` と `session_assigned=true` を保存します。

同じprovider serverに複数のactive Sessionが一致する場合は409で拒否します。

### strict assignment

`NODE_BOOTSTRAP_REQUIRE_SESSION_ASSIGNMENT` で未割当Nodeの扱いを制御できます。

- `1`: provider serverに対応するactive Sessionがなければbootstrapを409で拒否
- `0`: legacy/PoC互換としてsynthetic session IDを返すが、ユーザーSession event streamには接続しない
- 未指定: `IRLIGHT_PROVIDER=conoha` ではstrict、fake providerではlegacy fallback

実ConoHaでは、ユーザーの `prepare` が作成したserver IDとNode Agentの `NODE_PROVIDER_SERVER_ID` が一致することを前提にします。未割当の本番Nodeをsilentに別Sessionとして起動しません。

## ingest event

Node heartbeatの状態差分から次のeventを生成します。

- `ingest.connected`
- `ingest.reconnected`
- `ingest.format_detected`
- `ingest.policy_changed`
- `ingest.degraded`
- `ingest.recovered`
- `ingest.rejected`
- `ingest.disconnected`

Control Planeのcredential認証失敗は別経路で `ingest.auth_failed` として同じSession event streamへ追記します。

Node由来eventのpayloadには以下の統計を含めます。

- node ID
- ingest status / path
- online
- source type / source ID
- bitrate / max bitrate
- track情報
- quality情報（FPS / GOP / timestamp等）
- reason / warning
- enforcement状態
- Node側observed timestamp

credential secret、password、tokenはevent payloadへ保存しません。

## Session lifecycle

Node heartbeatとSession event更新はSessionStoreの同一lock内で原子的に行います。

- `ACCEPTED / WARNING / DEGRADED` の入力がonlineになった場合
  - `READY_WAIT_INGEST -> LIVE`
  - `HOLDING -> LIVE`
  - 初回のみ `first_ingest_at` を設定
  - `last_ingest_at` を更新
- LIVE中にinputがofflineになった場合
  - `LIVE -> HOLDING`
  - `last_ingest_at` を更新

`PENDING` の段階では接続eventは記録しますが、format/policy判定が完了するまでSessionをLIVEへ遷移させません。`REJECTED` はLIVE遷移対象外です。

Sessionが既にSTOPPING / FINISHED / FAILED等へ移行している場合、Node heartbeatによるSession lifecycle更新は行いません。Node heartbeat自体は継続させ、cleanupと競合してAgent restart loopを起こさないようにします。

## Event streamの上限

Session eventは `next_event_seq` による単調増加sequenceを使います。保存件数は最新1000件に制限します。古いeventをdropしてもsequence番号は再利用しません。

ユーザーAPIから追加するeventも同じatomic append経路を使うため、Node eventとの同時書込みでsequenceを重複させません。

## auth failure

既知Session IDに対する認証失敗は `ingest.auth_failed` として記録します。

保存するのは source IP、protocol、MediaMTX publisher ID、node ID、lock scopeだけです。reason codeは `INVALID_CREDENTIAL` または閾値到達時の `RATE_LIMITED` です。raw credentialは保存しません。

unknown Session IDへの総当たりはSession eventを生成せず、PR #36のbounded auth guardだけで処理します。これにより攻撃者が任意の偽Session名でユーザーevent streamを肥大化させることを防ぎます。

## テスト

`tests/test_node_session_integration.py`:

- provider server IDから既存ユーザーSessionへbootstrapをbind
- strict modeで未割当provider serverを拒否し、失敗時はone-time tokenを消費しない
- ACCEPTED inputでLIVE + connected/format event
- disconnectでHOLDING + disconnected event
- reconnect + DEGRADEDでreconnected/format/degraded event

`scripts/smoke-session-ingest-events.sh`:

1. Node Agentを起動せずControl Planeを開始
2. ユーザー登録・Session prepareでfake provider serverを作成
3. その実provider server IDをNode Agentへ渡してbootstrap
4. Sessionの `node_id / node_boot_id / node_registered_at` を確認
5. wrong secretで `ingest.auth_failed` を確認
6. 認証付き720p RTMP publisherを接続
7. SessionがLIVEになり `ingest.connected / ingest.format_detected` が記録されることを確認
8. publisher終了後にHOLDING + `ingest.disconnected` を確認
9. event sequenceが一意・昇順で、raw secretが含まれないことを確認
