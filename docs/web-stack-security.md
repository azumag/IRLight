# Control API Web framework security baseline

Control API は `/` の `FileResponse` と `/assets` の `StaticFiles` を利用する。
FastAPI だけを固定すると、古い Starlette を許容する依存範囲へ戻る可能性があるため、
`apps/control-api/requirements.txt` では両方を明示的に固定する。

2026-09-08 の更新対象は FastAPI 0.141.1 / Starlette 1.6.0。
選択したリリースの Python 要件はともに 3.10 以上。
Starlette 1.x への移行を含む framework 更新であり、単一のpatch version変更ではない。
アプリ側の認可・CSRF・Session 状態機械・provider・配信ロジックは変更せず、
互換性は全既存テストとDocker / recovery E2Eで確認する。

## 修正の根拠

- GHSA-7f5h-v6xp-fcq8 / CVE-2025-62727:
  `FileResponse` の Range header 解析・併合の計算量に関する問題。
  Starlette 0.49.1 で修正。旧環境で解決された 0.47.3 は影響範囲に入る。
  https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8
- GHSA-86qp-5c8j-p5mr / CVE-2026-48710:
  不正な Host header により `request.url.path` が実際のルーティング path と
  異なる問題。Starlette 1.0.1 で修正。
  https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr

旧依存で Host header の3つの異常値による URL path の不一致を、外部接続なしの
ASGI テストで再現した。これは IRLight 本番の認可迂回や攻撃被害の実証ではない。
Range header による外部サーバーへの負荷試験も行っていない。

## 回帰確認

```sh
python -m pip install -r apps/control-api/requirements.txt
python -m pip check
python -m unittest discover -s tests -p test_web_stack_security.py -v
python -m unittest discover -s tests -v
```

追加テストは、インストールされた Starlette の既知修正版下限に加え、不正・正常な
Host、FileResponse の通常 GET / HEAD / single range / 範囲外要求と、過剰な複数範囲要求の全体応答への切替を確認する。
範囲外要求は 416 と `Content-Range: bytes */<length>` を返す。
静的ファイルの範囲取得を無効化して回避したものではない。

変更時には、最新 PR HEAD の Docker smoke suite と Disconnect / RTMPS / SRT
recovery E2E も成功させる。依存更新だけを理由に CI を省略しない。

既知の2件への対応と、すべての依存関係に脆弱性がないことは別である。
今後の advisory 追加への追随、自動依存監査、依存更新の定期運用は継続課題とする。
