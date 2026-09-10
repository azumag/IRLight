# Destination verification admission

`POST /v1/destinations/{destination_id}/verify` は、ユーザー指定先に対して DNS 解決と RTMP/RTMPS/SRT の短時間 transport probe を行う。probe 本体には単一の monotonic deadline と bounded DNS/SRT cleanup があるが、同時要求数にも独立した上限を設ける。

## 同時実行上限

Control Plane は outbound probe を開始する前に non-blocking admission slot を取得する。既定は同一 runtime filesystem 上で最大 4 probe。同じコンテナ/host の複数 Uvicorn worker process と worker thread は `flock` slot を共有するため、worker 数を増やしても probe 上限がそのまま倍増しない。

- `IRLIGHT_VERIFY_MAX_CONCURRENT`: `1..32`。未設定・不正値・範囲外は既定 `4`。
- `IRLIGHT_VERIFY_ADMISSION_DIR`: slot file を置く絶対 path。未設定時は `/tmp/irlight-destination-probe-admission`。

slot file は request、Destination ID、URL、credential を保存しない。lock は process が異常終了しても kernel が解放するため、stale lease record の回収処理を必要としない。

## 飽和時の挙動

全 slot が使用中なら probe を開始せず HTTP 503 `destination verification is busy` と `Retry-After: 1` を返す。admission directory / slot を安全に利用できない場合も、外向き接続を開始せず HTTP 503 `destination verification is unavailable` に fail-closed する。

飽和・admission 障害は Destination 自体の検証失敗ではないため、`verification_status=FAILED` を記録するための store verify は呼ばない。再試行可能な入口負荷として扱う。

この gate は Destination verify route のみに掛かる。Session stop、Node heartbeat、既存配信の continuity/recovery path をこの slot 待ちに巻き込まない。

## 配置境界

既定 directory が共有するのは同じ Control Plane コンテナ/host 内の worker であり、別 host / 別 container の replica を跨いだ cluster-wide quota ではない。複数 replica を同時稼働する deployment では、`IRLIGHT_VERIFY_ADMISSION_DIR` を flock semantics が保証される共有 runtime filesystem に置くか、外部 admission layer を別途設計する。

per-user / IP 単位の開始頻度制限はこの変更では決めない。正当ユーザーのロックアウト、trusted proxy 境界、multi-replica counter semantics を伴うため、Issue #91 の follow-up として扱う。

## 安全境界

- admission 取得前に provider resource、Media Node、Session を作成しない。
- busy を回避するため timeout を延長したり、無制限 queue を作らない。
- slot file へ URL / stream key / SRT streamid / user identity を書かない。
- symlink の admission directory / slot file は受理しない。
- 既存 probe の SSRF 防止、TLS certificate verification、DNS pinning、single-deadline contract を弱めない。
