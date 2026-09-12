# Media Node provider selection

## 現在の方針

Phase B の Media Node は **ConoHa VPS を第一候補（baseline）** とする。

理由:

- 既存の ConoHa provider adapter と実機検証資産をそのまま利用できる。
- `1 Session = 1 VPS` のオンデマンド構成と時間課金の相性がよい。
- データ転送量に対する従量課金がなく、現行の 720p30 / 最大 6Mbps 程度の単一 Session ではネットワーク容量にも十分な余裕がある。
- Session 終了時に VPS / boot volume を削除する既存 lifecycle を維持できる。

この文書は ConoHa 固定を意味しない。provider 固有処理は adapter 境界に隔離し、Control Plane / Session lifecycle / DNS / cleanup は可能な限り provider-neutral に保つ。

## 将来候補: Akamai Cloud

Akamai Cloud は、将来トラフィックや同時 Session 数が増えた場合の追加 provider 候補とする。

特に次の状況で再評価する。

- ConoHa の共有回線または capacity が継続的な制約になる。
- 高 bitrate 化、同時 Session 増加、複数 Session 集約などでより大きなネットワーク容量が必要になる。
- Akamai の方が実測の RTT / jitter / packet loss / SRT retransmit や egress 品質で有意に優れる。
- multi-region や provider 冗長化が必要になる。
- Session 単価を含む総コストで Akamai が有利になる。

Akamai を現時点で本番 provider として実装することは Phase B の必須条件にしない。

## 将来の routing / provider selection

ユーザーに見せる ingest endpoint は provider が変わっても固定する。

```text
publisher
   |
   | RTMPS / SRT
   v
u-<opaque-id>.ingest.<service-domain>
   |
   | Control Plane が選択した Node へ DNS / routing
   v
+---------------------+
| Media Node provider |
|                     |
|  ConoHa  baseline   |
|  Akamai  scale-out  |
+---------------------+
```

将来の provider selection は単純な固定優先順位だけでなく、少なくとも以下を入力候補とする。

- provider / region ごとの capacity
- 予測・実測 egress
- ingest / egress bitrate
- CPU、frame drop、A/V sync
- RTT、jitter、packet loss、SRT retransmit
- Node 起動時間と provider API の失敗率
- Session 単位の実コスト
- destination / publisher からのネットワーク距離

閾値は現時点でハードコードせず、Phase B の実測を収集してから決める。

## 安全条件

provider routing を導入する場合も以下を維持する。

- 同一 Session の二重 Node 作成・二重課金を防ぐ idempotency / fencing
- Node READY 前に ingest endpoint を切り替えない
- Session 終了時の DNS park / resource cleanup
- provider 障害時にも orphan resource を回収する reaper
- Secret / credential を provider metadata、ログ、Issue に露出しない
- live 中の provider 間透過 migration は別設計とし、このTODOには含めない

## 関連

- ADR: `docs/adr/0002-phase-b-on-demand-media-node.md`
- 詳細設計: `docs/architecture/on-demand-media-node.md`
- Future TODO: #287
