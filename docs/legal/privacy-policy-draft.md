# プライバシーポリシー（ドラフト）

Status: Draft / 専門家確認前。Issue #12 の「プライバシー・表示」に基づく初版です。

## 1. 収集するデータと目的

IRLight は、サービス提供・安全運用・課金・法令対応のために、以下のデータを収集します。

- アカウント情報（登録時の identifier、認証情報の hash、role、接触先）
- Destination 設定と暗号化された secret（平文は保存しない）
- 待機素材と関連メタデータ
- Session metadata（lifecycle、protocol、時刻、利用量）
- 接続・切断・障害 event および監査ログ
- 決済・entitlement に関する情報
- 運用診断に必要な非秘密 metadata

映像・音声そのものは標準では録画・保存しません。デバッグ目的の無断保存は行いません。

## 2. 外部委託先・クラウド・決済事業者

- IRLight は、クラウド VPS、object storage、監視・ログ基盤、決済事業者などの外部サービスを利用します。
- 外部委託先には、必要な範囲のデータのみを、契約・提供目的に従って渡します。【要確認: 利用事業者一覧とデータ Processing Agreement の管理方針】
- Node / relay の実行基盤は provider ごとにネットワーク境界が異なるため、production の配置・TLS・到達制限は別途受入対象です。

## 3. Cookie / 分析

- 認証・セッション維持・CSRF 対策のために Cookie または同等の token を使用します。
- 任意の分析 Cookie / tracker を導入する場合は、目的・保持期間・オプトアウト方法を別途明記します。【現状: 専用の分析 SDK は未導入】

## 4. データ保持期間

保持期間はデータ種別・法令・会計要件により異なります。具体的な保持期間は `docs/operations/data-retention-deletion.md` に整理します。少なくとも以下の区分を設けます。

- 契約・会計上必要な最小限の保持
- 監査・不正調査のための metadata 保全
- 退会要求への対応と削除完了の記録

保持期間が定まっていない項目は【要確認】として残します。

## 5. データ削除請求と問い合わせ

- ユーザーは、アカウント停止・データ削除を要求できます。
- 削除要求の受付・本人確認・完了通知・例外（法令・会計上必要な保持）の扱いは `docs/operations/data-retention-deletion.md` に従います。
- 問い合わせ窓口: `docs/legal/security-contact.md` に記載の連絡先を利用します（一般サポート窓口は別途設定）。【要確認: 一般サポート窓口】

## 6. セキュリティ連絡先

- セキュリティに関する連絡先・脆弱性報告手順は `docs/legal/security-contact.md` に記載します。
- secret や credential をメール本文に含めないでください。

## 7. 変更通知

- プライバシーポリシーの重要な変更は、適切な方法で通知します。【要確認: 通知方法】

## 8. 特定商取引法等

- 有料提供に必要な表記（事業者名、所在地、連絡先、販売価格、提供時期、支払方法、返品・キャンセル条件など）は `docs/legal/tokushoho-disclosure-draft.md` にまとめます。

参照:
- Issue #12
- `docs/operations/data-retention-deletion.md`
- `docs/legal/security-contact.md`
- `docs/legal/tokushoho-disclosure-draft.md`