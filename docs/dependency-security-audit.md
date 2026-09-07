# Python dependency security audit

IRLight の Control API は `apps/control-api/Dockerfile` と同じ Python 3.13 系で依存関係を検証する。

## CI checks

`.github/workflows/dependency-audit.yml` は pull request、日次スケジュール、手動実行で次の二つを別ジョブとして確認する。

- `dependency-consistency`: runtime requirements をインストールし、`python -m pip check` で依存関係の不整合を検出する。
- `dependency-vulnerability-audit`: `pip-audit==2.10.1` で既知脆弱性を監査する。requirements の依存解決を無効化しないため、明示 pin だけでなく transitive dependencies も対象になる。

脆弱性監査は `--strict` を使用する。依存収集に失敗した場合を成功扱いにしない。通常の runtime requirements が既知脆弱性を含む場合もジョブを失敗させる。

## Audit contract fixtures

監査経路そのものが壊れていないことを確認するため、CI は次の fixture も検査する。

- `tests/fixtures/dependency-audit/known-vulnerable.txt`: 既知 advisory を持つ Starlette 0.47.3。`pip-audit` が exit code 1 を返し、JSON report に Starlette 0.47.3 の vulnerability が少なくとも1件含まれることを要求する。feed 取得失敗や壊れた応答で report が作れない場合も成功扱いにしない。
- `tests/fixtures/dependency-audit/patched.txt`: 修正版 Starlette 1.6.0。既知脆弱性なしで audit が成功することを要求する。

fixture は runtime へインストールするためのものではない。

## Exceptions

ベースラインでは `--ignore-vuln` を使用しない。将来例外が必要になった場合は、少なくとも advisory ID、適用理由、追跡 Issue、失効日を明記したうえで review を通す。無期限 ignore や監査ジョブ全体の skip は行わない。

監査ツールの更新も通常の dependency/security PR として扱い、CI を迂回して main へ直接変更しない。
