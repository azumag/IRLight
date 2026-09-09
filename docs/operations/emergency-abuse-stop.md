# 不正配信の緊急停止 runbook

Issue #11 / #12 の「不正配信の緊急停止」に対する運用手順です。権利侵害、違法利用、credential悪用、第三者への被害が継続しているなど、通常のSession停止より強い運用判断が必要な場合を対象にします。

## 原則

- 緊急停止は利用者影響を伴うため、自動判定だけで実行しない。対象Sessionと理由を人が確認し、操作主体と時刻を監査記録へ残す。
- 停止対象をSession単位で最小化し、無関係なユーザー、Node、全サービスを一括停止しない。
- 映像・音声の内容を標準で保存しない既存方針を維持する。必要な証拠保全はmetadataと既存監査記録を基本とする。
- stream key、ingest credential、destination secret、Authorization header等をticket/chatへ貼らない。
- provider側アカウント停止、返金、法的対応、データ長期保全は別の権限・判断事項として扱う。

## 検知

- 公開された通報窓口から具体的なSession/ユーザーについて緊急性の高い申告がある。
- credential悪用や想定外publishが監視で確認される。
- 管理者監査で、停止済み・失効済みであるべきSessionから送出が継続している。
- 法的要請やproviderからの明確な停止依頼を、所定の確認経路で受領する。

単なる通信障害、誤設定、通常の著作権問い合わせを「緊急停止」と混同せず、緊急性と対象を確認します。

## 影響判定

次の非秘密metadataを記録します。

- Session ID / user ID / Node ID
- 通報・検知時刻、確認者
- 現在のSession lifecycle / ingest / egress状態
- 停止理由の分類と緊急性
- 同じcredentialやresourceが他Sessionと共有されていないか
- 既に終了済みか、実際に送出が継続しているか

映像内容そのものを新たに録画・複製して調査材料にすることは、このrunbookの標準手順に含めません。

## 暫定対応

1. 対象Sessionを特定し、別Sessionでないことを再確認する。
2. 可能なら通常のowner/session-scoped stop経路を使い、desired stateをSTOPPEDへ遷移させる。
3. 停止だけでは再接続可能な場合、対象Sessionのingest credentialを失効する。
4. DIRECT_PUSHで不正送出が続く場合は、対象Sessionへのdestination secret配送を停止し、Node上のsession-scoped runtime secretを通常cleanup経路で破棄する。
5. Node Agent/Control Planeが不調で通常停止を確認できない場合は、対象process/sessionの隔離を運用判断として実施し、理由と操作を記録する。

全Node停止、volume削除、provider resource一括削除、共有secret rotationを最初の手段にしません。

## 停止確認

- Session desired/actual stateが停止側へ収束している。
- ingest credentialで新規publishできない。
- egress/relay接続が終了し、再接続loopが起きていない。
- provider inventoryに対象Session由来の不要resourceが残る場合は、authorityをread-only照合してから通常cleanup/reaperで回収する。
- 同一Node上の無関係Sessionが継続していることを確認する。

Control Plane表示だけで完了とせず、Media Node側のactual state / process / connectionの固定reason付き観測で確認します。

## 復旧・解除

誤通報や停止理由の解消後に再開する場合も、古いcredentialや旧Sessionをそのまま復活させません。

- 再開可否を別の運用判断として記録する。
- 必要なら新しいSession / credentialを発行し、停止対象との境界を明確にする。
- 旧credentialの失効を維持し、監査履歴を消さない。
- 補填や返金が必要な場合はbilling/entitlementの監査可能な手順へ接続する。

## 事後作業

- 検知から停止確認までの時間、対象、理由、操作主体、利用者影響を記録する。
- 誤停止だった場合も原因と解除判断を残す。
- credential再利用、停止競合、Node不調時の隔離、監査eventの回帰テストを追加する。
- 通報窓口、権限分離、MFA、管理者audit、データ保持方針に不足があれば #12 のfollow-up issueへ分離する。
