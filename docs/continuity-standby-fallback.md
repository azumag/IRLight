# Continuity standby fallback

Issue #4 / #7 の境界として、Continuity EngineはNode上に存在する待機画像を安全に選択し、取得失敗時にも出力を止めない。

## Selection order

1. `STANDBY_IMAGE_PATH`
   - Node Agent / Asset prefetchが配置した、検証済みローカル画像を想定する。
   - PNG / JPEG / WebPのみを受け付ける。
   - remote URLはContinuityへ直接渡さない。
2. `STANDBY_FALLBACK_IMAGE_PATH`
   - 既定値: `/opt/irlight/assets/default-standby.png`
   - Continuity imageへbuild時に同梱する共通素材。
3. synthetic black
   - custom/defaultの両方が利用不能でも、既存の`videotestsrc pattern=black`で出力を維持する最終fallback。

Custom画像の取得・checksum検証・cache・LRU・Sessionへのasset割当はIssue #7の責務とする。このsliceは、Nodeへ配送済みのローカル画像を受け取る契約と、取得失敗時の安全なfallbackだけを定義する。

## Runtime behavior

選択した画像は`uridecodebin -> imagefreeze`で連続videoへ変換し、既存standby branchへ流す。音声fallbackは従来どおりsilence sourceを使用する。

custom assetが欠損・空・上限超過・未対応formatの場合はNode defaultへ切り替える。Node defaultまで利用不能な場合だけsynthetic blackへ切り替える。Continuityのローカルhandoff検査では、symlink・FIFO/deviceなどの非regular fileも利用不能として扱い、`lstat -> no-follow/non-blocking open -> fstat -> pathname再確認`で検査中のpath差し替えをfail-closedにする。

検査に成功した画像は、その時点で開いたregular-file descriptorをContinuity pipeline終了まで保持する。GStreamerには元のpathnameではなく`/proc/self/fd/<n>`（利用可能なUnix環境では`/dev/fd/<n>`）を渡すため、検査後に元pathnameが別inodeへunlink/recreate・symlink差し替えされても、decoderは検査済みinodeから読み込む。さらに selection 時点でこのfd aliasが同じdevice/inodeへ解決できることまで確認する。customのdecoder handoffを構成できない場合はNode defaultを試し、defaultも構成不能ならsynthetic blackへ落とすため、`standby.json`の選択結果と実際にGStreamerへ渡せるsourceが起動時から食い違わない。descriptorが後から失効・再利用されidentityを確認できない場合も、元pathnameへ戻らずsynthetic blackへfail-closedする。

GStreamerへ渡す前のcheap guardとして、画像ヘッダは最大1 MiBだけ読み、PNG/JPEG/WebPの幅・高さと最低限のcontainer/header整合性を確認する。PNGは固定長IHDRのCRCとbit depth / color type / compression / filter / interlaceの組み合わせ、JPEGはSOFのsample precision・component数・segment長の整合、WebPはRIFF宣言サイズと実ファイルサイズ、および先頭chunkの境界を確認する。そのうえで幅または高さが16,384 pxを超える画像、0 pxの寸法、総画素数が16 Mi pixelsを超える画像は利用不能としてfallbackする。これはNode-local handoffで明らかに壊れたheaderや極端なdimension宣言をdecoderへ渡さないための追加防御であり、完全な画像decode、全chunkの検証、展開後メモリ量の保証ではない。

## Diagnostics

`/state/standby.json`には次の安全な情報だけを書く。

- `source`: `CUSTOM | NODE_DEFAULT | SYNTHETIC_BLACK`
- `fallback_reason`
- `custom_configured`
- `selected_at`

ローカルfilesystem path、ファイル名、署名URL、asset IDなどはstatus/logへ保存しない。

主なreason:

- `ASSET_UNAVAILABLE`: customが使えずNode defaultへfallback
- `ASSET_AND_NODE_DEFAULT_UNAVAILABLE`: custom/defaultとも使えずsynthetic black
- `NODE_DEFAULT_UNAVAILABLE`: custom未指定かつNode defaultが使えずsynthetic black

## Security boundary

Continuityは任意URLをfetchしない。`STANDBY_IMAGE_PATH`はtrusted Node側でprefetch済みのローカルregular fileのみを対象とし、安価なformat/size/header-integrity/dimension検査、symlink・special-file拒否、検査済みinodeへのdecoder handoff固定を追加防御として行う。この検査はNode側prefetchのtrust boundaryを置き換えるものではない。同じinodeの内容を別writerが検査後にin-place変更するケース、malformed imageの完全decode検証、decompression bombの展開量保証、checksum、object storage認証はIssue #7のAsset processing/prefetchで実施する。
