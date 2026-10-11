# Control Plane Worker template

[Issue #629](https://github.com/azumag/IRLight/issues/629) の cf-first 雛形です。
既存の Python Control API / Compose を置換しません。認証、Access、Session authority、
DB、Workflows、DNS、provider、配信・メディア処理は実装していません。

## 境界

- `offline` と `staging` はどちらも検証用。Cloudflare account / zone、secret、resource binding、route、domain を設定しません
- `workersDev` / `previewUrls` / observability は無効。local dev は loopback に限定し、remote bindings、tunnel、inspector、永続local stateを無効にします
- mode は明示必須。Vite の既定 `development` / `production`、未指定、未知modeを拒否します。`local` は Vite の予約語なので使いません
- `GET /healthz` は雛形の生存だけを示します。`GET /readyz` は常に503 `CONTROL_PLANE_NOT_CONFIGURED`。業務readyの証明にはなりません
- その他のpathは404、既知endpointのGET以外は405。request dataやcredentialをresponse・logへ反映しません
- deploy用script、token、account IDを追加しません。dry-run成功は実deployやresource操作の承認を意味しません

本番・secret・authorityの境界は [cf-first契約](../../docs/architecture/cloudflare-cf-first.md) を参照してください。

## 固定toolchain

2026-10-11 UTC に npm registry とインストール済みCLIを照合した版です。
直接依存をexact pin、解決済み依存を `package-lock.json` へ記録します。

- Node.js `24.19.0`（`.node-version`、CIで同一版） / npm `11.9.0`
- `cf` `1.0.0-beta.14`
- `@cloudflare/vite-plugin` `2.0.0-beta.sha-91c870c02`（cf-native 2.0 beta）
- Vite `8.3.4` / TypeScript `7.0.2` / `@types/node` `24.19.2`
- Workers compatibility date `2026-10-01`

CLI / plugin はbetaです。更新はexact pinとlockfileを同時にreviewし、下記検証を再実行してください。
stable 1.x plugin は Wrangler をpeer dependencyに持つため、この雛形では採用しません。
現lockfileには Wrangler がありません。Node toolchain advisory は専用CIの `npm audit --audit-level=high` で検査します。
既存Python dependency audit、Docker CI、update policyは変更しません。npm用Dependabot設定は別途残件です。

## 開発・検証

リポジトリrootでcfを実行せず、専用ディレクトリで実行してください。下記の検証scriptにCloudflare loginは不要です。
account ID未定義は任意のcf API操作を禁止する仕組みではありません。直接resource/API commandを実行すると、
既存credential・環境変数・保存accountから対象が選択され得ます。この雛形の検証は下記の固定scriptに限定し、
account/zone API照会、login、実deploy、preview deploy、resource作成・変更は行いません。
installとauditはnpm registryを利用します。「API requestなし」はdry-runのCloudflare account APIに関する境界で、
すべての検証がOSレベルでネットワーク隔離されているという意味ではありません。

```sh
cd apps/control-plane
npm ci --ignore-scripts --no-fund
npm audit --audit-level=high
npm run typecheck
npm test
npm run test:local
npm run build
npm run dry-run
```

- `typecheck`: `cf workers types --mode staging` で `.cloudflare/types/index.d.ts` を生成後、`tsc --noEmit`
- `test`: mode拒否、resource-free設定、missing config/lock/dependencyのpreflight、HTTP handler、offline設定の契約テスト
- `test:local`: Vite plugin / workerd のlocal HTTP検査。CIでは90秒の上限を設定し、起動失敗をskipしません
- `build`: `cf build --mode staging`。独自typecheck scriptをcfが自動実行しないため、型検査は別に必ず実行します
- `dry-run`: `cf deploy --prebuilt --mode staging --dry-run`。同じmodeの `.cloudflare/output/v0/` を検査し、Cloudflare account API request / upload / resource作成を行いません。missing buildやmode不一致は停止します

すべてのcf scriptと `test:local` は共通の `scripts/run.mjs` を通し、継承したopt-inより優先して
`CF_SEND_TELEMETRY=false` / `DO_NOT_TRACK=1` を設定します。CLI telemetryを手動実行時も無効化します。
さらにMiniflare の `Request.cf` metadata取得を
`CLOUDFLARE_CF_FETCH_ENABLED=false` で無効化します。通常のMiniflareはcacheがない／古い場合に
`https://workers.cloudflare.com/cf.json` へGETしますが、この雛形は固定placeholderだけを使います。
この設定を外して検証を外部接続へ広げないでください。

```sh
npm run dev
```

開発serverは `cf dev --mode offline`、loopback `http://127.0.0.1:5173` で起動します。
portは `vite.config.ts` で変更してください。config、lockfile、installed cf / Vite / plugin / tscがない場合は
preflightがcfの自動設定より前に停止します。

## CIと残件

`.github/workflows/control-plane.yml` は独立jobです。PRとmain pushでlocked install、
audit、types、unit test、local runtime、build、prebuilt dry-runを実行します。
`contents: read`、SHA固定Action、checkout credential非永続化を維持し、Cloudflare secretは渡しません。
既存Python / Compose / Docker-heavy CIの条件・検証は変更しません。

templateのmergeは #629 全体の完了ではありません。Workflows / Access / authority / DNS / deployは
別sliceです。account・zone・環境別実Worker名、token権限、bindings、rollback、active Session境界を
決定・承認する前に、実環境へ接続しないでください。

## 公式根拠

- [cfのWorker雛形と要件](https://developers.cloudflare.com/cf/get-started/first-worker/)
- [typed configとmode](https://developers.cloudflare.com/cf/projects/cloudflare-config/)
- [build / prebuilt / dry-run](https://developers.cloudflare.com/cf/projects/)
- [CI、固定版、telemetry](https://developers.cloudflare.com/cf/ci/)
- [MiniflareのRequest.cfと取得無効化](https://developers.cloudflare.com/workers/testing/miniflare/get-started/)
