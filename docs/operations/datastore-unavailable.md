# DB / Redis 障害 runbook

Issue #11 の「DB/Redis障害」に対する運用手順です。PostgreSQL / Redis互換ストアを導入した構成を対象とし、現在のファイルauthority障害は `state-readiness.md` / `state-restore-drill.md` を優先します。

## 原則

- 接続不能を「空のデータ」と解釈しない。owner、Session、Entitlement、Node割当を推測補完しない。
- DB/Redisが不明な状態で新規課金、Session prepare、resource作成、reaper cleanupを進めない。
- 進行中Media Sessionを、Control Planeの保存障害だけを理由に即時停止しない。Media Planeの既存継続性を独立確認する。
- credential、DSN、password、接続文字列をissue/chat/ログへ貼らない。
- failover、restore、promotion、データ削除は状態と世代を確認したうえで別の承認操作として行う。このrunbook自体は自動変更を行わない。

## 検知

- Control Planeのreadinessがdatastore理由で非ready。
- APIのDB/Redis接続エラー率が継続上昇。
- transaction / lock / queue処理がtimeoutし、Session prepareやevent保存が失敗。
- billing webhookやoutboxの未処理件数が増え続ける。
- Redis依存の短期lock/event配信が失われ、再接続や重複処理の兆候がある。

単発の外部provider timeoutと、authority datastore自体の障害を混同しない。

## 影響判定

secretを含まない範囲で次を記録します。

- 最初に異常を確認した時刻
- 影響サービスとreason code
- read / writeのどちらが失敗しているか
- 進行中Session数、prepare中Session数
- webhook/outbox/backlogの件数
- DB/Redisのprimary/replicaまたはmanaged service healthの公開状態
- 直近deploy / migration / network変更の有無

read成功・write失敗の場合も「一部正常」とみなして新規変更を続けず、書込整合性を優先します。

## 暫定対応

1. 新規Session prepare、課金を伴うresource作成、管理者補正など不可逆なwriteを止める。
2. 既存SessionのMedia Plane heartbeat / continuity / egressを別経路で確認する。
3. UI/APIは固定reasonで「準備を開始できない」ことを返し、内部DSNや例外本文を公開しない。
4. webhook/outboxは捨てず、重複排除キーを保持したままbacklogとして残す。
5. Redisだけが失われた場合も、lock/eventの意味を推測して同じ処理を二重実行しない。

自動的なDB初期化、空schema作成、Redis flush、古いbackupの即時restoreは行いません。

## 復旧

- DB/Redisの接続、schema/version、read/write、transaction/lockの基本動作を確認する。
- migration適用状況を確認し、アプリとschemaの互換versionが一致することを確認する。
- backlogはidempotency key / event IDを使って再処理し、重複Entitlement、二重Session、二重課金を作らない。
- 進行中SessionとNode/provider inventoryをread-onlyで照合してから通常prepare/reaperを再開する。
- readinessが安定して成功し、エラー率とbacklogが減少していることを確認してwrite制限を解除する。

restoreが必要な場合は、復旧時点・RPO/RTO・credential fencingを確認し、`state-restore-drill.md`と同じ「古い状態をそのまま正としない」原則を適用します。

## 事後作業

- 原因、影響時間、失敗/遅延した操作、backlog再処理結果を記録する。
- migration、connection pool、timeout、lock、failoverの回帰試験を追加する。
- 失われたeventやusageがある場合は、推測で補完せず監査可能な再集計・補填手順へ接続する。
- 利用者影響や課金影響があればstatus案内と補填判断へ接続する。
