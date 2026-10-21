# 通報・停止・異議申立て runbook（ドラフト）

Status: Draft / 専門家確認前。Issue #12 の「通報・緊急停止」「利用規約」に基づく初版です。

## 受付

- 公開通報窓口を設置します（窓口 URL / メールは `docs/legal/security-contact.md` と運用設定で確定）。
- 通報には、対象 URL / Session / user、事象の概要、発生日時、権利侵害の根拠（可能な範囲）を含めます。
- secret、credential、個人情報の不要な貼付を避けるよう案内します。

## トリアージ

1. 緊急性（継続的な不正送出、重大な権利侵害、法的要求）を判定する。
2. 対象を Session 単位で最小化し、無関係なユーザー / Node を巻き込まない。
3. 誤通報の可能性、異議申立ての余地、既存 Session の実状態を確認する。
4. 必要な metadata（Session ID、user ID、Node ID、operator、時刻、理由）を記録する。

## 停止

- 通常の Session 停止経路をまず試し、継続する場合は ingest credential を失効する。
- DIRECT_PUSH の目的地配送を停止し、runtime secret の cleanup を確認する。
- Node 不調時は `docs/operations/emergency-abuse-stop.md` に従って隔離し、理由を記録する。

## 証拠保全

- 保全は metadata と監査ログを基本とし、映像・音声の無断保存は行いません。
- retention と access control は `docs/operations/data-retention-deletion.md` に従います。

## 異議申立て・誤通報

- 誤通報の場合は、解除判断と理由を監査履歴に残します。
- 解除時も旧 credential を復活させず、必要なら新しい Session / credential を発行します。
- 反復違反ユーザーの停止、法的要請への対応窓口と記録方式は運用・法務確認が残ります。

## 完了条件（運用）

- 公開通報窓口が存在する
- 管理者が不正 Session を即時停止できる
- 通報・監査・データ削除フローが定義される

参照:
- Issue #12
- `docs/operations/emergency-abuse-stop.md`
- `docs/operations/data-retention-deletion.md`