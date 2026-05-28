# Gemini2API

将 Google Gemini 网页版逆向为 **OpenAI 兼容 API**，支持 OpenAI SDK 直接接入。

只需从浏览器导出一个 `.har` 文件，即可自动提取认证参数，启动一个与 OpenAI 兼容的代理服务器。

## 原理

```
浏览器 F12 Network 导出 .har 文件
        │
        ▼
config_tool.py — GUI 配置工具
        ├─ 解析 HAR 文件
        ├─ 提取 Gemini 认证参数（at, f.sid, sn_param ...）
        └─ 保存到 .env
        │
        ▼
server.py — FastAPI 代理服务器
        ├─ /v1/chat/completions  ← OpenAI 格式
        ├─ /v1/models
        └─ /health
        │
        ▼
任何 OpenAI SDK 客户端均可使用
```

## 特性

| 特性 | 支持 |
|------|------|
| OpenAI 兼容 API | ✅ |
| 流式 / 非流式 | ✅ |
| HAR 自动解析 | ✅ |
| GUI 配置工具 | ✅ |
| .env 双段配置（鉴权 / 服务器） | ✅ |
| 一键启动 | ✅ |

## 快速开始

### 前置条件

- Python 3.10+
- Google 账号（已登录 Gemini）

### 安装

```bash
pip install fastapi uvicorn httpx python-dotenv pydantic
```

### 1. 捕获 HAR 文件

1. 打开 Chrome，按 `F12` 进入 DevTools
2. 切换到 **Network** 面板
3. 勾选 **Preserve log**
4. 访问 [https://gemini.google.com](https://gemini.google.com)，**确保已登录**
5. 发送一条聊天消息，等待 AI 回复完成
6. 在 Network 面板右键任意请求 → **Save all as HAR with content**
7. 保存为 `.har` 文件

### 2. 运行配置工具

```bash
python config_tool.py
```

1. 点击 **浏览...** 选择刚才保存的 `.har` 文件
2. 点击 **解析** — 自动提取认证参数
3. 点击 **保存到 .env**
4. 点击 **启动代理服务器**

### 3. 一键启动（后续使用）

```bash
start.bat
```

服务器默认运行在 `http://localhost:1800`（可在 .env 中修改 `PORT=`）。

### 4. 测试

```bash
curl http://localhost:1800/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}]}'
```

### 5. 接入 OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:1800/v1",
    api_key="sk-web2api-placeholder",
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

或 Claude Code：

```bash
set OPENAI_API_BASE=http://localhost:1800/v1
set OPENAI_API_KEY=sk-web2api-placeholder
claude
```

## 项目结构

```
Gemini2API/
├── config_tool.py      # GUI 配置工具（选择 HAR → 解析 → 保存 .env）
├── server.py           # FastAPI 代理服务器
├── adapter.py          # Gemini API 适配器（请求转换 / 响应解析）
├── har_parser.py       # HAR 文件解析器
├── start.bat           # 一键启动脚本
├── .env                # 配置文件（自动生成）
└── README.md
```

## 配置说明

`.env` 分为两段：

### 不易变部分 — 服务器配置

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `MODEL_NAME` | 模型名称 | `gpt-4o` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `PORT` | 监听端口 | `1800` |
| `API_KEY` | API 密钥 | `sk-web2api-placeholder` |
| `DSML_ENABLED` | 工具调用支持 | `false` |

### 异变部分 — 账号鉴权凭证

由 `config_tool.py` 自动提取并更新，包含 `F_SID`、`AT`、`SN_PARAM`、`BL_PARAM`、`HL` 等。

> **注意**：这些参数有时效性。`at` 有效期数小时，`f.sid` 有效期数天。过期后需重新捕获 HAR 文件并运行配置工具更新。

## 认证参数过期处理

| 参数 | 有效期 | 说明 |
|------|--------|------|
| `at` | 数小时 | 会话级别，需重新捕获 HAR |
| `f.sid` | 数天 | 会话 ID |
| `sn_param` | 每次响应刷新 | 加密令牌 |
| `bl` | 版本更新 | 构建版本号 |

过期后只需重新 F12 → 保存 HAR → 运行 `config_tool.py` 更新即可。

## 文件说明

| 文件 | 功能 |
|------|------|
| `config_tool.py` | GUI 配置工具，选择 HAR 文件并自动提取认证参数到 .env |
| `server.py` | FastAPI 代理服务器，提供 OpenAI 兼容接口 |
| `adapter.py` | Gemini API 适配器，处理请求格式转换和响应解析 |
| `har_parser.py` | HAR 文件解析，自动识别 Gemini API 调用 |
| `start.bat` | 一键启动脚本 |

## 注意事项

- 仅支持文本对话，不支持多模态（图片/文件）
- 每次请求独立会话，不维护多轮上下文
- 需要 Chrome/Edge 浏览器导出 HAR 文件
- Python 3.10+ 环境

## License

Unlicense — 公有领域。详见 [LICENSE](./LICENSE)。

## 免责协议

本软件仅供**学习研究和技术交流**使用。

使用本软件时，您需自行承担一切法律责任：

1. **账号风险**：使用本软件可能导致 Google 账号被限制或封禁，由使用者自行承担。
2. **合规责任**：使用者应确保使用方式符合目标网站的服务条款及当地法律法规。
3. **用途限制**：禁止将本软件用于任何违反法律法规、侵犯他人权益或商业牟利的用途。
4. **无担保**：本软件按"原样"提供，不提供任何明示或暗示的担保。

通过使用本软件，即表示您已阅读、理解并同意以上条款。如不同意，请立即停止使用并删除本软件。
