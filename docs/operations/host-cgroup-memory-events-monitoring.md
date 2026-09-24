# Host cgroup memory.events monitoring

Issue #11 の host pressure 診断で cgroup v2 の `memory.events` を扱う場合、累積 counter の生値をそのまま health 判定に使わない。同じ workload cgroup・同じ generation で取得した operator-managed baseline と現在値を比較し、その後に増えた event だけを評価する。

## Explicit-target adapter

host 側から利用する場合は `check-host-cgroup-memory-events.sh` に現在の `memory.events` と baseline snapshot の両方を明示する。

```bash
bash scripts/check-host-cgroup-memory-events.sh \
  /sys/fs/cgroup/<target>/memory.events \
  /run/irlight-monitor/<target>.memory.events.baseline
```

片方でも未指定なら `UNKNOWN reason=target_not_configured` とする。adapter は root/current cgroup を推測せず、baseline の探索・作成・更新・削除もしない。

実際の判定は既存の `check-cgroup-memory-events.sh` に委譲する。

- `oom` / `oom_kill` / `oom_group_kill` の増加: `CRITICAL`
- `low` / `high` / `max` の増加: `WARNING`
- 対象 counter の増加なし: `OK`
- current/baseline の欠落、読取不能、不正 record、counter reset: `UNKNOWN`

`memory.events` は kernel version により counter が追加され得るため、checker は未知 counter の record shape と整数値を検証したうえで、解釈対象外の counter 自体は無視する。既知の必須 counter が欠ける場合は正常と推測せず `UNKNOWN` にする。

## Baseline lifecycle

baseline は監視対象 cgroup と同じ generation の snapshot でなければならない。container/service の再作成、cgroup path の再利用、host reboot 等で generation が変わった可能性がある場合は、operator 側で generation を確認して baseline を更新する。

この診断は baseline lifecycle を所有しない。counter reset を自動的に「新 generation」と解釈して baseline を上書きすることもない。誤った generation を正常扱いするより `UNKNOWN` へ fail-closed する。

## Safety

この adapter と underlying checker は read-only である。cgroup control file、baseline、process、memory limit、swap、reclaim 設定を変更しない。OOM を検知しても process/container restart、kill、Node drain、provider resource 操作などの remediation は実行しない。

出力には cgroup path、container ID、Session ID、内部識別子、secret を含めない。

## Verification

```bash
python -m unittest discover -s tests -p 'test_cgroup_memory_events_check.py' -v
python -m unittest discover -s tests -p 'test_host_cgroup_memory_events_adapter.py' -v
bash -n scripts/check-cgroup-memory-events.sh
bash -n scripts/check-host-cgroup-memory-events.sh
```

この adapter は host aggregate に安全に組み込むための explicit-target boundary であり、`check-host-pressure.sh` への opt-in 登録は別の差分で行う。aggregate へ追加する際も default output を変更せず、明示 opt-in、bounded execution、`CRITICAL > UNKNOWN > WARNING > OK` の既存契約を維持する。
