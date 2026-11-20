# TG_PMbot 🤖

[![Deploy to Cloudflare Workers](https://img.shields.io/badge/Deploy%20to-Cloudflare%20Workers-F38020?logo=cloudflare&logoColor=white)](https://workers.cloudflare.com/)
[![Python](https://img.shields.io/badge/Python-3.12%20(Pyodide)-3776AB?logo=python&logoColor=white)](https://pyodide.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

轻量、高性能、零成本的 **Telegram 私聊反代/客服转发机器人**。  
基于 **Cloudflare Workers (Python Runtime)** 与 **Workers KV** 架构，无需自备服务器，全球边缘低延迟直连 Telegram。

---

## ✨ 核心特性

- **🔄 双向消息原生转发与引用（Reply）支持**：
  - **访客发消息**：通过 `forwardMessage` **直接转发**给管理员，完整保留对方名片信息、头像与 Telegram 转发来源标签，方便识别对方身份。
  - **管理员回复**：管理员在私聊中直接【回复】某条转发消息，Bot 将通过 `copyMessage` **无引用转发**给对方（隐匿管理员真实账号），同时在用户侧精准还原对原消息的引用关联（Quote）。
  - **双向引用保持**：双方使用 Telegram 的 Reply 功能时，均可在接收端精准看到被引用的原消息。
  - **30 天索引存储**：基于 Workers KV 自动维护双向消息 ID 映射关系，TTL 30 天自动过期，杜绝垃圾数据堆积。
- **👌 原生 Reaction 表情投递反馈**：
  - 消息转发成功后，Bot 自动在原消息上打上 `👌` 表情反应（无需多余提示气泡，体验极度轻量丝滑）。
- **🛡️ 创新 ASCII 点阵 Captcha 验证**：
  - 纯 Python 动态生成 $7\times 7$ 点阵字符画（使用 `-` 与 `0` 绘制 2 位随机数）。
  - 使用 Telegram `<pre>` 等宽语法包裹下发，移动端和桌面端排版工整不跑偏。
  - 5 分钟超时控制，通过验证后自动清理验证码消息，保持聊天界面整洁。
- **👤 访客身份名片 (User Info Banner)**：
  - 未验证用户首次通过验证时，在转发其消息前，先向管理员推送结构化名片：
    - **昵称**：使用 `<code>{name}</code>` 包裹，点击即可复制。
    - **用户名**：`@{username}`（无则展示 `null`）。
    - **UID**：使用 `<code>{uid}</code>` 包裹，点击即可复制。
    - **DC（数据中心）**：通过解码用户头像的 Telegram File ID 二进制结构（Base64 URL-safe + RLE 解压缩 + 二进制解包）自动识别其所属数据中心（如 `DC2`、`DC5`），无头像时展示 `null`。
- **🚫 403 故障识别（屏蔽 vs 注销）**：
  - 管理员发送消息失败时，智能细分并明确告警：
    - `bot was blocked by the user` $\rightarrow$ 提示“对方已将 Bot 屏蔽，消息无法送达”。
    - `user is deactivated` $\rightarrow$ 提示“对方账号已注销，消息无法送达”。
- **🔨 黑名单管理 (`/ban` & `/unban`)**：
  - 支持回复消息或指定 UID 进行封禁/解封。
  - 支持填写封禁理由（如 `/ban 广告骚扰`）；若不填理由则只发送默认封禁提醒。
  - 被封禁用户的后续消息将在边缘入口直接拦截，不再打扰管理员。
- **⏱️ 消息频率限制 (Rate Limiting)**：
  - 边缘级滑动窗口限频（10 秒最多 5 条消息），超限自动提示并拦截，有效抵御脚本刷屏。
- **🧰 工具箱命令 (`/info`)**：
  - 管理员回复任意用户消息发送 `/info`，随时重新调出该用户的详细资料、数据中心与账号状态。
- **🔐 零泄露 Secrets 安全体系**：
  - 代码库中不包含任何敏感 Token 或 UID，配置文件已与密钥完全解耦。
  - 敏感凭据（`BOT_TOKEN`、`ADMIN_UID`、`TG_WEBHOOK_SECRET`）全部采用 Cloudflare Worker Secrets 加密保存。
  - 自动校验 Telegram `X-Telegram-Bot-Api-Secret-Token` 请求头，杜绝外部伪造 Webhook。

---

## 📐 交互架构流程

```mermaid
sequenceDiagram
    autonumber
    actor User as 访客用户
    participant TG as Telegram API
    participant Worker as Cloudflare Worker (Python)
    participant KV as Workers KV (pmbot_captcha)
    actor Admin as 管理员

    User->>TG: 发送私聊消息
    TG->>Worker: Webhook (Update)
    Worker->>KV: 检查封禁与频控 (banned / ratelimit)
    Worker->>KV: 检查验证状态 verified:{uid}
    alt 未通过验证
        Worker->>User: 下发 ASCII 等宽点阵验证码
        User->>Worker: 回复 2 位数字验证码
        Worker->>KV: 写入 verified:{uid} = true
        Worker->>User: 打上 👌 表情
        Worker->>Admin: 推送用户详细名片 (昵称/UID/DC)
        Worker->>Admin: 转发用户首次发送的消息
    else 已通过验证
        Worker->>Admin: forwardMessage (直接转发，保留名片)
        Worker->>User: 打上 👌 表情 (投递成功)
        Worker->>KV: 记录双方消息映射 (a2u / u2a, TTL 30天)
    end

    Note over Admin, Worker: 管理员回复
    Admin->>TG: 【回复】用户的某条转发消息
    TG->>Worker: Webhook (Update)
    Worker->>KV: 查询该消息映射 (a2u)
    alt 目标用户有效
        Worker->>User: copyMessage (无引用转发，带引用原消息)
        Worker->>Admin: 打上 👌 表情 (投递成功)
        Worker->>KV: 记录该回复的双方映射
    else 发送失败 (403 屏蔽/注销)
        Worker->>Admin: 精准提示：对方已屏蔽 Bot / 账号已注销
    end
```

---

## 📋 BotFather 指令设置清单

在 [@BotFather](https://t.me/BotFather) 中使用 `/setcommands` 命令，复制粘贴以下文本即可配置快捷指令提示：

```text
start - 开始使用并进行人机验证
help - 查看帮助说明与管理员指令
ban - 封禁用户 (仅管理员)
unban - 解封用户 (仅管理员)
info - 查看用户完整名片与状态 (仅管理员)
```

---

## 🚀 快速部署指南

### 1. 前置准备
- 一个 [Cloudflare](https://dash.cloudflare.com/) 账号并安装 [Wrangler CLI](https://developers.cloudflare.com/workers/wrangler/install-and-update/)：
  ```bash
  npm install -g wrangler
  wrangler login
  ```
- 从 [@BotFather](https://t.me/BotFather) 获取你的 Telegram Bot Token。
- 获取你的 Telegram 账号数字 UID（可通过 [@userinfobot](https://t.me/userinfobot) 查询）。

### 2. 创建 Workers KV 命名空间
在项目根目录运行以下命令创建 KV：
```bash
wrangler kv namespace create pmbot_captcha
```
控制台将输出该 KV 的 `id`，将其填入 `wrangler.toml` 中的 `[[kv_namespaces]]` 区域：
```toml
[[kv_namespaces]]
binding = "pmbot_captcha"
id = "你的_KV_ID"
```

### 3. 配置加密 Secrets
无需在配置文件中编写明文凭据，通过命令行安全注入：
```bash
# 1. 注入 Bot Token
wrangler secret put BOT_TOKEN
# 提示输入时粘贴你的 Token，如：<YOUR_BOT_TOKEN>

# 2. 注入 管理员 UID
wrangler secret put ADMIN_UID
# 提示输入时粘贴你的 UID，如：<YOUR_TELEGRAM_UID>

# 3. 注入 Webhook 鉴权密钥 (可选但强烈推荐)
wrangler secret put TG_WEBHOOK_SECRET
# 输入一段自定义的高强度随机字符串，用于验证请求来自 Telegram
```

### 4. 部署 Worker
执行一键部署：
```bash
wrangler deploy
```
部署成功后，Wrangler 会给出你的 Worker 域名（例如：`https://tg-pmbot.<your-subdomain>.workers.dev`）。

### 5. 绑定 Telegram Webhook
使用 `curl` 设置 Webhook（将 `<BOT_TOKEN>`、`<WORKER_URL>`、`<WEBHOOK_SECRET>` 替换为你的真实值）：
```bash
curl -F "url=https://<WORKER_URL>" \
     -F "secret_token=<WEBHOOK_SECRET>" \
     https://api.telegram.org/bot<BOT_TOKEN>/setWebhook
```
当接口返回 `{"ok":true,"result":true,"description":"Webhook was set"}` 时，即代表配置成功！🎉

---

## 💡 管理员操作指南

1. **回复用户**：在 Bot 窗口中，直接**引用回复（Reply）**用户的任意消息，Bot 会自动无缝传递给对方。
2. **封禁用户**：
   - 快捷回复封禁：直接回复某条转发消息输入 `/ban`（默认提示）或 `/ban 广告刷屏`（带理由）。
   - 指定 UID 封禁：输入 `/ban 123456789 广告刷屏`。
3. **解封用户**：回复消息发送 `/unban`，或输入 `/unban 123456789`。
4. **查阅资料**：回复某条消息发送 `/info`，随时查询该用户 UID、昵称、用户名、DC 节点与封禁状态。

---

## 📂 项目结构

```text
TG_PMbot/
├── src/
│   └── entry.py          # Python Worker 核心代码 (含 Captcha、DC 解码、双向映射、Reaction、403识别、Ban)
├── .gitignore            # Git 忽略规则 (防误传本地缓存)
├── LICENSE               # MIT 开源协议
├── README.md             # 项目完整文档
├── wrangler.toml         # Wrangler 生产部署配置 (无敏感明文)
└── wrangler.toml.example # 开源示例配置文件
```

---

## 📜 许可证

本项目基于 [MIT License](LICENSE) 开源发布。
