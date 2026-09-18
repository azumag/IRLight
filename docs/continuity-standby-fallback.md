# Continuity standby fallback

Issue #4 / #7 の境界として、Continuity EngineはNode上に存在する待機画像を安全に選択し、取得失敗時にも出力を止めない。

## Selection order

1. `STANDBY_IMAGE_PATH`
   - Node Agent / Asset prefetchが配置した、検証済みローカル画像を想定する。
   - PNG / JPEG / WebPのみを受け付ける。
   - remote URLはContinuityへ直接渡さない。
   - Asset側に信頼できるchecksum metadataがある場合は、`STANDBY_IMAGE_SHA256`へ64文字のlowercase SHA-256、必要なら`STANDBY_IMAGE_SIZE_BYTES`へ正のdecimal byte sizeを渡す。sizeだけをintegrity proofとして使うことはできない。
2. `STANDBY_FALLBACK_IMAGE_PATH`
   - 既定値: `/opt/irlight/assets/default-standby.png`
   - Continuity imageへbuild時に同梱する共通素材。
3. synthetic black
   - custom/defaultの両方が利用不能でも、既存の`videotestsrc pattern=black`で出力を維持する最終fallback。

Custom画像の取得・cache・LRU・Sessionへのasset割当、checksumをowner/object/versionへ永続的に結び付けるauthorityはIssue #7の責務とする。このsliceは、Nodeへ配送済みのローカル画像を受け取る契約、任意のtrusted checksum metadataと実際にdecoderへ渡すsnapshotのbyte列を結び付ける検査、取得・整合性確認失敗時の安全なfallbackを定義する。

## Runtime behavior

選択した画像は`uridecodebin -> imagefreeze`で連続videoへ変換し、既存standby branchへ流す。音声fallbackは従来どおりsilence sourceを使用する。

custom assetが欠損・空・上限超過・未対応formatの場合はNode defaultへ切り替える。Node defaultまで利用不能な場合だけsynthetic blackへ切り替える。Continuityのローカルhandoff検査では、symlink・FIFO/deviceなどの非regular fileも利用不能として扱い、`lstat -> no-follow/non-blocking open -> fstat -> pathname再確認`で検査中のpath差し替えをfail-closedにする。

検査対象のregular fileは最大32 MiBという既存上限の範囲でprivate temporary fileへ全byteをcopyする。copy前後で元fdのdevice/inode/size/mtime/ctimeとpathnameのdevice/inodeを再確認し、copy中のpath差し替えや同一inodeへの書換えを検出した場合はその候補を利用不能としてfallbackする。copy完了後のsnapshotはmode `0400`でread-only fdとして開き直し、temporary pathnameをunlinkしてから選択結果として保持する。このため、selection完了後にNode側の元pathnameを別inodeへ差し替えたり、元inodeをin-place変更したりしてもdecoderが読むbyte列は変化しない。

`STANDBY_IMAGE_SHA256`が設定されている場合、checksum確認は元pathnameを再度openせず、このprivate snapshot fdそのものに対して行う。`pread`でfd offsetを動かさず全byteをboundedに読み、前後のdevice/inode/size/mtime/ctimeが同一であること、任意のexpected size、expected SHA-256を確認する。これにより「別pathを先にhashして、その後decoder用pathを開き直す」verify-then-reopenのTOCTOUを作らない。digest形式不正、size-only設定、size/digest不一致、snapshot読取異常はいずれもcustomを採用せずNode defaultへfallbackし、defaultも使えなければsynthetic blackを使う。checksum metadata未設定時は既存のtrusted Node-local handoffとの互換性を保つ。

GStreamerには元のpathnameや元inodeではなく、private snapshot fdの`/proc/self/fd/<n>`（利用可能なUnix環境では`/dev/fd/<n>`）を渡す。selection時点でこのfd aliasがsnapshotと同じdevice/inodeへ解決できることまで確認する。customのsnapshot/decoder handoffを構成できない場合はNode defaultを試し、defaultも構成不能ならsynthetic blackへ落とすため、`standby.json`の選択結果と実際にGStreamerへ渡せるsourceが起動時から食い違わない。descriptorが後から失効・再利用されidentityを確認できない場合も、元pathnameへ戻らずsynthetic blackへfail-closedする。

GStreamerへ渡す前のcheap guardとして、snapshot作成時に画像ヘッダは最大1 MiBだけmemoryへ保持し、PNG/JPEG/WebPの幅・高さと最低限のcontainer/header整合性を確認する。PNGは固定長IHDRのCRCとbit depth / color type / compression / filter / interlaceの組み合わせ、JPEGはSOFのsample precision・component数・segment長の整合、WebPはRIFF宣言サイズと実ファイルサイズ、および先頭chunkの境界を確認する。そのうえで幅または高さが16,384 pxを超える画像、0 pxの寸法、総画素数が16 Mi pixelsを超える画像は利用不能としてfallbackする。これはNode-local handoffで明らかに壊れたheaderや極端なdimension宣言をdecoderへ渡さないための追加防御であり、完全な画像decode、全chunkの検証、展開後メモリ量の保証ではない。

## Image packaging contract

Continuity imageのDockerfileはPython sourceを明示的に`/app`へcopyする。source tree上のunit testだけが成功しても、`runner.py`またはbuild時にdefault画像を生成する`make_default_standby.py`から新しいlocal moduleをimportし、そのmoduleをimageへ入れ忘れるとcontainer内では起動・buildできない。

`tests/test_continuity_dockerfile_packaging.py`は、この2つのentrypointから辿れるstatic local import closureが実際に`/app`へcopyされることを検査する。別directoryへのCOPYは契約を満たしたものとして扱わない。dynamic importはこの検査対象外なので、導入する場合はimage-level smokeも合わせて追加する。

## Diagnostics

`/state/standby.json`には次の安全な情報だけを書く。

- `source`: `CUSTOM | NODE_DEFAULT | SYNTHETIC_BLACK`
- `fallback_reason`
- `custom_configured`
- `selected_at`

ローカルfilesystem path、ファイル名、署名URL、asset ID、expected/actual checksumなどはstatus/logへ保存しない。

主なreason:

- `ASSET_UNAVAILABLE`: customが使えずNode defaultへfallback
- `ASSET_INTEGRITY_CHECK_FAILED`: customの設定済みintegrity metadataとsnapshotが一致せずNode defaultへfallback
- `ASSET_INTEGRITY_CHECK_FAILED_AND_NODE_DEFAULT_UNAVAILABLE`: customのintegrity確認に失敗し、Node defaultも使えずsynthetic black
- `ASSET_AND_NODE_DEFAULT_UNAVAILABLE`: custom/defaultとも使えずsynthetic black
- `NODE_DEFAULT_UNAVAILABLE`: custom未指定かつNode defaultが使えずsynthetic black

## Security boundary

Continuityは任意URLをfetchしない。`STANDBY_IMAGE_PATH`はtrusted Node側でprefetch済みのローカルregular fileのみを対象とし、安価なformat/size/header-integrity/dimension検査、symlink・special-file拒否、copy中のsource安定性確認、private unlinked snapshotへのdecoder handoff固定を追加防御として行う。trusted expected SHA-256が渡された場合は、そのexact snapshot byte列との一致もdecoder handoff前に確認する。

この検査はNode側prefetchのtrust boundaryやAsset authorityを置き換えるものではない。expected digest自体のowner/object/version binding、署名済み・認証済みobject storage取得、processing workerでの完全decode/decompression-memory制限、metadata除去、durable READY state、cache/LRU、参照安全な削除は引き続きIssue #7のAsset processing/prefetchで実施する。
