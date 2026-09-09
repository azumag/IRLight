# Object storage 障害 runbook

Issue #11 の「object storage障害」に対する運用手順です。待機画像・生成済みvariant・upload/processing成果物をS3互換等のobject storageへ置く構成を対象にします。

## 原則

- storageが読めないことを「assetが存在しない」と解釈して削除や再作成を進めない。
- 既存Media Nodeに検証済みcacheがある場合は、勝手にpurgeせず継続利用可否を確認する。
- custom standby assetを取得できない場合に、入力映像へ自動復帰して秘匿性を下げない。安全な組込み共通素材へfallbackできる構成だけを使う。
- bucket名、署名URL、credential、object keyのsecret相当部分をissue/chat/通常ログへ貼らない。
- bucket削除、versioning変更、lifecycle変更、credential rotationはこのrunbookの自動手順に含めない。

## 検知

- upload URL発行、upload完了確認、processing worker、Media Node prefetchの失敗率が上昇。
- object GET/HEAD/PUTが継続timeoutまたは5xx。
- standby assetのchecksum検証失敗、取得不能、生成variant欠落が増加。
- provider status上のregion/bucket障害と時刻が一致。

単一objectの404、ユーザー削除済みasset、権限不備と、storage全体障害を分けて判定します。

## 影響判定

- 最初の失敗時刻と対象operation（GET/HEAD/PUT等）
- 新規upload / processing / prepare / 既存Sessionのどこまで影響するか
- cache済みassetで継続できているSession数
- fallback素材が利用可能か
- provider statusと直近deploy/config変更
- checksum不一致がavailability障害なのかdata integrity問題なのか

## 暫定対応

1. 新規uploadとasset processingを一時停止または明示失敗にする。
2. prepare時に必須assetが取得・checksum確認できない場合は、利用可能な組込み共通素材へ安全にfallbackできる契約だけを許可し、それ以外はprepareを失敗させる。
3. 既存Sessionのcacheを削除しない。cache identity/checksumを確認して利用を継続する。
4. object不存在と断定してDB/catalogからasset参照を削除しない。
5. retryは指数backoffと上限を設け、storage障害時にworker/node全体をretry stormにしない。

## 復旧

- HEAD/GET/PUTの代表操作を非秘密のtest objectで確認する。
- 既存objectのversion/checksum/metadataが期待値と一致することを確認する。
- processing backlogを冪等に再開し、同じvariantを競合生成しない。
- Node cacheの再取得はchecksum一致を確認して段階的に行う。
- fallback中のSessionは、元assetの復旧だけで即時切替せず、既存continuityの安全な切替条件に従う。

data integrity疑いが残る場合はavailability復旧だけで完了扱いにせず、versioning/backupからの復元判断へエスカレーションします。

## 事後作業

- 影響asset/Session、fallback利用、失敗operation、backlog量を記録する。
- storage timeout、retry、checksum、cache fallbackの回帰試験を追加する。
- lifecycle/versioning/backup設定を見直す場合は、既存objectを破壊しない変更計画を別途レビューする。
- ユーザー独自待機素材が使えなかった場合は、表示内容と補填要否を障害記録へ残す。
