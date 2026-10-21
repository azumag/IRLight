# ユーザー向け状態通知・復旧案内の契約

## 目的

Issue #11 が要求する「ユーザー向け状態表示」成果物について、repository の UI 本体がない gap を docs-only で閉じます。障害中の利用者への表示と、復旧後の補填案内までを契約として定義し、実 UI / API 実装は #9 および公開準備 #14 と調整して着手します。

本契約は read-only です。dashboard や runbook の情報をユーザー向けに再表示するだけであり、障害中の購入・Session prepare を無制限に受け付けない運用判断を自動化しません。

## 表示の原則

- 現在のサービス障害をダッシュボード上で表示する。
- 影響中の Session には、現在の状態と次にユーザーが行うべき操作を表示する。
- 誤解を避けるため、運用内部の host metric / node ID / stack trace を通常画面に出さない。
- status が不明なときは `UNKNOWN` とし、通常と表示しない。

## 状態の区分

| 状態 | 表示 | ユーザーへの説明 | 運用判断 |
| --- | --- | --- | --- |
| `healthy` | 緑 | 通常利用可能 | 特になし |
| `degraded` | 黄 | 一部機能が制限され、復旧作業中 | Session prepare は条件付きで受付 |
| `major_outage` | 赤 | 影響中の機能を明示、新規 prepare を制限 | #12 と連携した communication を優先 |
| `maintenance` | 青 | 予定メンテナンス、完了予定時刻 | draining / rollback 手順に従う |
| `unknown` | 灰 | 判定不能 | 直ちに值班へエスカレート |

## 影響中の Session 表示

- Session ID を correlation key として `docs/operations/log-redaction-audit.md` の schema と突き合わせ、入力・継続・出力の状態を追跡する。
- 入力が回復中のときは「待機映像へ切り替え中」と表示し、通常映像が戻るまでの期待動作を案内する。
- 利用権の補填が必要な場合は `entitlement` の導線を案内し、運用監査を残す。
- 障害中に購入や Session prepare を無制限に受け付けない。

## Status Page

- Status Page の導入は検討段階とし、本 doc では response shape と update cadence だけを定義する。
- 更新は dashboard の alert が Critical に達した場合に手動承認で行う。自動公開はしない。
- 復旧通知も Critical 解除後に運用判断で行う。

## 障害後の案内

- 影響時間
- 影響した機能
-補填の有無と申請方法
- 再発防止策の Issue 参照

補填の実行自体は運用判断であり、UI から直接実行させない。

## 関連

- `docs/operations/dashboard.md`
- `docs/operations/deploy-rollback.md`
- `docs/operations/alert-catalog.md`
- `docs/operations/state-readiness.md`
- `docs/operations/abuse-report-takedown.md`
