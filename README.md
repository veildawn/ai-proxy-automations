# AI Proxy Automations

AI Proxy 的公开自动化市场。这里发布 `type: automation` 的 Manifest v3；声明式供应商集成位于
[`veildawn/ai-provider-plugins`](https://github.com/veildawn/ai-provider-plugins)。

## 仓库布局

```text
index.json                         自动化索引 v1
automations/<id>.json              Automation Manifest v3
publishers/<handle>.json           Ed25519 发布者公钥
revoke.json                        包/版本/密钥撤回清单
schemas/*.schema.json              JSON Schema
scripts/validate.py                索引、WASM 摘要和发布者一致性检查
```

## 自动化包

Automation 是一个或多个 Job、其 WASM 模块和可选设置表单的组合：

```json
{
  "manifest_version": 3,
  "id": "example-job",
  "type": "automation",
  "version": "1.0.0",
  "name": {"zh": "示例任务", "en": "Example Job"},
  "description": {"zh": "每小时运行。", "en": "Runs hourly."},
  "publisher": "your-handle",
  "provides": {
    "jobs": [{
      "key": "hourly",
      "module": "main",
      "schedule": "0 * * * *",
      "timeout": "30s",
      "capabilities": {"read": ["usage"]}
    }]
  },
  "modules": [{
    "id": "main",
    "runtime": "wasm@1",
    "data": "...base64...",
    "sha256": "...64 hex..."
  }],
  "signature": {"alg": "ed25519", "key_id": "ed25519:...", "sig": "..."}
}
```

`id` 是市场包 id；Job 的 `key` 只需在包内唯一。升级必须保留已发布 Job key，否则旧运行历史将失去关联。
Automation 不能同时声明 providers/model facts/pools/plans；两条 lane 的信任与生命周期不同。

## pluginrt WASM 沙箱

`runtime: wasm@1` 由 `pluginrt` 执行。每次运行创建全新实例，默认没有 WASI，因此没有环境变量、
文件系统、网络、系统时钟、随机源、stdin/stdout。模块只能导入：

```text
ai_proxy_v1.call(op_ptr, op_len, req_ptr, req_len, out_ptr, out_cap) -> response_len
```

并必须导出：

```text
memory
alloc(i32) -> i32
run(input_ptr: i32, input_len: i32) -> i32
```

`run` 返回 0 表示成功。模块大小上限 2 MiB，总模块大小上限 2 MiB，单次内存上限 64 MiB；
默认超时 30 秒，Manifest 最大可设 2 分钟。每个模块必须携带解码后字节的 SHA-256。

当前 `quota-guard` 文件保留上游仓库提供的最小 WASM 占位模块，用于发布格式与能力审核；它没有
pluginrt 所需的 ABI 导出，运行会被安全地记录为错误。要将其用于生产告警，先替换为实现上述 ABI、
读取额度并按阈值调用 `notify.send` 的模块，更新摘要、版本和签名。这样不会把一个无效示例伪装成可用守卫。

## 调度规则

Job 的 `schedule` 接受五段 Cron 或 `@every <duration>`：

- `0 */1 * * *`：每小时整点。
- `*/15 * * * *`：每 15 分钟。
- `@every 6h`：从调度器启动起每 6 小时。

Cron 使用服务端时区。`timeout` 使用 Go duration（如 `15s`、`1m`），必须大于 0 且不超过 2 分钟。
`disabled: true` 可让任务安装后保持暂停；管理员仍可手动运行。调度、手动运行、成功、失败、超时与取消
都会写入运行历史，写操作另有逐项审计记录。

## Capabilities

权限绑定在每个 Job，而不是模块上；同一模块被两个 Job 使用时不会自动共享权限。省略即拒绝。

| 字段 | 可选值/含义 |
|---|---|
| `http` | 精确主机或单层 `*.example.com`；不能写 scheme、路径或 `*`。 |
| `read` | `accounts`、`quota`、`usage`、`catalog`；只返回无凭据投影。 |
| `write` | `catalog_prices`、`account_state`、`quota_refresh`；每次写入都会审计。 |
| `notify` | `true` 时允许向管理后台通知箱写消息。 |

常用 host 操作包括 `accounts.list`、`accounts.unhealthy`、`quota.snapshot`、`usage.today`、
`quota.refresh`、`quota.poll`、`notify.send` 与包私有 KV。能力检查发生在 host 边界，WASM 无法绕过。

## Settings

可选 `settings[]` 由后台渲染表单。类型为 `string`、`secret`、`number`、`bool`、`select`、
`textarea`。每项需要稳定的 `key` 和多语言 `label`；secret 禁止携带默认值。提交值按 Schema 校验，
secret 加密保存且不会回显给客户端。

## 发布流程

1. 新增 `automations/<id>.json`，使用 `type: automation`，填入 Job、模块、最小权限与多语言文本。
2. 对 WASM 计算 SHA-256；确认 base64 解码后以 `\0asm` 开头。
3. 在 `index.json` 添加摘要，保证 id/version/type/name/description/publisher 与 Manifest 一致。
4. 用发布者 Ed25519 私钥签名，发布对应公钥；私钥永远不进入 Git。
5. 运行校验并提交 PR。升级时提高版本、重新签名、同步索引，不要原地改写已发布版本。

```bash
python3 -m pip install check-jsonschema
check-jsonschema --schemafile schemas/index-v1.schema.json index.json
check-jsonschema --schemafile schemas/automation-v3.schema.json automations/*.json
python3 scripts/validate.py
```

`revoke.json` 可按包、版本、checksum 或 key id 撤回。发布者文件可保留历史密钥；标记 `revoked_at`
的密钥不能再用于新安装。
