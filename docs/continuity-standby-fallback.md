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

GStreamerへ渡す前のcheap guardとして、画像ヘッダは最大1 MiBだけ読み、PNG/JPEG/WebPの幅・高さを取得する。幅または高さが16,384 pxを超える画像、0 pxの寸法、総画素数が16 Mi pixelsを超える画像は利用不能としてfallbackする。これはNode-local handoffで極端なdimension宣言をそのままdecoderへ渡さないための追加防御であり、完全な画像decodeや展開後メモリ量の保証ではない。

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

Continuityは任意URLをfetchしない。`STANDBY_IMAGE_PATH`はtrusted Node側でprefetch済みのローカルregular fileのみを対象とし、安価なformat/size/dimension検査とsymlink・special-file拒否を追加防御として行う。この検査はNode側prefetchのtrust boundaryを置き換えるものではなく、GStreamerが画像をdecodeする時点までの完全なfilesystem transactionも提供しない。malformed imageの完全decode検証、decompression bombの展開量保証、checksum、object storage認証はIssue #7のAsset processing/prefetchで実施する。
