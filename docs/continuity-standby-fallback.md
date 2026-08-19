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

custom assetが欠損・空・上限超過・未対応formatの場合はNode defaultへ切り替える。Node defaultまで利用不能な場合だけsynthetic blackへ切り替える。

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

Continuityは任意URLをfetchしない。`STANDBY_IMAGE_PATH`はtrusted Node側でprefetch済みのローカルfileのみを対象とし、安価なmagic-byte/size検査を追加防御として行う。decompression bomb、MIME偽装、寸法上限、checksum、object storage認証はIssue #7のAsset processing/prefetchで実施する。
