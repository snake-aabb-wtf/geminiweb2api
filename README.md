# gemini2api v1.0

> 把 Google Gemini 网页版 API 包装成 OpenAI 兼容接口。
> 通过浏览器 HAR 文件提取认证参数,启动一个本地代理服务器。

**v1.0 是一次较大重构**:全面修复了 v0.3 的安全 / 并发 / 正确性问题,新增鉴权、限流、真正流式、Token 统计、多模态、Function calling、WebUI 重做、51 个测试。详见 [CHANGELOG.md](CHANGELOG.md)。

---

## ✨ v1.0 新增能力

| 类别 | 能力 |
| --- | --- |
| **安全** | Bearer Token 鉴权中间件、Admin Cookie 登录、CORS 收紧、WebUI XSS 修复 |
| **稳定性** | 账号池 `asyncio.Lock`、每账号并发/速率双限流、自动重试 + 指数退避、`.env` 热加载 |
| **协议** | 真正 SSE 增量流式、Token usage 解析、多模态图片、Function calling 透传 |
| **运维** | 结构化 logging(滚动文件)、SSE 实时日志页、账号池 `.env` 持久化 |
| **测试** | 51 个 pytest 单元/集成测试,覆盖协议、池、鉴权、限流、HAR、API |
| **UI** | 全新深色 SPA(原生 JS+HTMX),5s 自动刷新,登录页+账号添加表单 |

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

要求 Python 3.10+。Windows 用户直接双击 `start.bat`。

### 2. 导出 HAR 并生成配置

在浏览器登录 [gemini.google.com](https://gemini.google.com),打开 DevTools → Network 面板,刷新页面,右键任意 `StreamGenerate` 请求 → "Save all as HAR with content"。

然后运行 GUI:

```bash
python config_tool.py
```

* 选择 HAR → 点击"解析" → 输入账号名 → 点击"保存到 .env"。
* 可选:点击"启动代理服务器"实时查看日志。

### 3. 启动服务

```bash
python server.py          # 默认 0.0.0.0:1800
python server.py 19000    # 自定义端口
```

启动后:

* 浏览器打开 [http://localhost:1800/](http://localhost:1800/) → 登录管理面板(默认鉴权关闭时直接进入)。
* 客户端把 base URL 设为 `http://localhost:1800/v1`,API key 与 `ADMIN_KEY` 相同(留空则不校验)。

---

## 🔐 鉴权

`.env` 中:

```ini
# 设为任意非占位值即启用
API_KEY=sk-my-secret
ADMIN_KEY=sk-my-admin-secret
```

* `API_KEY` 校验 `/v1/*` 端点(OpenAI 客户端兼容)。
* `ADMIN_KEY` 校验 `/api/*` 和 WebUI 页面(浏览器登录页 `/webui/login`)。
* 留空 / 设为 `sk-web2api-placeholder` / `off` / `disabled` 即禁用,仅供本地开发。

**生产环境务必设置。** 服务启动时会在日志和 dashboard 顶部显示鉴权状态。

---

## ⚙️ 关键配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `1800` | 监听端口 |
| `API_KEY` | placeholder | `/v1/*` 鉴权 |
| `ADMIN_KEY` | 同 `API_KEY` | WebUI + `/api/*` 鉴权 |
| `CORS_ORIGINS` | (空) | 逗号分隔;留空不启用 CORS |
| `ROTATION_STRATEGY` | `least-recently-used` | `round-robin` / `random` / `first` |
| `MAX_ERRORS_BEFORE_DISABLE` | `3` | 账号连续失败 N 次后自动停用 |
| `GLOBAL_RATE_LIMIT_RPM` | `0` | 全局每 IP 每分钟限流;0=关闭 |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FILE` | `gemini_proxy.log` | 日志文件,空字符串禁用 |
| `PERSIST_ACCOUNTS` | `1` | WebUI 修改是否自动写回 .env |
| `PROFILES` | (空) | 逗号分隔的模型 profile 名 |
| `MODEL_FAMILY_<name>` | `1` | 1=Flash, 3=Pro, 6=Flash Lite |
| `THINKING_MODE_<name>` | `1` | 1=标准, 2=进阶 |
| `DEFAULT_MODEL` | 第一个 profile | `model` 字段缺省值 |

每账号:

| 变量 | 说明 |
| --- | --- |
| `ACCOUNT_<name>_F_SID` | 会话 ID |
| `ACCOUNT_<name>_AT` | 认证令牌(数小时有效) |
| `ACCOUNT_<name>_SN_PARAM` | SN 加密令牌(每次响应刷新) |
| `ACCOUNT_<name>_BL_PARAM` | 构建版本(版本更新时变) |
| `ACCOUNT_<name>_HL` | 语言,默认 `zh-CN` |
| `ACCOUNT_<name>_UUID` | JSPB 索引 16 |
| `ACCOUNT_<name>_HASH` | JSPB 索引 4 |
| `ACCOUNT_<name>_ENABLED` | `true` / `false` |
| `ACCOUNT_<name>_MODELS` | 绑定的 profile 列表,逗号分隔 |
| `ACCOUNT_<name>_RATE_LIMIT_RPM` | 单账号每分钟限流,默认 60 |
| `ACCOUNT_<name>_MAX_CONCURRENT` | 单账号并发上限,默认 4 |
| `ACCOUNT_<name>_HEADER_<name>` | 自定义请求头(原 HAR 复制) |

---

## 📡 API

### OpenAI 兼容

* `POST /v1/chat/completions` — 完全兼容 OpenAI 协议
  * `model`: profile 名
  * `messages`: OpenAI 格式
  * `stream`: `true` / `false`
  * `temperature`, `max_tokens`, `top_p`: 透传
  * `tools`, `tool_choice`: function calling
  * `messages[].content`: 字符串 或 `[{"type": "text"|"image_url", ...}]`
* `GET /v1/models` — 模型列表

### 管理 API(`/api/*`,需要 `ADMIN_KEY`)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats` | 池统计 + 账号详细状态 |
| GET | `/api/accounts` | 账号列表 |
| POST | `/api/accounts` | 创建账号(JSON) |
| PATCH | `/api/accounts/{name}` | 修改 enabled/bound/限流 |
| DELETE | `/api/accounts/{name}` | 删除账号 |
| PUT | `/api/accounts/{name}/toggle` | 快速启停(legacy) |
| GET | `/api/profiles` | 模型 profile 列表 |
| POST | `/api/accounts/from-har` | 表单上传 HAR,返回提取的凭证(不直接入库) |
| GET | `/api/events/stream` | **SSE** 实时日志推送 |
| POST | `/api/auth/web_login` | 浏览器登录,设置 admin cookie |
| POST | `/api/auth/logout` | 清除 cookie |

### WebUI

* `/` — 仪表盘(8 张统计卡 + 自动 5s 刷新)
* `/webui/accounts` — 账号管理(表格 + 增删改 + HAR 导入)
* `/webui/logs` — 实时日志(SSE)
* `/webui/login` — 登录页

---

## 🧪 测试

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

覆盖范围:

* `test_build_jspb_header.py` — 协议头 17 元素形状
* `test_build_request_body.py` — 双层 JSON 编码 + padding 81
* `test_parse_response.py` — 主答案/思考分离/工具调用/Token 解析
* `test_account_pool.py` — LRU/random/限流/并发/in-flight/持久化
* `test_auth.py` — 占位禁用/启用/缺失/错误/正确 key
* `test_rate_limit.py` — 桶容量/独立 key/forwarded-for
* `test_har_parser.py` — URL/JSPB/body 三路字段提取
* `test_api.py` — `/health`、`/v1/models`、鉴权 + 快乐路径(用 fake `send_request` 注入)

---

## ⚠️ 已知限制

* **协议风险**:基于逆向的 `/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate` 端点,Google 改版可能失效。
* **多轮对话**:仅取最后一条 user message;无 conversation memory。
* **Function calling**:Gemini Web 不保证工具调用可用;实现是 best-effort。
* **多模态图片**:走 `UploadFile` 端点,失败时降级为"忽略图片 + 仅文本"。
* **Cookie/凭证有效期**:`at` 数小时、`f.sid` 数天、`sn_param` 每次响应更新。失效后用 HAR 重新提取。

---

## 🗂 项目结构

```
geminiweb2api/
├── server.py              # FastAPI 入口、路由、鉴权、流式处理
├── adapter.py             # Gemini 协议适配(JSPB 头、双层 body、响应解析、限流)
├── account_pool.py        # 账号池(线程安全)、持久化、HAR 解析入口
├── auth.py                # Bearer/admin 鉴权
├── rate_limit.py          # 令牌桶限流
├── logger.py              # 结构化日志
├── har_parser.py          # HAR → 凭证提取
├── config_tool.py         # Tkinter GUI(.env 生成 + 子进程管理)
├── start.bat              # Windows 启动脚本
├── requirements.txt       # pin 版本依赖
├── .env.example           # 配置模板
├── pytest.ini             # pytest 配置
├── CHANGELOG.md           # 版本变更日志
├── templates/             # WebUI 模板
│   ├── dashboard.html
│   ├── accounts.html
│   ├── logs.html
│   └── login.html
├── static/                # 静态资源
│   ├── style.css
│   └── app.js
└── tests/                 # 单元 + 集成测试
    ├── conftest.py
    └── test_*.py (8 个文件,51 用例)
```

---

## 📜 License

Unlicense — public domain.
