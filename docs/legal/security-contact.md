# セキュリティ連絡先と脆弱性報告（ドラフト）

Status: Draft / 専門家確認前。公開前に窓口と手順を確定します。

## 連絡先

- セキュリティ窓口: 【要補完: security@...】
- PGP public key: 【要補完: key id / fingerprint / URL】
- 受付言語: 日本語 / English
- 受信確認: 受理後、通常 N 営業日以内に確認連絡 【要補完】

## 報告内容

報告には、以下を含めてください。

- 影響を受ける URL / 機能 / コンポーネント
- 再現手順または proof-of-concept（安全な範囲）
- 影響範囲・想定被害
- 連絡先（匿名報告も可能な場合はその旨を明記）
- stream key、ingest credential、destination secret、Authorization header、データベース認証情報、個人情報などの実秘密は含めないでください

## スコープと禁止事項

- 本番環境への影響を与えるテスト、大量リクエスト、サービス妨害、データ削除・変更を伴う検証は、事前の書面同意なしに行わないでください。
- 第三者への被害や権利侵害が及ぶ検証は行わないでください。
- 脆弱性情報は、修正・通知が完了するまで公開しないでください（公開時期は IRLight と調整します）。

## 対応方針

- IRLight は報告を受け次第、再現確認、影響範囲、修正、通知、必要に応じた credential rotation / Session 停止を実施します。
- 不正利用が継続している場合は、`docs/operations/emergency-abuse-stop.md` の緊急停止 runbook に従って最小単位で対応します。
- Fix 後の root cause と再発防止策は、Issue / PR / runbook に無害化した形で記録します。

## 参照

- Issue #12
- `docs/operations/emergency-abuse-stop.md`
- `docs/operations/secret-exposure-suspected.md`