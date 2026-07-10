# 双向 connector 配置

生产入口默认读取 `connectors/outbound.json`；该文件被本目录 `.gitignore` 排除，不能提交。配置文件只写
endpoint、目标和环境变量名，token 本身只能通过环境变量注入。若确实只做离线测试，运行命令必须显式带
`--no-outbound`；这会保留本地 outbox，但不算通知已交付。

profile 加载成功后，transport 会在内存持有 token/secret，并立即从 run 进程的 `os.environ` 擦除对应变量；随后
启动的研究 Codex 与 manifest/harness 子进程不会继承 connector 凭据。父 shell 的环境不受影响。
每个 outbound/inbound/channel 必须使用不同的环境变量和不同的 secret value；inbound HMAC secret 至少
32 个 printable-ASCII 字符。inbound listener 只接受 IPv4 loopback 字面量。

## 严格 webhook（推荐）

从 `outbound.example.json` 复制配置。系统发送：

```json
{
  "protocol_version": 1,
  "channel": "qq",
  "producer_id": "mr-0123456789abcdef0123456789abcdef",
  "event_key": "directive:1:received:v2",
  "kind": "directive_received",
  "payload": {}
}
```

同时发送 `Idempotency-Key: <producer_id>:<event_key>`。接收端必须先按
`(producer_id, event_key)` 耐久去重，再返回：

```json
{"accepted": true, "producer_id": "mr-0123456789abcdef0123456789abcdef", "event_key": "directive:1:received:v2", "delivery_id": "可选远端回执号"}
```

只有两个身份字段都精确回显才会写本地成功回执。`producer_id` 在每个 work-root 首次启动时生成并持久保存在
`state/outbound_producer_id`；进程重启保持不变，不同 work-root 不会撞键。若远端已接纳、但本地回执落盘前
进程被 kill，系统会重发同一 `(producer_id,event_key)`；
接收端必须把它视为同一事件而不是生成第二次用户可见效果。connector 禁止 HTTP redirect；远端只允许 HTTPS，
明文 HTTP 仅允许 loopback IP，避免 bearer token 被转发或明文外泄。生产 profile 的所有 endpoint（包括
loopback）都强制要求 `token_env`，防止共享机器上错误进程/端口冒占后伪造 ACK。

## OneBot v11 QQ

可直接连接本机 OneBot HTTP 实现：

```json
{
  "version": 1,
  "channels": {
    "qq": {
      "type": "onebot_v11",
      "base_url": "http://127.0.0.1:5700",
      "token_env": "METARESEARCH_ONEBOT_TOKEN",
      "timeout_s": 1,
      "target_kind": "private",
      "target_id": 123456789,
      "conversation_id": "qq:123456789",
      "inbound": {
        "type": "onebot_v11_http_post",
        "listen_host": "127.0.0.1",
        "listen_port": 8766,
        "path": "/onebot/events",
        "secret_env": "METARESEARCH_ONEBOT_INBOUND_SECRET",
        "consumer_id": "research-run-primary",
        "source_id": "onebot-primary",
        "request_timeout_s": 1,
        "self_id": 987654321,
        "allowed_user_ids": [123456789],
        "require_at": false
      }
    }
  },
  "delivery": {"retry_initial_s": 1, "retry_max_s": 300, "batch_size": 32}
}
```

`target_kind` 可为 `private` 或 `group`。`conversation_id` 是该固定收件目标的强绑定；任何
`interaction_*` / `directive_*` 事件若来源会话不完全一致都会拒投，避免另一私聊/群的回复泄漏到本目标。需要多会话时应为
每个经认证的会话配置独立 channel，或使用严格 webhook 网关完成持久路由。OneBot 消息正文和 `echo` 都携
`producer_id:event_key`；但 OneBot v11 本身没有
标准化的幂等写接口，因此极窄的“QQ 已发送、本地回执未落”崩溃窗可能让人看到重复消息。需要机械
exactly-once 用户效果时，应使用上面的严格 webhook，由网关持久去重后再调用 QQ。

OneBot inbound 使用标准 reverse HTTP POST：校验原始 body 的
`X-Signature: sha1=<HMAC-SHA1>` 与 `X-Self-ID`，且只接纳固定 private friend，或显式确认共享可见性的固定群。
群配置必须增加 `"group_shared_conversation_ack": true`；默认还要求首个 segment 为指向本 bot 的 `at`。
OneBot 必须把 `message` 配为 segment array；image/reply/file/CQ 元数据均拒绝。若实现不能为 reverse POST
提供一个与 outbound API token 不同的签名 secret，应在前面增加严格 webhook 网关，不要降低密钥隔离。

## 入站 webhook v1

`inbound.type=webhook_v1` 时，网关向配置的 loopback path 发送严格 JSON：

```json
{"protocol_version":1,"message_id":"gateway-event-42","text":"当前状态是什么？"}
```

必须恰有以下签名头：`X-Meta-Research-Version: 1`、`X-Meta-Research-Key-Id`、
`X-Meta-Research-Audience`（等于 `consumer_id`）、十进制 Unix 秒
`X-Meta-Research-Timestamp`、32 位小写十六进制 `X-Meta-Research-Request-Id`、
`X-Meta-Research-Event-Id`（等于 body.message_id）及 `X-Meta-Research-Signature`。
签名输入为以下 ASCII 前缀加原始 body bytes，使用 inbound secret 做 HMAC-SHA256：

```text
meta-research-inbound-v1\n<key_id>\n<consumer_id>\n<timestamp>\n<request_id>\n<event_id>\n<body bytes>
```

签名头值为 `sha256=<64位小写hex>`。系统只从已认证请求接受外部 message ID 与纯文本；channel、source、
principal、conversation、session 和 DB idempotency key 全由本地 profile 派生。记录 fsync 到独立 connector spool
后才返回 `accepted=true`；同 event/envelope 重放返回相同 receipt 且不追加，同 event 改内容会耐久 quarantine
并使该 ingress fail-loud。内部消费是非破坏 `poll` + 逐条 durable `commit_poll`，因此 DB 提交前崩溃会安全重放。
`request_timeout_s` 是从 accept 起覆盖 request-line/header/body 的总墙钟 deadline，不是可被 slow-drip 重置的
idle timeout；header 在 stdlib 物化前即受 32 行/16 KiB 上限约束，并发 handler 固定有界。每次 pump 对每个
channel 最多提交一条、轮转起始 channel；远端洪泛会让研究 fail-closed 等待 backlog，但不能长期独占本地
console emergency pause 的同步锁。

精确回复 `确认指令 dN` / `拒绝指令 dN` 才进入 connector action 语法，并在最终事务核对同一
connector/conversation/principal/profile；其他 JSON/CQ/近似文本都只是普通自然语言。精确“继续”在一个 DB
事务内读取到达时 pause 状态：运行中只回无状态变更 ACK，已暂停才创建待确认 resume。

生产 profile 的单次 `timeout_s` 限在 0.1–1.0 秒：connector 为串行、有界发送，过长的低优先级请求会占住
随后到达的 query reply；1 秒上限为 policy 的 2 秒 ACK/query p95 目标保留派生与第二次发送预算。超时事件会
以同一幂等身份退避重试，不会丢失。`batch_size` 允许 4–256；最小 4 为当前四档优先级各保留一个推进槽，
避免持续 query 或 poison event 永久饿死审计通知。

## 运行与观测

```bash
cp connectors/outbound.example.json connectors/outbound.json
chmod 600 connectors/outbound.json
export METARESEARCH_QQ_CONNECTOR_TOKEN='<secret>'
export METARESEARCH_QQ_INBOUND_SECRET='<至少32字符且与outbound不同的随机secret>'
python -m orchestrator.run --system-root . --work-root /path/to/run --max-cycles 100
```

运行期文件位于 `<work-root>/state/`：

- `outbox.jsonl`：从 DB 权威状态可重建的事件流；
- `outbound_producer_id`：远端幂等命名空间，**必须和整个 work-root 一起备份/恢复，不可单独删除或重建**；
- `outbound_delivery_state.json`：当前失败、attempt 次数、下次重试时刻和消毒后的错误；
- `delivery_receipts.jsonl`：远端 ACK（v1）或历史无可信路由事件安全抑制（v2）的耐久终态；
- `delivered.log`：旧版兼容标记，新投递以 receipts 为权威。
- `connector_<channel>_inbox.jsonl` / `.cursor` / `.retry.json`：该认证 channel 独立的入站、消费游标与重试权威；
- `connector_<channel>_quarantine.jsonl`：身份碰撞的 hash-only 安全证据；非空时重启拒绝继续，须人工归档。
- `connector_<channel>_recovery.jsonl`：进程崩溃留下的未 ACK、未换行 inbox 尾部之 hash/长度/offset 审计；
  启动会先耐久记录再安全截断，绝不把残尾补成 committed poison 后继续 ACK。

控制台通知流会合并显示 `delivered` / `retrying` / `suppressed`；权威文件损坏会明确显示
`transport_authority_corrupt`，不会伪报成功。网络调用在独立 delivery 线程执行，不阻塞研究 Runner、
interaction 入站或 query 完成；传输失败按 event_key 指数退避并跨重启继续。调度只在同一
interaction/directive/file-request 生命周期内保序，紧急 query reply 可越过无关积压；一条永久拒绝不会拖死
整个 channel。worker 本地状态损坏会在常驻 supervisor 的下一个安全轮询点上浮并终止，不能静默形成通知黑洞。

升级自旧的本地 outbox 时，若只有 `outbox.jsonl`/`delivered.log`，系统会在锁内生成 producer ID，保留本地键与
既有 delivered 语义；若已有 receipt/retry 却缺 producer ID，启动会 fail-loud——必须从同一 work-root 备份恢复，
不可删除状态来“修复”。坏 receipt/retry 同样应先停机、保全整个 `state/` 后修复，不能手工删行并继续生产。
