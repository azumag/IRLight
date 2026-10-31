# Media Control Node reconcile design

Issue #15 / Roadmap #692 Phase 2 の Node delivery / acknowledgement を、既存の Node heartbeat と Continuity の共有 state を使って閉ループ化する。

## 既存境界

- Control Plane Session authority: `media_control.audio` (`desired_mode`, `actual_mode`, `version`, `command_id`)
- Node-local desired authority: `/state/control.json`
- Continuity runtime observation: `/state/status.json`
  - `desired_audio_mode`
  - `actual_audio_mode`
  - `control_version`
  - `command_id`
  - `updated_at`
- Node Agent heartbeat: Node -> Control Plane の定期通信が既にある

このため追加の常時接続チャネルを先に導入せず、heartbeat を reconcile transport として使う。

## Control Plane -> Node

Heartbeat response に最新の Session audio command を含める。

```json
{
  "desired_state": "RUNNING",
  "media_control": {
    "audio": {
      "desired_mode": "MUTED",
      "version": 4,
      "command_id": "...",
      "updated_at": 0
    }
  }
}
```

Node Agent は local `control.json` へ atomic に適用する。

- remote version > local version: 適用
- remote version == local version かつ command/mode一致: no-op
- remote version < local version: stale responseとして無視
- 同一versionで command/mode が食い違う: authority conflict として fail closed。既存 local state を破壊しない
- Node再起動時も既存 `control.json` はbootstrap seedで上書きせず、次heartbeatで最新versionへ収束

Bootstrap response も assigned Session の最新 audio command を seed 値として使う。固定 `NODE_INITIAL_AUDIO_MODE` は未割当/legacy用fallbackに限定する。

## Node -> Control Plane

Node Agent は Continuity の `status.json` を読み、heartbeat request に observation を付与する。

```json
{
  "media_control": {
    "audio": {
      "actual_mode": "MUTED",
      "control_version": 4,
      "command_id": "...",
      "observed_at": 0
    }
  }
}
```

Control Plane は Session authority の current command と version + command_id が一致する observation だけを ACK として採用する。

- actual == desired: `media_control.audio.applied`
- pipeline/runtime失敗: `media_control.audio.failed` + reason code
- restart/reconnect後に同じdesiredへ再収束した場合: 必要に応じ `media_control.audio.reconciled`
- stale ACK は current actual を巻き戻さない
- future/unknown command は current authority を上書きしない

## Safety

- final-state commandのみ。toggleは導入しない
- Node access tokenで既存heartbeat認証境界を再利用する
- command/version不一致を成功扱いしない
- Continuity statusが古い/読めない場合は applied としない
- Control Plane一時断では local desired を維持する

## 完了条件

- Web/APIで受理された `desired_mode` がNodeの `control.json` へ到達する
- Continuityが同じ `version` / `command_id` の actual を返す
- Control Plane `actual_mode` が ACK 後に `LIVE` / `MUTED` へ遷移する
- stale/out-of-order heartbeatで状態が巻き戻らない
- Node再起動とControl Plane一時断後にdesiredへ自動収束する
- requested / applied / failed / reconciled の監査イベントが重複なく記録される

Generated with ChatGPT
