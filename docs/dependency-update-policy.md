# Dependency update policy

IRLight の依存更新は、脆弱性監査と更新追随を分けて扱う。

## 自動追跡

`.github/dependabot.yml` で次を週1回追跡する。

- `apps/control-api/requirements.txt` の Python 依存
- `.github/workflows/` の GitHub Actions
- `apps/control-api/Dockerfile` の base image
- `apps/node-agent/Dockerfile` の base image

更新時刻は Asia/Tokyo の月曜朝に分散し、同時に大量の PR を作らないよう ecosystem ごとに open PR 数を制限する。

Dependabot は更新候補を PR にするだけで、自動マージはしない。通常の変更と同様に差分をレビューし、互換性・セキュリティ境界・テストへの影響を確認する。

## Merge gate

依存更新 PR も既存の merge gate を弱めない。

- `Dependency audit`
- `CI`（unit / Docker smoke を含む）
- Disconnect recovery E2E
- RTMPS ingest recovery E2E
- SRT ingest recovery E2E

必要な gate が green になり、必須レビュー指摘が解消されてから merge する。監査で advisory が検出された場合は ignore して green にすることを既定対応とせず、修正版への更新または影響範囲を確認した follow-up issue で扱う。

## Security boundary

更新確認のために本番 credential、外部配信先、クラウド課金を使用しない。依存更新によって TLS 検証、SSRF 防止、Secret 配送、認証・認可など既存の安全境界を暗黙に緩めない。

major version や挙動変更を伴う更新は、Dependabot PR が作成されたことだけを理由に採用しない。互換性や migration 方針に判断が必要なら PR を保留し、Issue に判断事項を分離する。

## Remaining supply-chain work

Issue #12 の supply-chain 項目のうち、SBOM、container image digest pinning、release artifact signing はこの設定では決めない。方式・運用コスト・release flow への影響を確認して別 slice として進める。
