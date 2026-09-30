# Cloudflare cf-first 実装・運用契約

調査日: 2026-09-30 UTC  
対象: [Issue #629](https://github.com/azumag/IRLight/issues/629)  
参照コミット: `c69d51c2b9b506e2f3d41943f9aff334806f7eb3`  
対象範囲: 新規Cloudflare実装の標準化。既存runtimeの移植・本番操作は対象外。

## 結論

最初の変更は、既存アプリの置換ではなく、Cloudflare向けの新規実装契約と運用設計を文書化する小さなPRにする。現在の実装はPython / Docker Compose中心であり、Cloudflare Workerプロジェクトを変換するための既存manifestは確認できない。

`cf-first` は操作の入口と設定の標準を意味する。Workersへのランタイム移植、認証基盤切替、Session authorityの移行、ConoHa操作をまとめて実施する許可とは分ける。

## 調査範囲・重複確認

- mainの完全なGit tree（truncated=false）を確認。`package.json`、Node lockfile、`wrangler.*`、`cloudflare.config.ts`、`apps/control-plane/` はない
- 16本の `.github/workflows/*` を読取り、Cloudflare / Wrangler / cf の操作参照なし。現行CIはPython、Compose、Docker smoke、fault/recovery等
- 2026-09-30 06:31 UTCの調査時点で#629はopen、コメント0件。open PR一覧は0件。直近20件のclosed PRにも本件の実装は見当たらない
- #630は2026-09-30 06:17:32 UTCにmerge済み。本件の変更対象には含めない
- Cloudflareアカウント、実際のAccess設定、DNS zone、production token・権限、外部CI設定は未照会。以下はリポジトリの実装・設計棚卸しであり、稼働資産の存在・不存在を断定しない

## 現在・予定コンポーネント

| 領域 | リポジトリの証拠と現在地 | cf-firstで扱う範囲 |
|---|---|---|
| Control UI / API | `apps/control-api/app.py`はFastAPI。`auth_api.py`はcookie sessionとCSRFを実装。古いhandoffの「無認証」を現状全体へ適用しない | 将来Worker APIを追加する際の設定・開発・build経路。現行認証を無断でAccessへ置換しない |
| Lifecycle | `apps/control-api/session_workflow.py`のPython実装。provider抽象、checkpoint、cleanup lease、credential失効を保持 | Cloudflare WorkflowsはADRの候補。移植時は冪等性・補償・中断復帰を別sliceで実証 |
| Scheduled reaper | `deploy/systemd/irlight-reaper.service`がCompose内のPython CLIを起動 | Worker/Workflow化で独立reaperを失わない。最初のPRはsystemd運用を変更しない |
| Access | ADR 0002でWeb UI/API保護の候補。JWT署名・issuer・audienceとSession ownership検査を要求 | Accessを通過しただけで認可済みにしない。実アカウント設定は未確認 |
| DNS / RTMPS | ADR・詳細設計にopaque hostname、DNS-only、activate/park、外部TLS probeの契約 | cf経由のresource操作を設計。production zone/record IDとownershipを確定してから実装 |
| State / storage | 現行Control APIはローカルauthorityのreadinessを検査。詳細設計ではControl Plane DBの具体製品が未決定 | D1/KV/R2/DOをCLI移行だけで導入しない。consistency、復旧、ownershipを別ADRで決める |
| Media Node | ConoHa第一候補。Node Agent、MediaMTX、Continuity、egress、Composeが現在の資産 | Workersへメディア処理を移植しない。provider変更も本件に含めない |
| Deploy / rollback | `docs/production-deploy-preflight.md`と`docs/operations/deploy-rollback.md`にread-only検査、drain、immutable image、authority保全 | cf系deployにも同じ判断境界を追加。現行Compose手順を削除しない |

## 新規Cloudflare実装の標準契約

1. 新規Workerは専用ディレクトリで `cloudflare.config.ts`、Vite、Cloudflare Vite pluginを使う。rootのPythonアプリへ自動設定をかけない
2. 公式cfは現在beta。Node.js 22.18以上、ESMを前提に、採用するcf・Vite plugin・Nodeの具体版とlockfileをレビューして固定する。版番号は未検証のまま書き込まない
3. `cf cli search "目的"` → generated APIなら `cf schema ...`、project commandなら `--help` → 対象と副作用を確認する
4. API結果はJSONを機械処理する。ただし空レスポンス・raw contentを例外処理し、listの1ページを全件と誤認しない。stderrをJSONへ混ぜない
5. account / zone / mode / Worker名を明示する。mode未指定や未知modeがproductionを選ばない設計にする。環境ごとに完全な設定を返し、名前とbindingsを分離する
6. `cloudflare.config.ts`は実行可能なTSとして扱い、module評価中にクラウド操作、secret取得、外部fetchを行わない
7. build時の副作用を限定し、依存・設定がない状態でCIが自動補完しないよう、事前検査で停止する

出典: [cf導入](https://developers.cloudflare.com/cf/get-started/)、[設定](https://developers.cloudflare.com/cf/projects/cloudflare-config/)、[agent向け操作](https://developers.cloudflare.com/cf/agents/)

## 標準コマンド経路（設計案、未実行）

以降は専用プロジェクトの固定済みlocal cfを使う想定。

| 目的 | 経路 | 境界 |
|---|---|---|
| ローカル開発 | `cf dev --mode development` | production bindingを接続しない |
| 型生成 | `cf workers types --mode staging` | 生成型をtsconfigへ含める |
| 型検査 / build | プロジェクトtypecheckを明示実行後、`cf build --mode staging` | cfはpackage.jsonのbuild scriptの追加処理を自動実行しない |
| PR検査 | `cf deploy --prebuilt --mode staging --dry-run` | build済み成果物・同一mode、認証情報なし |
| 承認済みdeploy | `cf deploy --prebuilt --mode staging` / production | 別承認・対象アカウント・artifact commitを確認 |
| resource一覧・管理 | `cf cli search`、schema/helpで発見 | resource操作は原則remote。GET/更新/削除を分類し、paginationを完了する |
| observability設定 | Worker設定・redaction契約をレビュー | payload、Authorization、鍵やsecretを記録しない |
| live logs | 下記Wrangler例外 | cf nativeのtailが未対応 |

注意: `cf deploy --dry-run`はAPI uploadなしでも、設定がないディレクトリでは自動設定や依存追加を起こし得る。最初に設定とlockfileを人がレビューし、以後は `--prebuilt` と存在チェックで固定する。新規Workerがない現状では、このコマンド群をrootで実行しない。

出典: [develop/build/deploy](https://developers.cloudflare.com/cf/projects/)、[CI](https://developers.cloudflare.com/cf/ci/)

## Wrangler fallbackの最小範囲

公式referenceでは現時点でlive tailとsingle secret putが未対応。必要な時だけ、検証した版を一時実行し、対象Workerを明示する。下記は説明用の形であり、secret操作の実行手順・承認ではない。

- live tail: `npx wrangler@<検証済み版> tail <明示Worker名>`
- 単一secret設定: `npx wrangler@<検証済み版> secret put <SECRET名> --name <明示Worker名>`。値はチャット、引数、ログ、文書に含めない
- `cf deploy --secrets-file`はversion/deploymentと結びつく変更であり、単一secret操作の無害な代替として使わない
- Wranglerは `cloudflare.config.ts`を読まない。名前を省略して過去設定へ暗黙接続しない
- 例外記録: 対象操作、公式未対応根拠、固定版、owner、production影響、撤去条件、次回見直し日
- 撤去条件: pinned cfで同等機能が使え、stagingでtarget/secret/redactionの契約試験を通すこと
- 現在のIRLightには変換対象Wrangler設定なし。将来見つかった場合は `cf migrate` を別変更でpreview・レビューする。自動設定で既存bindingを無視させない

出典: [Wrangler対応表](https://developers.cloudflare.com/cf/wrangler/reference/)、[migration](https://developers.cloudflare.com/cf/wrangler/migrate/)

## CI・権限・production境界

- 最初のdocs PRはtoken不要。既存CIの `contents: read`、commit固定Action、Python/Compose検査を維持
- Worker追加PRでNode jobを独立追加。未信頼PRではcf config、build、依存scriptが実行されてもstagingを含むdeploy token・アプリsecretを一切渡さない
- 型検査・unit test・build・dry-runが成功した同一commit/artifactだけをdeploy候補とする。buildとdeployのmodeが一致しなければ停止
- deploy承認前にbindingの既存resource IDと不足resourceの作成計画を確認する。ID省略bindingはdeploy時に自動作成され得るため、意図しない課金resourceを作らない
- non-interactiveで破壊操作を拒否したcfは `Aborted.` をstderrへ出しexit 0になる場合がある。exit codeだけで変更成功とせず、結果とread-backを確認する。自動的に `--force` を補って再実行しない
- deploy jobは承認された環境専用tokenのみ。Cloudflare deploy token、DNS編集token、ConoHa credential、アプリsecretを分ける。権限の具体名は実装するAPIのschemaと公式token権限表で確認する
- secret作成・rotation、OAuth、権限拡大、DNS変更、provider resource作成/削除、配信開始/停止は本件準備に含めない
- 既存のSecret file/tmpfs、bootstrap一回限り、Node固有credential、credential失効後cleanup、Session ownership、clock/authority fail-closedを保持
- DNSはingest DNS-only、旧IPを残さない。READYは外部名前解決とTLS probe成功後。cf CLIでのAPI成功だけをREADY条件にしない
- 本番前はactive Session/drain、互換性、rollback対象、独立reaper、監査記録を確認。healthz成功と業務readyを分ける

## 実装順と残件

この文書とAGENTSの契約追加が最初のslice。#629全体の完了を意味しない。

1. 専用 `apps/control-plane/` にproduction接続なしの最小Worker/Vite雛形を追加。未知mode拒否、型検査、local test、build/dry-run CIを検証する
2. Workflows永続stepの採否と現在のlifecycle契約を照合。fake providerで再試行・中断・補償と独立reaperを実証する
3. Accessと現行cookie認証の関係、authorityデータの配置・整合性・復旧を決定する
4. DNS adapter、activate/park、外部probeをsandboxで検証する
5. アカウント資産・最小権限・rollbackとactive Session境界を確認してから、別承認でstaging、本番を進める

Node/Vite template、CIのCloudflare job、実環境のtoken権限確認は未実施。既存Python/Compose CIを弱めない。現行path classifierは `docs/` のみを軽量扱いするため、AGENTSやREADMEの変更ではDocker-heavy CIが実行される。

## 検証状況と未決定事項

実施済み: GitHub読取りによるtree・16 workflow・関連実装・設計・Issue/PR照合、2026-09-29更新のCloudflare公式資料確認。

未実施: cf/Vite pluginのinstall、CLI help/schemaの実環境照合、build、unit test、dry-run、token権限検査、Cloudflare稼働資産一覧、deploy。本書は未実施のチェックを成功扱いしない。

未決定: Cloudflare account/zone/domain、環境別Worker名、DB製品、Accessと現行cookie認証の関係、Workflow採用・料金/制限、cf/pluginの固定版、staging実証・production運用の責任者。これらが未決定でもdocs-onlyの最初のsliceは進められる。

## リポジトリ参照

- [AGENTS](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/AGENTS.md)
- [ADR 0002](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/docs/adr/0002-phase-b-on-demand-media-node.md)
- [詳細設計](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/docs/architecture/on-demand-media-node.md)
- [現在のControl API](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/apps/control-api/app.py)
- [現在のauth](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/apps/control-api/auth_api.py)
- [現在のlifecycle](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/apps/control-api/session_workflow.py)
- [現行CI](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/.github/workflows/ci.yml)
- [Deploy / rollback](https://github.com/azumag/IRLight/blob/c69d51c2b9b506e2f3d41943f9aff334806f7eb3/docs/operations/deploy-rollback.md)
