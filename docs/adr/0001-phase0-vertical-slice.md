# ADR 0001: Phase 0は再エンコード型の縦切りPoCから開始する

- Status: Proposed / PoC only
- Date: 2026-08-16
- Related: #2, #4, #15, #16

## Context

IRLightの本番方針は、通常時のH.264映像を可能な限り再エンコードせず中継することである。一方、最初に確認すべき価値は次の一連の挙動である。

1. 入力がなくても出力接続を開始し、待機映像と連続した音声トラックを送る
2. 入力映像が安定したら通常映像へ切り替える
3. 入力が消えたら同じ出力パイプライン内で待機映像へ戻る
4. Web UIから、映像を止めず音声だけを無音化する
5. 入力再接続やプロセス再起動後もdesired stateへ収束する

圧縮済みH.264/AACを切り替えながらPTS/DTS・codec設定・GOPを連続させる方式は、最終的な低原価構成として有望だが、最初の機能検証としては不確実性が高い。

## Decision

最初の縦切りPoCはGStreamerで映像・音声をrawへ展開し、待機／通常／無音を`input-selector`で切り替えた後、H.264/AACへ再エンコードする。

このPoCの実行profileは `COMPOSITED_VIDEO_POC` とし、本番の `PASSTHROUGH` または `AUDIO_PROCESSED` と同一視しない。

Web UIの命令は一回限りのtoggleではなく、`audio_mode=LIVE|MUTED`というdesired stateとして共有ボリュームへ保存する。Continuity Engineはactual pipelineを定期的にreconcileする。

## Consequences

### Positive

- 待機画面、自動復帰、音声ミュートを一つの実行可能な構成で検証できる
- 音声トラックを消さず、無音AACを連続送出できる
- 将来の時刻・現在地・通信指標オーバーレイを試す境界が明確になる
- desired/actualモデルとモバイルUIを早い段階で試せる

### Negative

- 映像を再エンコードするためCPU、追加遅延、画質劣化が発生する
- このPoCのNode capacityや原価は本番passthrough構成の見積もりに使えない
- 入力profileを720p30へ正規化するため、本番の幅広い互換性をまだ証明しない
- 出力切断時の高度な再接続、認証、複数Sessionは未実装である

## Follow-up experiment

#2/#15で次を比較し、別ADRで本番方式を決める。

1. H.264 passthrough + 音声のみdecode/encode
2. 圧縮済みH.264/AACと事前生成素材のtimestamp整合切替
3. 現在の再エンコード型baseline

比較指標は、切替成功率、A/V sync、追加遅延、CPU/GPU、1時間当たり原価、Twitch/YouTube側での配信枠維持とする。
