# IRLight threat model

[Issue #12](https://github.com/azumag/IRLight/issues/12) の11脅威を、既存統制・根拠・回帰テスト・残ギャップへ対応付ける文書です。[2026-10-08 の監査](https://github.com/azumag/IRLight/issues/12#issuecomment-6057892683) を出発点に、main [`499c2cc8adac40f8d078831386e2d6a42d2eb7ef`](https://github.com/azumag/IRLight/commit/499c2cc8adac40f8d078831386e2d6a42d2eb7ef)（2026-10-09）をコードで再確認しました。以後の相対リンクは現在の checkout の根拠を指します。

これは部分成果です。Issue #12 全体は open を維持し、有料βの Go/No-Go、production の安全性や実運用の受入完了を宣言しません。Phase 0 の技術 PoC と Phase B の個別実装を、公開環境に必要な統制の完成と混同しません。

## 対象資産

| 資産 | 守る性質と保管・処理境界 |
| --- | --- |
| Destination stream key / SRT passphrase、master key | 機密性。Control Plane の暗号文と復号鍵、Session に割当てる Node の runtime secret を分離する。authenticated SRT destination probe は未対応 |
| ingest / relay / internal media credential、bootstrap / heartbeat / admin Bearer、auth cookie / CSRF | 機密性・限定 scope・失効。publisher の資格情報と内部機械の資格情報を取り違えない |
| ユーザー登録、Destination / Asset / Session / Node / entitlement authority | 完全性・owner 境界。壊れた永続 state を空に初期化して認可・失効・利用枠を解除しない |
| ライブ映像・音声、待機素材、upload storage の source / variant | 利用者の内容と権利、完全性・可用性。標準で映像を録画しない方針を維持し、デバッグ目的の無断保存を追加しない |
| CPU / memory / process / FD / 帯域、VPS・storage、無料枠・決済 | 可用性・課金の完全性。入力・検証・配信・素材処理が他 Session や運用基盤を枯渇させない |
| event / log / monitoring / CI artifact、依存 image / package / release | 秘密非露出・監査可能性・実行物の完全性。短期 CI artifact と incident / retention policy を区別する |

## 攻撃者の前提

- 未認証のインターネット利用者は、到達可能な register/login と公開 ingest を反復試行できる。機械用 internal API を自由に呼べる前提にはしない。そのネットワーク防護は別途必要である。
- 認証済みの悪意ある owner は、自分の Destination URL、upload の申告メタデータ、メディア入力を選べる。他 owner の state や secret の正当な閲覧権限は持たない。owner 境界を実装・テストの対象にする。
- 正しい ingest credential を共有された publisher、攻撃者管理の destination / DNS、悪意ある codec/container は、認証成功後でも非信頼入力を送り、DNS 応答や接続のタイミングを変えられる。
- 侵害された Media Node、過大権限の管理者、依存物の供給者は、与えられた機械・復号・実行権限の範囲で攻撃できる。root / Docker daemon 侵害後に同一 host の tmpfs や process memory の秘密を守れるとは仮定しない。
- state の破損・不正復元、ローカル file の差替えも検査する。ただし通常 API の利用だけで任意の persisted state を書けるという主張ではない。公開コードと合成テストの所見であり、本番侵入・実秘密の漏えい観測ではない。

## 信頼境界

| 境界 | 渡るもの・既存の防護 | 残る前提 |
| --- | --- | --- |
| user browser → Control Plane | auth cookie / CSRF、owner-scoped API、非秘密 catalog と別 secret store。[auth API](../apps/control-api/auth_api.py)、[secret delivery](egress-secret-delivery.md) | production TLS、ネットワーク露出、管理者 RBAC/MFA は別の受入事項 |
| publisher → Media Node (MediaMTX) → Node Agent → Control Plane | RTMP/RTMPS/SRT と ingest credential。Session / protocol / expiry / stop を検査し、`overridePublisher: false` で既存 publisher の追い出しを防ぐ。[ingest auth](ingest-authentication.md)、[MediaMTX config](../config/mediamtx.yml) | auth hook の `ip` は機械が申告する値。internal endpoint の private network / mTLS、実機互換性、L3/L4 防御を別途検証する |
| Control Plane → Node Agent → media processes | 一回限り bootstrap、Node 固有 heartbeat と list/stop 用 admin Bearer、割当 Session の egress URL。0600 tmpfs と process ごとの read-only secret mount。[bootstrap authority](node-agent-bootstrap-control-authority.md)、[Compose](../docker-compose.node.yml) | TLS / machine-network protection、Node 侵害時の fencing・権限縮小。bootstrap を「自動 TTL 付き短期 secret lease 完成」とは扱わない |
| Continuity → local relay → Egress Gateway → destination | Continuity は外部配信先に直接接続せず relay を維持。Gateway が各 attempt の DNS を再検査。[egress reconnect](egress-reconnect.md)、[runtime guard](../apps/egress-gateway/destination_guard.py) | destination と DNS は非信頼。sink の再 DNS lookup による TOCTOU、proxy / port policy、実プラットフォーム互換性は残る |
| owner → Control Plane upload intent → upload storage → processing worker → Node cache | server-generated owner-bound key と単回 completion の**メタデータ境界のみ**。[upload intent](standby-asset-upload-intent.md)、[local checksum primitive](standby-asset-checksum.md) | upload storage は別の認証・バイト・保持境界。presigned URL、実オブジェクトのバイト検査、processing worker / transcode / prefetch / cache / 物理削除は未実装。図式上の handoff を既存接続と解釈しない |
| runtime → logs / monitoring / CI、maintainer → dependency / release | allowlist / fixed reason、read-only redaction inspector、pin と audit / SBOM。[log audit](operations/log-redaction-audit.md)、[dependency audit](dependency-security-audit.md) | inspector は全自由文を保証しない。container / OS / codec 全体の監査と release signing は別の対象 |

Node Agent は Docker socket を mount し、stack を制御します。socket の `:ro` mount は Docker API を read-only にする認可機構ではありません。media network は egress のため `internal: false` であり、network namespace の定義だけで接続先 allowlist が完成したとは扱いません。

## 状態の読み方

各脅威は統制ごとの状態を記します。部分実装のある脅威を「解消済み」にまとめません。

- **実装済み**: 上記 main に対象のコード / 設定 / 手順が存在する。手順だけの実装は runtime enforcement と区別する。
- **CI 固定済み**: 実在する回帰テストが [unit discovery](../.github/workflows/ci.yml) または指定 workflow に組み込まれている。ここでの意味は契約が CI に載っていることで、特定 run の green を表すものではない。PR の exact HEAD の CI 結果は PR 側で確認する。
- **実環境未確認**: 本文のコード・CI 根拠だけでは production の設定、実配信先、負荷、運用や侵害後の挙動を確認できない。
- **未実装 / 未決**: テストや運用判断を含め残ギャップ。方式・閾値・権限・法務を本書で決めない。

## 11脅威と統制・回帰テスト

### T01: 出力先stream keyの漏えい

- 状態: 暗号化・owner 分離・限定配送は実装済み、下記は CI 固定済み。鍵の運用と production の秘密境界は実環境未確認。
- 既存統制・根拠: [Destination secret store](../apps/control-api/destination_secret_store.py) は Fernet ciphertext と owner-bound record を保存し、通常 API は全文を返さない。[bootstrap 解決](../apps/control-api/node_internal.py) は割当 Session の secret を解決し、[delivery 契約](egress-secret-delivery.md) と [runtime file 境界](egress-runtime-secret-file-boundary.md) に沿って tmpfs へ配送する。
- 回帰テスト: [test_destination_secrets.py](../tests/test_destination_secrets.py) — `DestinationSecretStoreTest.test_secret_is_encrypted_at_rest_and_resolves` と `DestinationSecretStoreTest.test_same_ref_is_isolated_by_user`（暗号化・owner 分離）。[test_session_destination.py](../tests/test_session_destination.py) — `BootstrapEgressResolutionTest.test_assigned_session_resolves_secret_only_for_bootstrap`（割当境界）。
- 残ギャップ: KMS / managed secret / envelope encryption、key version と master-key rotation、復号権限の限定・監査、配送 secret の TTL / 再配送 / 失効は未決・未実装部分がある。単一 Fernet key を KMS 完成扱いにしない。UI・TLS・実 Node の漏えい防止は別途受入が必要。

### T02: 入力資格情報の総当たり・共有・再利用

- 状態: 発行・hash 保存・protocol / expiry / rotation / stop 失効と ingest abuse guard は実装済み、CI 固定済み。機械境界と実 publisher は実環境未確認。
- 既存統制・根拠: [ingest store](../apps/control-api/ingest_store.py) の32-byte random secret は一度だけ返し SHA-256 のみ永続化。[ingest auth](ingest-authentication.md) と [guard](../apps/control-api/ingest_auth_guard.py) は IP / credential の失敗を bounded に追跡する。[cache](node-local-ingest-auth-cache.md) は既に成功した publisher に限る outage fallback を持つ。
- 回帰テスト: [test_ingest_credentials.py](../tests/test_ingest_credentials.py) — `IngestCredentialStoreTest.test_rotation_revokes_old_secret` と `IngestCredentialStoreTest.test_protocol_expiry_and_revoke_are_enforced`。[test_ingest_auth_guard.py](../tests/test_ingest_auth_guard.py) — `IngestAuthGuardTest.test_ip_lockout_catches_credential_spray`（spray 拒否）。
- 残ギャップ: 有効な credential の共有を本人性で防ぐ仕組みは未実装。internal auth endpoint の到達制限・mTLS、cache 使用中の失効反映と実機再接続の受入、分散 IP の頻度 / 誤検知解除 / edge 防御は別途判断。auth hook の source IP と user login の trusted proxy policy を同じ保証としない。

### T03: Custom RTMP URLを用いたSSRF・内部ネットワーク探索

- 状態: URL safety、verify 時の public IP 検査・接続先 pin、runtime DNS guard は実装済み、CI 固定済み。実 publish の完全な transport-IP pinning と proxy policy は未実装 / 未決、実環境未確認。
- 既存統制・根拠: [URL safety](../apps/control-api/destination_url_safety.py) は userinfo と credential-bearing / 曖昧な SRT query を拒否。[probe](../apps/control-api/destination_probe.py) は IPv4/IPv6 の非 global address を拒否し検査済み sockaddr / IP literal へ接続、RTMPS hostname 検証を行う。[runtime guard](../apps/egress-gateway/destination_guard.py) は各 attempt で非 public を含む answer set と optional verified-peer の drift を拒否。[verification](destination-verification.md)、[egress の残余リスク](egress-reconnect.md)、[peer-file 境界](egress-verified-peer-file-boundary.md) を参照。
- 回帰テスト: [test_destination_url_safety.py](../tests/test_destination_url_safety.py)（URL policy）。[test_egress_destination_guard.py](../tests/test_egress_destination_guard.py) — `DestinationGuardTest.test_rejects_cloud_metadata_link_local_address` と `DestinationGuardTest.test_rejects_mixed_public_and_private_answers` と `DestinationGuardTest.test_rejects_dns_drift_after_verification`。
- 残ギャップ: Gateway guard 後に sink が再 DNS lookup する TOCTOU は残る。peer file は欠損時 optional であり、接続時 peer 一致の完全保証ではない。port allowlist、proxy 環境変数の排除、redirect の一貫した禁止と実 connector の検証は別の設計 / テスト対象。private-target override は明示 PoC / self-hosted 用で、production 防御の根拠にはしない。

### T04: 不正なメディア入力によるprocess crash・DoS

- 状態: 入力 policy、外部 egress 障害の分離、bounded attempt supervisor は実装済み、CI 固定済み。legacy process isolation は opt-in canary、実環境未確認。
- 既存統制・根拠: [ingest policy](../apps/node-agent/ingest_policy.py) が codec / track / resolution / bitrate を検査。[Compose](../docker-compose.node.yml) の Continuity / Gateway / Node Agent は `cap_drop: [ALL]` と `no-new-privileges` を使用。[supervisor](../apps/egress-gateway/attempt_supervisor.py) は reap を確認し、[isolated attempt](../apps/egress-gateway/isolated_attempt.py) は sanitized IPC と旧 child fencing を持つ。[process-isolation 契約](egress-attempt-process-isolation.md) と [#545](https://github.com/azumag/IRLight/issues/545) を参照。
- 回帰テスト: [test_ingest_policy.py](../tests/test_ingest_policy.py) — `IngestPolicyTest.test_h265_is_rejected_and_rtmp_source_is_kicked`。[test_egress_attempt_supervisor.py](../tests/test_egress_attempt_supervisor.py) — `ChildReapingContractTest.test_unreapable_child_fails_closed`。[test_egress_isolated_attempt.py](../tests/test_egress_isolated_attempt.py) — `IsolatedAttemptContractTest.test_canary_is_explicit_and_never_wraps_rtmp2sink`。
- 残ギャップ: `EGRESS_LEGACY_PROCESS_ISOLATION_CANARY` の既定は `0`。#545 の canary を全 Node の既定 isolation 完成扱いにしない。root filesystem 全体の read-only、非 root 実行、CPU / memory / PID / file / time limit、明示 seccomp/AppArmor profile と egress network 制限、malicious codec corpus / fuzz、upload worker isolation は未実装・未確認部分がある。Docker の既定 profile を専用 sandbox の検証済み証拠としない。

### T05: 巨大画像・動画によるresource exhaustion

- 状態: Node-local standby の byte / header / dimension / pixel / snapshot 境界と upload completion の申告値検査は実装済み、CI 固定済み。実 storage / decode worker は未実装、実環境未確認。
- 既存統制・根拠: [standby asset](../apps/continuity/standby_asset.py) は stable regular file の bounded private snapshot と画像 header を検査。[upload intent](../apps/control-api/asset_upload_intent.py) と [catalog API](../apps/control-api/catalog_api.py) は owner-bound key、MIME、正の strict integer size、digest 形式、単回 transition を検査。[#646](https://github.com/azumag/IRLight/pull/646) / [#647](https://github.com/azumag/IRLight/pull/647) と [upload 契約](standby-asset-upload-intent.md)、[checksum primitive](standby-asset-checksum.md) を参照。
- 回帰テスト: [test_standby_asset.py](../tests/test_standby_asset.py) — `StandbyAssetTest.test_png_pixel_budget_falls_back_to_node_default`。[test_asset_upload_intent.py](../tests/test_asset_upload_intent.py) — `AssetUploadIntentHttpTest.test_non_integer_json_sizes_are_rejected_before_store` と `AssetUploadIntentStoreTest.test_completion_rejects_foreign_object_key`。
- 残ギャップ: #646/#647 はメタデータ境界のみ。`PROCESSING` は worker 完了や安全な素材の証明ではない。client 申告 size / MIME / sha256 を実オブジェクトのバイト検査で照合していない。presigned URL、full decode / decompression budget、動画 transcode、job lease / retry、storage quota、worker isolation、Node prefetch / cache / LRU / reference-safe deletion は未実装。legacy create_asset も metadata-only で任意 key に書く authority を付与しない。

### T06: 無料枠・イベントパスの大量取得

- 状態: password KDF の同時実行 admission、per-user concurrent Session reservation と期限は実装済み、CI 固定済み。大量アカウント・取得頻度対策は未実装部分があり実環境未確認。
- 既存統制・根拠: [KDF admission](../apps/control-api/auth_kdf_admission.py) は同じ filesystem の worker の expensive compute を bound する。[entitlement store](../apps/control-api/entitlement_store.py) と [Session store](../apps/control-api/session_store.py) は per-user 利用枠と reservation を扱う。[KDF 運用境界](operations/auth-kdf-admission.md)、[Session capacity](operations/session-capacity-exhaustion.md) を参照。
- 回帰テスト: [test_auth_kdf_admission.py](../tests/test_auth_kdf_admission.py)（同時 compute と file boundary）。[test_entitlement.py](../tests/test_entitlement.py) — `ConcurrentSessionLimitTest.test_reservation_itself_consumes_slot` と `ConcurrentSessionLimitTest.test_limits_are_per_user`。
- 残ギャップ: [#645](https://github.com/azumag/IRLight/pull/645) はこの baseline では open PR。login/register の頻度 admission と trusted proxy 実装案を main の統制に数えず、閾値 / window / proxy policy は未決とする。KDF concurrency は頻度制限ではない。メール確認、Turnstile 等 bot 対策、支払手段 / account / IP の重複検知、誤検知解除、cluster-wide quota、event pass 発行・消費の実決済受入は残件。

### T07: 違法・権利侵害コンテンツの中継

- 状態: Session stop / credential revoke と緊急停止手順は実装済み、基盤の下記テストは CI 固定済み。通報から停止までの管理者運用と法務は未実装 / 未決、実環境未確認。
- 既存統制・根拠: [Session workflow](../apps/control-api/session_workflow.py) と [ingest store](../apps/control-api/ingest_store.py) に通常停止・失効経路。[緊急停止 runbook](operations/emergency-abuse-stop.md) は人による対象確認、最小 Session 範囲、metadata の保全と実 Node の actual state 確認を要求する。runbook に窓口への言及があっても窓口実在の根拠にはしない。
- 回帰テスト: [test_session_lifecycle.py](../tests/test_session_lifecycle.py) — `WorkflowPrepareTest.test_stop_is_idempotent`。[test_ingest_credentials.py](../tests/test_ingest_credentials.py) — `IngestCredentialStoreTest.test_revoke_session_invalidates_all_active_credentials`。[test_operations_runbook_inventory.py](../tests/test_operations_runbook_inventory.py)（手順の存在・索引契約。通報運用の E2E ではない）。
- 残ギャップ: 公開通報窓口、security contact、管理者が他 owner の不正 Session を止める権限モデル、誤通報 / 異議申立て / 反復違反停止 / 法的要請の記録、証拠保持期間、利用規約 / プライバシー / 特商法表示と専門家確認は未決・未実装。内容審査や自動録画を本書で導入しない。

### T08: 管理者権限の濫用

- 状態: Node の機械用 heartbeat と list/stop 用 admin Bearer の分離は実装済み、CI 固定済み。人間の管理者 RBAC/MFA は未実装、実環境未確認。
- 既存統制・根拠: [node internal API](../apps/control-api/node_internal.py) は Node-specific credential と admin credential を区別。[bootstrap 契約](node-agent-bootstrap-control-authority.md) と [緊急停止 runbook](operations/emergency-abuse-stop.md) は権限・対象・操作記録の運用境界を説明する。auth user の role field 検証は RBAC enforcement ではない。
- 回帰テスト: [test_node_bootstrap.py](../tests/test_node_bootstrap.py) — `NodeInternalApiTest.test_node_and_admin_endpoints_reject_missing_tokens`（既存機械認証の限定された範囲）。[test_auth_user_role_validation.py](../tests/test_auth_user_role_validation.py) — `AuthUserRoleValidationTest.test_role_with_surrounding_whitespace_fails_closed`（record 検証のみ）。
- 残ギャップ: RBAC、MFA 必須化、緊急停止と secret 復号権限の分離、全管理操作の audit、改変耐性、support 時に配信キーを閲覧させない体制は未実装 / 未決。これらに対応する専用回帰・運用 E2E も未整備。admin Bearer の存在を管理者統制の完成証拠としない。

### T09: Node侵害時のsecret流出

- 状態: Session 割当の限定配送、media process 間の secret mount 分離、cleanup は実装済み、CI 固定済み。host 侵害後の封じ込めは実環境未確認。
- 既存統制・根拠: [node internal API](../apps/control-api/node_internal.py) と [Node Agent](../apps/node-agent/agent.py) が bootstrap / stop / cleanup を扱う。[Compose](../docker-compose.node.yml) は Continuity に continuity secret、Gateway に relay / egress secret を分け read-only mount。[delivery](egress-secret-delivery.md)、[Continuity file 境界](continuity-secret-file-boundary.md)、[漏えい疑い runbook](operations/secret-exposure-suspected.md) を参照。
- 回帰テスト: [test_node_bootstrap.py](../tests/test_node_bootstrap.py) — `NodeInternalApiTest.test_bootstrap_token_rejects_a_different_attempt`。[test_egress_runtime_secret_files.py](../tests/test_egress_runtime_secret_files.py) — `EgressSecretInputTest.test_destination_boundary_failure_is_generic_and_redacted`。[test_continuity_secret_files.py](../tests/test_continuity_secret_files.py)（file / mount 境界）。
- 残ギャップ: Node root / Docker socket / process memory 侵害時にその Session の secret は保護できない。短期配送 lease、host / worker isolation、network egress 制限、実 Node の再構築・失効・rotation の受入、機械接続の TLS / private network、復号権限分離は残件。tmpfs と `:ro` の mount は侵害後の secret 非流出保証ではない。

### T10: ログ・監視・例外通知へのsecret混入

- 状態: fixed reason / bounded status、runtime secret file readers、structured log audit と smoke の診断 redaction は実装済み、CI 固定済み。全 producer / 外部監視の受入は実環境未確認。
- 既存統制・根拠: [log inspector](../apps/control-api/log_redaction_inspect_cli.py) は JSONL の sensitive key / URL、構造・容量を read-only 検査し値を出力しない。[log audit](operations/log-redaction-audit.md) と [漏えい runbook](operations/secret-exposure-suspected.md) は producer 側の allowlist と秘密非保存を要求。[runtime file reader](../apps/egress-gateway/egress_runtime_secret_file.py) は固定・pathless な失敗を返す。
- 回帰テスト: [test_log_redaction_inspect.py](../tests/test_log_redaction_inspect.py) — `LogRedactionInspectTests.test_nested_unredacted_secret_is_reported_without_value` と `LogRedactionInspectTests.test_sensitive_url_query_is_reported_without_url`。[test_egress_runtime_secret_files.py](../tests/test_egress_runtime_secret_files.py) — `EgressRuntimeSecretFileTest.test_oversize_and_invalid_utf8_are_controlled_and_pathless`。
- 残ギャップ: inspector は sanitizer ではなく自由文・URL path の秘密を完全検出できない。`SAFE` は全 log の秘密不在証明ではない。exception / monitoring / 通知 / artifact 全経路の producer contract、アクセス制限、retention と漏えい時削除・rotation の運用は別途受入・決定が必要。

### T11: 依存コンテナ・FFmpeg/GStreamer等の脆弱性

- 状態: Python direct pin、resolved transitive audit、CycloneDX SBOM、Action SHA pin、Dependabot と更新 runbook は実装済み、CI 固定済み。container / codec 全体と release integrity は未実装部分があり実環境未確認。
- 既存統制・根拠: [requirements](../apps/control-api/requirements.txt)、[dependency workflow](../.github/workflows/dependency-audit.yml) は `pip check` と `pip-audit --strict`、known-vulnerable fixture、SBOM を検査。[Dependabot](../.github/dependabot.yml) と [update policy](dependency-update-policy.md)、[vulnerability response](dependency-vulnerability-response.md)、[audit scope](dependency-security-audit.md) は通常の merge gate と期限付き例外を定義する。
- 回帰テスト: [test_dependency_update_policy.py](../tests/test_dependency_update_policy.py) — `DependencyUpdatePolicyTest.test_external_github_actions_are_pinned_to_full_commit_sha`。[test_dependency_sbom_workflow.py](../tests/test_dependency_sbom_workflow.py) — `DependencySbomWorkflowTests.test_workflow_generates_and_validates_cyclonedx_sbom`。[test_web_stack_security.py](../tests/test_web_stack_security.py)（既知 framework 修正と互換性）。
- 残ギャップ: direct version pin は全 transitive dependency の lock ではない。Python SBOM / audit は container / OS / FFmpeg / GStreamer の完全な SBOM・vulnerability scan ではない。container image digest pinning、release artifact signing、codec/image の継続監査と更新責任は未決・未実装部分がある。advisory feed の一時点 green を未知脆弱性の不在保証としない。

## 残ギャップの追跡と更新契約

KMS / master-key rotation、RBAC/MFA / audit、データ retention / 退会時削除 / 法令・会計保持、法務文書 / 窓口、worker isolation は Issue #12 と依存 Issue の未決事項として保持します。本文は運用方式・閾値・DB・認証・権限・secret・Cloudflare/ConoHa production を変更する決定文書ではありません。

[#645](https://github.com/azumag/IRLight/pull/645) の採否と閾値・proxy policy、[#545](https://github.com/azumag/IRLight/issues/545) の canary rollout、[#646](https://github.com/azumag/IRLight/pull/646) / [#647](https://github.com/azumag/IRLight/pull/647) 以降の storage / worker 接続は、新しい main のコードと exact-HEAD CI・実環境証拠を区別して更新します。古い audit の「未着手」をそのまま転記せず、実装が増えても残ギャップを消しません。

[README](../README.md) と [operations index](operations/README.md) が本書の入口です。[文書契約テスト](../tests/test_threat_model_contract.py) は11脅威の欠落・重複、根拠ファイルと local link / anchor、回帰テスト symbol、索引からの脱落を offline で検出します。これは安全性そのものの証明ではありません。新しい統制では runtime の回帰テストと未確認事項も更新してください。
