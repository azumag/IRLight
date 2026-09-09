# Billing webhook 停止 / backlog runbook

Issue #11 の「Stripe webhook停止」に対する運用手順です。課金実装が有効な環境で、決済providerからのwebhook受信・検証・保存・Entitlement反映が止まった場合を対象にします。

## 原則

- webhook未着・処理失敗を「未払い」と即断しない。provider上の決済結果とIRLight内部状態を分けて扱う。
- 逆に、providerでの成功確認なしに手動でpaid/Entitlementを付与しない。
- 同じeventの再送で二重Entitlement、二重補填、二重usage確定を起こさない。
- webhook signing secret、payment token、顧客の決済情報をissue/chat/通常ログへ載せない。
- provider側の返金、capture、subscription変更など金銭を動かす操作は、このrunbookから自動実行しない。

## 検知

- webhook受信数が通常値から急減または0になる。
- signature verification failure、5xx、timeoutが増加。
- provider側deliveryがpending/retryingで増加。
- webhook inbox / outbox / retry queueのbacklogが単調増加。
- 決済完了後もEntitlement反映が遅延する問い合わせが増える。

## 影響判定

- 最初の失敗時刻、最後に正常処理したevent時刻
- 受信失敗か、署名検証か、保存か、consumer処理か
- backlog件数と最古event age
- Event Pass / Subscription / refund等、影響event種別
- 既存Entitlement利用と新規購入のどちらに影響するか
- provider statusと直近deploy/config変更

生payload全体を保存・共有せず、event ID、event type、固定reason code、処理時刻など必要最小限のmetadataで追跡します。

## 暫定対応

1. webhook処理の整合性が確認できない間は、新規購入後の即時利用を「確認中」として扱い、推測でEntitlementを発行しない。
2. 既に有効なEntitlementをwebhook障害だけで即時失効しない。
3. queue/backlogは捨てず、event IDによる重複排除情報を維持する。
4. signature verificationを無効化して復旧扱いにしない。
5. retry stormを避けるため、consumerの再試行にはbackoffと同時実行上限を設ける。

利用者が配信直前で影響を受ける場合の一時補填は、監査理由・期限・対象を明記した管理者判断として別途行い、決済成功の代替事実にはしません。

## 復旧

- webhook endpointの到達性、TLS、署名検証、永続化、consumer処理を段階別に確認する。
- providerから再送されたevent、既存backlog、手動retryが混在してもevent IDで一度だけ処理されることを確認する。
- Entitlement / Payment / Subscriptionの内部状態をproviderの機械可読な事実と照合する。
- backlogを古い順に有界なworker数で処理し、エラー率と最古event ageが継続低下することを確認する。
- 新規購入フローを再開する前に、重複発行が起きない回帰確認を行う。

## 事後作業

- 影響event数、Entitlement反映遅延、ユーザー影響、補填有無を記録する。
- webhook重複、順序逆転、signature failure、queue停止、provider retryのfixtureを追加する。
- alert閾値は単なる瞬間失敗率だけでなくbacklog ageを含めて見直す。
- 課金または補填に不整合が残った場合は、監査可能な個別reconcileを行い、一括推測更新はしない。
