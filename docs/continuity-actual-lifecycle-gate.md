# Continuity actual lifecycle gate

Issue #4 の Session lifecycle は、入力publisherの接続だけで `LIVE` へ昇格せず、Continuity Engineが実際に通常映像へ切り替えたことを復帰条件に含める。

## 背景

Node Agentのingest observationはMediaMTX / ffprobeから入力の接続・品質を観測する。一方、Continuity Engineは入力復帰後に `RECOVERY_STABLE_SECONDS`（既定3秒）の安定確認を行い、その間は `STABILIZING` のまま待機映像を出す。

従来はControl Plane Sessionがingest `ACCEPTED` を受けた時点で即 `LIVE` になり、実映像はまだ待機画面という短い不一致があった。

## 方針

Continuity Engineが `/state/status.json` に出すsafeなactual stateをNode Agentがread-onlyで読む。

- `session_status=LIVE`
- `video_source=LIVE`

の両方を満たすまで、onlineかつusableなingest statusをheartbeat上では一時的に `PENDING` とする。

対象:

- `ACCEPTED`
- `WARNING`
- `DEGRADED`

`OFFLINE / REJECTED / UNKNOWN` などは隠さずそのまま報告する。

## Event semantics

`PENDING` でも `online=true` は維持するため、最初の接続・再接続時の次の監査eventは失われない。

- `ingest.connected` / `ingest.reconnected`
- `ingest.format_detected`

ただしSession lifecycleは昇格させない。

Continuity actualがLIVEになった次のheartbeatで元のingest statusを報告し、既存state machineが次を行う。

- `READY_WAIT_INGEST -> LIVE`
- `READY_WAIT_INGEST -> DEGRADED`
- `HOLDING -> LIVE`
- `HOLDING -> DEGRADED`
- `DEGRADED -> LIVE`

切断や既にLIVE中のquality degradationは従来どおり即時反映する。

## 安全性

Node Agentが読むContinuity statusはallow-listしたactual fieldだけに正規化し、egress URL・command ID等をheartbeatへ持ち込まない。status fileが欠損・破損・staleの場合はfail closedでactual不明とし、usable inputをSession LIVEへ昇格させない。

Control Planeに別の3秒timerを実装しないため、Media Planeの実際の映像切替とSession lifecycleが同じ判定結果を共有する。
