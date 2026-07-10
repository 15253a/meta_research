# 出站 connector 配置

生产入口默认读取 `connectors/outbound.json`；该文件被本目录 `.gitignore` 排除，不能提交。配置文件只写
endpoint、目标和环境变量名，token 本身只能通过环境变量注入。若确实只做离线测试，运行命令必须显式带
`--no-outbound`；这会保留本地 outbox，但不算通知已交付。

profile 加载成功后，transport 会在内存持有 token，并立即从 run 进程的 `os.environ` 擦除对应变量；随后
启动的研究 Codex 与 manifest/harness 子进程不会继承 connector 凭据。父 shell 的环境不受影响。

## 严格 webhook（推荐）

从 `outbound.example.json` 复制配置。系统发送：

```json
{
  "protocol_version": 1,
  "channel": "qq",
  "producer_id": "mr-0123456789abcdef0123456789abcdef",
  "event_key": "directive:1:received",
  "kind": "directive_received",
  "payload": {}
}
```

同时发送 `Idempotency-Key: <producer_id>:<event_key>`。接收端必须先按
`(producer_id, event_key)` 耐久去重，再返回：

```json
{"accepted": true, "producer_id": "mr-0123456789abcdef0123456789abcdef", "event_key": "directive:1:received", "delivery_id": "可选远端回执号"}
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
      "conversation_id": "qq:123456789"
    }
  },
  "delivery": {"retry_initial_s": 1, "retry_max_s": 300, "batch_size": 32}
}
```

`target_kind` 可为 `private` 或 `group`。`conversation_id` 是该固定收件目标的强绑定；任何
`interaction_*` 事件若来源会话不完全一致都会拒投，避免另一私聊/群的回复泄漏到本目标。需要多会话时应为
每个经认证的会话配置独立 channel，或使用严格 webhook 网关完成持久路由。OneBot 消息正文和 `echo` 都携
`producer_id:event_key`；但 OneBot v11 本身没有
标准化的幂等写接口，因此极窄的“QQ 已发送、本地回执未落”崩溃窗可能让人看到重复消息。需要机械
exactly-once 用户效果时，应使用上面的严格 webhook，由网关持久去重后再调用 QQ。

生产 profile 的单次 `timeout_s` 限在 0.1–1.0 秒：connector 为串行、有界发送，过长的低优先级请求会占住
随后到达的 query reply；1 秒上限为 policy 的 2 秒 ACK/query p95 目标保留派生与第二次发送预算。超时事件会
以同一幂等身份退避重试，不会丢失。`batch_size` 允许 4–256；最小 4 为当前四档优先级各保留一个推进槽，
避免持续 query 或 poison event 永久饿死审计通知。

## 运行与观测

```bash
cp connectors/outbound.example.json connectors/outbound.json
chmod 600 connectors/outbound.json
export METARESEARCH_QQ_CONNECTOR_TOKEN='<secret>'
python -m orchestrator.run --system-root . --work-root /path/to/run --max-cycles 100
```

运行期文件位于 `<work-root>/state/`：

- `outbox.jsonl`：从 DB 权威状态可重建的事件流；
- `outbound_producer_id`：远端幂等命名空间，**必须和整个 work-root 一起备份/恢复，不可单独删除或重建**；
- `outbound_delivery_state.json`：当前失败、attempt 次数、下次重试时刻和消毒后的错误；
- `delivery_receipts.jsonl`：远端 ACK 的耐久回执及其 hash；
- `delivered.log`：旧版兼容标记，新投递以 receipts 为权威。

控制台通知流会合并显示 `delivered` / `retrying`；权威文件损坏会明确显示
`transport_authority_corrupt`，不会伪报成功。网络调用在独立 delivery 线程执行，不阻塞研究 Runner、
interaction 入站或 query 完成；传输失败按 event_key 指数退避并跨重启继续。调度只在同一
interaction/directive/file-request 生命周期内保序，紧急 query reply 可越过无关积压；一条永久拒绝不会拖死
整个 channel。worker 本地状态损坏会在常驻 supervisor 的下一个安全轮询点上浮并终止，不能静默形成通知黑洞。

升级自旧的本地 outbox 时，若只有 `outbox.jsonl`/`delivered.log`，系统会在锁内生成 producer ID，保留本地键与
既有 delivered 语义；若已有 receipt/retry 却缺 producer ID，启动会 fail-loud——必须从同一 work-root 备份恢复，
不可删除状态来“修复”。坏 receipt/retry 同样应先停机、保全整个 `state/` 后修复，不能手工删行并继续生产。
