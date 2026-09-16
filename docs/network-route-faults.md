# Network route fault injection

`network-route-fault-injector.py` は、Issue #13 の route-change 障害を再現するための bounded QA helper です。

## 安全境界

- 対象は明示した **named disposable Linux network namespace** のみです。
- host network namespace、default route、resolver 設定、provider resource は変更しません。
- 対象は literal IPv4/IPv6 destination 1件だけで、hostname は受け付けません。
- `plan` は read-only で subprocess を実行しません。
- `apply` / `clear` は `--confirm-disposable-namespace` が必須です。
- loopback destination は追加の `--allow-loopback` がない限り拒否します。
- 障害時間は既存 QA matrix と同じ 10 / 30 / 120 / 600 秒だけです。
- 実行 command は shell を経由せず argv で渡し、1 command あたり 10 秒で timeout します。

## 注入方式

指定した destination に対し、main routing table へ `/32` または `/128` の **blackhole host route** を追加します。

例:

```text
192.0.2.25 -> 192.0.2.25/32
2001:db8::25 -> 2001:db8::25/128
```

既存の default route を置き換えるのではなく、より具体的な host route を一時的に追加するため、その宛先だけを route-level fault にできます。

追加 route には `proto 99` と `metric 42760` を付け、cleanup では同じ selector を使います。注入前には `ip -json route show ... exact <prefix>` で exact host route の有無を確認し、何か存在していれば **置換せず fail-closed** します。

## plan

```bash
python scripts/network-route-fault-injector.py plan \
  --namespace irlight-qa \
  --destination 192.0.2.25 \
  --duration 30 \
  --json
```

plan は preflight / apply / cleanup argv を表示するだけで、routing table を変更しません。

## apply

```bash
python scripts/network-route-fault-injector.py apply \
  --namespace irlight-qa \
  --destination 192.0.2.25 \
  --duration 30 \
  --confirm-disposable-namespace
```

apply 前に exact destination route が存在した場合は mutation を開始しません。apply command が failure / timeout / `KeyboardInterrupt` になった場合も、tagged route が途中まで反映された可能性を考慮して exact cleanup を試みます。

cleanup を確認できなかった場合は成功扱いしません。QA namespace が disposable であることを前提に、その namespace を隔離・破棄して状態をリセットしてください。

## clear

```bash
python scripts/network-route-fault-injector.py clear \
  --namespace irlight-qa \
  --destination 192.0.2.25 \
  --confirm-disposable-namespace
```

clear はこの helper が使う blackhole type / prefix / table / protocol / metric の組み合わせだけを削除します。

## loopback

loopback を対象にする場合は明示的に追加承認します。

```bash
python scripts/network-route-fault-injector.py apply \
  --namespace irlight-qa \
  --destination 127.0.0.1 \
  --duration 10 \
  --allow-loopback \
  --confirm-disposable-namespace
```

loopback route の変更は namespace 内の test harness 自体を壊しやすいため、通常の RTMP/SRT egress fault では使用しないでください。

## E2Eでの使い方

RTMP/SRT の同一 fault 条件比較では、対象 destination の literal IP を test harness 側で確定した後にこの helper を使います。DNS 障害は `network-dns-fault-injector.py`、TCP reset は `network-tcp-reset-fault-injector.py` と責務を分離します。

この helper 自体は namespace の作成、veth 設定、publisher/destination の起動、外部配信先への接続を行いません。実 routing mutation は disposable QA namespace が用意された環境でのみ実施します。
