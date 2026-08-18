# AI Proxy Service 插件与代码扩展开发手册 (Plugin Developer Guide)

欢迎开发 `ai-proxy-service` 自定义插件！本平台支持两类插件体系：
1. **集成插件 (`integration`)**：用于接入第三方模型供应商、OAuth 登录流、端点声明与多模型目录映射。
2. **自动化与代码插件 (`automation`)**：支持用户**自带完整编译代码 (WebAssembly 沙箱运行时)**，执行**自定义配额查询、提示词清洗与重写、协议转换拦截、多账号智能健康巡检与告警通知**。

---

## 目录
- [一、插件 Manifest 规范 (v3)](#一插件-manifest-规范-v3)
- [二、四大核心能力开发实践](#二四大核心能力开发实践)
  - [1. 自定义配额与余额查询 (Quota & Balance Poller)](#1-自定义配额与余额查询-quota--balance-poller)
  - [2. 提示词清洗与安全脱敏 (Prompt Sanitizer & Rewriter)](#2-提示词清洗与安全脱敏-prompt-sanitizer--rewriter)
  - [3. 协议转换与请求响应拦截 (Protocol Transformer)](#3-协议转换与请求响应拦截-protocol-transformer)
  - [4. 账号健康巡检与告警推送 (Health Check & Alerting)](#4-账号健康巡检与告警推送-health-check--alerting)
- [三、WebAssembly ABI 接口与 SDK 说明](#三webassembly-abi-接口与-sdk-说明)
- [四、多语言编译与打包指南 (Rust / Go / TinyGo / TypeScript)](#四多语言编译与打包指南-rust--go--tinygo--typescript)
- [五、本地调试与上传安装](#五本地调试与上传安装)

---

## 一、插件 Manifest 规范 (v3)

插件以单个 `.json` 格式组织，支持声明元数据、可配置参数（Settings）、权限声明（Capabilities）及内嵌的 Wasm 字节码（Modules）：

```json
{
  "manifest_version": 3,
  "id": "my-custom-plugin",
  "type": "automation",
  "version": "1.0.0",
  "name": {
    "zh": "全能自定义扩展插件",
    "en": "All-in-One Custom Plugin"
  },
  "description": {
    "zh": "包含自定义配额查询、提示词清洗与智能告警的完整代码插件",
    "en": "Custom code plugin for quota polling, prompt sanitizing and alerting."
  },
  "author": "developer",
  "settings": [
    {
      "key": "webhook_url",
      "type": "secret",
      "label": {"zh": "告警 Webhook 地址", "en": "Alert Webhook URL"},
      "required": true
    },
    {
      "key": "quota_threshold",
      "type": "number",
      "label": {"zh": "额度预警阈值 ($)", "en": "Quota Alert Threshold ($)"},
      "default": 5.0
    }
  ],
  "provides": {
    "jobs": [
      {
        "key": "quota_and_health_tick",
        "name": {"zh": "每 15 分钟巡检", "en": "15-Min Inspection"},
        "module": "main_runner",
        "schedule": "*/15 * * * *",
        "timeout": "30s",
        "capabilities": {
          "read": ["quota", "accounts", "usage"],
          "write": ["quota_refresh", "account_state"],
          "http": ["api.vendor.com", "qyapi.weixin.qq.com", "open.feishu.cn"],
          "notify": true
        }
      }
    ]
  },
  "modules": [
    {
      "id": "main_runner",
      "runtime": "wasm@1",
      "data": "<BASE64_ENCODED_WASM_BYTES>",
      "sha256": "<SHA256_HEX_OF_WASM_BYTES>"
    }
  ]
}
```

---

## 二、四大核心能力开发实践

### 1. 自定义配额与余额查询 (Quota & Balance Poller)
针对非标准第三方或聚合 API，插件可编写代码拉取账号余额并上报网关：

```rust
// Rust 示例: 提取第三方配额并写入网关
fn poll_vendor_quota(account_id: &str, api_key: &str) -> Result<f64, String> {
    // 1. 发起外部 HTTP 请求
    let req = serde_json::json!({
        "method": "GET",
        "url": "https://api.vendor.com/v1/user/balance",
        "headers": { "Authorization": format!("Bearer {}", api_key) }
    });
    let resp = host_call("http.request", &req)?;
    let balance: f64 = resp["value"]["body"]["balance"].as_f64().unwrap_or(0.0);

    // 2. 刷新网关配额窗口
    let quota_update = serde_json::json!({
        "account_id": account_id,
        "remaining_usd": balance
    });
    host_call("quota.refresh", &quota_update)?;
    Ok(balance)
}
```

### 2. 提示词清洗与安全脱敏 (Prompt Sanitizer & Rewriter)
在请求流经网关时对 `messages` 进行清洗、敏感词过滤或系统上下文注入：

```rust
// Rust 示例: 清洗 Prompt 与去除敏感信息
fn sanitize_messages(messages: &mut Vec<serde_json::Value>) {
    for msg in messages.iter_mut() {
        if let Some(content) = msg.get_mut("content") {
            if let Some(text) = content.as_str() {
                // 替换敏感 Token / 注入合规规则
                let cleaned = text.replace("SECRET_API_KEY", "[REDACTED]");
                *content = serde_json::Value::String(cleaned);
            }
        }
    }
}
```

### 3. 协议转换与请求响应拦截 (Protocol Transformer)
将私有/非标准模型协议（如自定义 JSON-RPC 或变种 SSE）转换为 OpenAI / Anthropic 标准协议：

```rust
// 示例: 将自定义入参映射为标准 OpenAI Chat Completions
fn transform_inbound_request(custom_payload: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "model": custom_payload["target_model"].as_str().unwrap_or("gpt-4o"),
        "messages": custom_payload["conversation"],
        "temperature": custom_payload["temp"].as_f64().unwrap_or(0.7),
        "stream": custom_payload["streaming"].as_bool().unwrap_or(true)
    })
}
```

### 4. 账号健康巡检与告警推送 (Health Check & Alerting)
当账号失效（401/429/欠费）或额度低于阈值时，自动禁用账号并向管理员企微/飞书推送告警：

```rust
fn check_and_alert_low_quota(account_id: &str, balance: f64, threshold: f64) {
    if balance < threshold {
        // 发送内置系统通知
        let _ = host_call("notify.send", &serde_json::json!({
            "title": format!("账号额度预警: {}", account_id),
            "body": format!("当前剩余额度: ${:.2}，已低于阈值 ${:.2}", balance, threshold),
            "severity": "warn"
        }));
    }
}
```

---

## 三、WebAssembly ABI 接口与 SDK 说明

### 必须导出的函数
模块需向宿主导出以下符号：
1. `memory`: 线性内存实例（最大限制 64 MiB）。
2. `alloc(size: i32) -> i32`: 内存分配函数。
3. `run(input_ptr: i32, input_len: i32) -> i32`: 主执行入口，入参为包含 `plugin`、`job`、`settings` 的 UTF-8 JSON，返回 `0` 表示成功。

### 宿主 Host Import 调用
沙箱唯一允许导入的宿主接口为：
```text
ai_proxy_v1.call(
  op_ptr: i32, op_len: i32,
  req_ptr: i32, req_len: i32,
  resp_ptr: i32, resp_capacity: i32
) -> resp_len: i32
```

支持的操作列表（需在 Job Capabilities 声明对应权限）：
- `http.request` (需 `capabilities.http`)：发起受限的出站 HTTP 请求。
- `accounts.list` / `accounts.disable` / `accounts.enable` (需 `read/write: accounts`)：查询与启停账号。
- `quota.snapshot` / `quota.refresh` / `quota.poll` (需 `read/write: quota`)：查询与同步配额。
- `store.get` / `store.set` / `store.delete` / `store.keys`：插件私有持久化 KV 存储。
- `notify.send` (需 `notify: true`)：发送管理后台通知。
- `log.write`：向当前执行日志写入记录。

---

## 四、多语言编译与打包指南

### 1. 使用 Rust 编译
```bash
# 1. 添加 wasm32-unknown-unknown 目标
rustup target add wasm32-unknown-unknown

# 2. 编译发布包
cargo build --target wasm32-unknown-unknown --release

# 3. 提取 wasm 文件并生成 Base64
base64 -i target/wasm32-unknown-unknown/release/my_plugin.wasm | tr -d '\n' > module.b64
shasum -a 256 target/wasm32-unknown-unknown/release/my_plugin.wasm
```

### 2. 使用 Go (TinyGo) 编译
```bash
tinygo build -o plugin.wasm -target=wasm main.go
base64 -i plugin.wasm | tr -d '\n' > module.b64
```

---

## 五、本地调试与上传安装

1. **打包 Manifest**：将 Base64 字符串填入 `manifest.json` 的 `modules[0].data`，计算对应 `sha256`。
2. **管理后台上传**：
   - 登录 `ai-proxy-service` Web 管理后台；
   - 进入 **「自动化 (Automations)」** 或 **「集成 (Integrations)」** 页面；
   - 点击 **「导入 Manifest」**，选择本地编写好的 `.json` 文件一键安装。
3. **私有市场分发**：
   - 将 Manifest 提交到私有仓库 `veildawn/ai-proxy-automations` / `veildawn/ai-provider-plugins`；
   - 网关配置了对应 Registry 后，即可在市场列表中一键点击下载与平滑升级！
