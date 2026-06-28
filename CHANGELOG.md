# Changelog

## [1.1.0] - 2026-06-28

### 🐛 Bug fixes
- **Profile 温度覆盖竞态** — 每请求 `temperature` / `max_tokens` 改用 `dataclasses.replace` 浅拷贝,不再污染共享 `ModelProfile`。
- **`stats()` 未持锁** — `AccountPool.stats()` 现 `async with self._lock`,与 `select` / `record_*` 互斥。
- **LRU 冷启动总是选第一个** — `min` 加 `random.random()` 打破 `last_used` 平局。
- **`recent_requests` 过期清理** — `select` 入口 `popleft` 到 60s 窗口外,O(过期数) 而非 O(128)。
- **CHAT_MAX_RETRIES 独立配置** — 之前复用 `MAX_ERRORS_BEFORE_DISABLE` 语义错位,现独立环境变量,默认 2。
- **`stats_sync` 加锁** — GUI / 调试用同步路径补齐锁语义。
- **`assert chosen is not None` 收窄 mypy** — 修 round-robin 类型推断。
- **SSE 死连接清理** — `_InMemoryLogHandler.mark_dead` / `force_remove` 跟踪 dead 队列,enqueue 时 30s grace 后剪除。

### ✨ New features
- **`/v1/embeddings` stub** — 开启 `GEMINI_EMBEDDINGS_ENABLED=1` 后返回 768 维零向量 + `X-Gemini2api-Status: stub` 头;默认关闭,免烧真实账号。
- **多 API Key 支持** — `API_KEYS=sk-a,sk-b,sk-c` 与 `API_KEY` 共存;admin 路径 `ADMIN_KEYS` 同样支持。`/health` 暴露 `api_key_count` / `admin_key_count`。
- **24h Token 用量图** — `account_pool.usage_log` 每分钟 1 个 `UsageTick`(24h*60 上限),`record_usage` 在 `_handle_blocking` 成功时调用;`GET /api/usage?hours=24` 返回时序 + 汇总。`templates/dashboard.html` 已就位,后续 PR 接入 Chart.js。
- **账号健康自检** — `POST /api/health/accounts` 并发 ping 所有 enabled 账号,返回 `[{name, model, ok, status_code, latency_ms, error}]`,超时 30s。
- **PII 脱敏** — `logger.scrub_pii` 替换 email / 中国手机号 / 身份证号;`LOG_SCRUB_PII=1` 启用,在 `request_started` 日志头 120 字符应用。
- **`upload_image` 真接入 server** — v1.0 写但没调用的 `adapter.upload_image` 现在被 `chat_completions` 实际调用,失败的图片打 `upload_skipped` warning 但不阻塞请求。
- **流式失败换号重试** — `_handle_stream` 在 **首个 chunk 之前** 探测失败换号,最多 `CHAT_MAX_RETRIES` 次;已发过 chunk 则不换号(避免协议分裂)。

### 🛠 Tooling
- **Dockerfile** + **docker-compose.yml** + **.dockerignore** — 一行 `docker compose up -d` 起服务;非 root 用户、tini PID 1、`/health` 健康检查。
- **mypy 严格模式** — `mypy.ini` 通过 7 个核心模块 (`server.py` 仍开启 per-module 放松),CI 加 mypy step。
- **CI 升级** — 新增 `mypy` job 和 `docker` build+smoke test job。
- **CI 持久 autouse fixture** — `conftest.py` 自动 reload `auth` 模块,杜绝测试间 `monkeypatch.setenv` 残留导致跨测试 401。

### 📊 Tests
- 51 → **87 用例**(新增 36):
  - `test_account_pool.py` +7: LRU 冷启动平局、并发 stats、recent_requests 过期、usage_log 累积、按分钟分桶、usage_series 空分钟、round-robin
  - `test_api.py` 1 个稳定
  - `test_auth_multi.py` 6 个新
  - `test_embeddings.py` 3 个新
  - `test_health_accounts.py` 2 个新
  - `test_har_parser.py` 5 个原有
  - `test_scrub_pii.py` 8 个新
  - `test_server_temperature.py` 1 个新
  - `test_sse_bridge.py` 4 个新
  - `test_upload_image_integration.py` 3 个新
  - `test_usage_endpoint.py` 2 个新

### ⚠️ Notes
- 协议仍基于逆向工程的 `StreamGenerate` 端点。
- 多模态和 Function calling 仍是 best-effort;`upload_image` 失败的图片会被丢弃并打日志。
- Docker 镜像基于 `python:3.12-slim`,约 150MB。

---

## [1.0.0] - 2026-06-27

### 🐛 Bug fixes
- **API_KEY 鉴权缺失** — 新增 `verify_api_key` 中间件保护 `/v1/*`。占位符值(空、`sk-web2api-placeholder` 等)即禁用,生产环境必须设非占位值。
- **全局 `_reqid` 竞态** — 改为每请求局部 `reqid`,并发请求不再互相覆盖。
- **账号池无锁** — `AccountPool.select` / `record_*` 全部加 `asyncio.Lock`,并发下 `last_used` / `error_count` / `inflight` 不再失序。
- **WebUI XSS** — 账号名、profile 名、状态文本经 `html.escape` 注入到模板。
- **CORS 反模式** — 移除 `allow_origins=["*"]` + `allow_credentials=True`,改为可选 `CORS_ORIGINS` 环境变量。
- **`parse_response` 取最长错位** — 改为取第一个非空候选(主答案),第二候选进 `reasoning_content`。
- **`stream_request` 假流式** — 用 `httpx.AsyncClient.stream` 接收 + 服务端分块到 SSE 客户端。
- **每次新建 httpx client** — 进程级共享 client,省去每次 TLS 握手。
- **timeout 不分项** — `httpx.Timeout(connect=10, read=90, write=30, pool=10)`,流式 read 180s。
- **`except: pass` 吞噬异常** — 所有 `except` 改为具体类型,警告可见。
- **重试无退避** — 5xx / 401 / 403 / 429 加 1s/2s/4s/8s 指数退避。
- **流式无重试** — 流式路径预选账号失败时回退到 `send_request`。
- **HAR 解析死代码** — 删 `_find_content_path`、`template_params`、`all_endpoints`。
- **start.bat 端口 typo** — `18000` → `1800`,并加 `where python` 预检。
- **Pydantic 别名** — 删 `PydanticBase` 别名,统一 v2 strict 模式。
- **requirements.txt 版本** — 全部 pin 到兼容区间,新增 `pytest` / `pytest-asyncio`。
- **`_reqid` 初值 7 位回绕** — 改用 28 位微秒时间窗。

### ✨ New features
- **Bearer Token 鉴权中间件** — `Authorization: Bearer <key>` 或 `x-api-key` header;admin 路径还支持 cookie。
- **Admin 登录页** — `/webui/login` 浏览器登录,1 小时 cookie。
- **真正流式** — 服务端分块 `delta` 事件,OpenAI 协议兼容;附带尾部 `usage` chunk。
- **Token usage 统计** — 从 `inner_data[2]._mtokenCount/_ttokenCount` 提取,`response.usage` 不再全是 0。
- **多模态(图片)输入** — `image_url` 支持 `data:` URI、HTTP(S) URL、本地路径;走 Gemini `UploadFile` 端点。
- **Function calling / Tools** — `tools` / `tool_choice` 字段;响应解析时检测 function_call 转 OpenAI `tool_calls`。
- **账号池限流** — 每账号 `rate_limit_rpm` + `max_concurrent`,LRU 候选自动排除超限账号。
- **账号池持久化** — WebUI 增删改后写回 `.env`(原子 rename);后台 `_env_watcher` 每 5s 检测外部编辑并热加载。
- **结构化 logging** — `logger.py` 统一,DEBUG/INFO/WARNING/ERROR 分级,输出 stderr + `gemini_proxy.log` 滚动文件;`extra` 自动 namespace-prefix。
- **SSE 实时日志** — `/api/events/stream` 推送日志事件,`/webui/logs` 页面实时显示。
- **HAR 导入端点** — `/api/accounts/from-har` 接受 HAR 文本,返回解析结果,前端再确认保存。
- **Token Bucket 限流中间件** — `GLOBAL_RATE_LIMIT_RPM` 控制单 IP 全局速率;支持 `x-forwarded-for`。
- **WebUI 全面重做** — 统一 `static/style.css` + `static/app.js`,深色主题,响应式,5s 自动刷新统计。
- **Pydantic v2 严格** — `extra="ignore"`,字段 validator(账号名 `^[A-Za-z0-9_.-]{1,64}$`、role 白名单、温度区间)。
- **51 个单元/集成测试** — 覆盖 `parse_response`、`build_jspb_header`、`build_request_body`、`account_pool.select/release/限流/持久化`、`auth.verify_*`、`rate_limit`、`har_parser`、`/health`、`/v1/models`、`/v1/chat/completions` 鉴权+快乐路径。
- **HAR 解析合并** — GUI `config_tool.py` 不再重复实现,统一调用 `har_parser.parse_har`。
- **GUI 启动 stdout 实时显示** — `subprocess.Popen(stdout=PIPE)` + 线程读取 → Tkinter Text 实时显示;启动前 socket 端口占用预检。

### ⚠️ Notes
- 协议仍基于逆向工程的 `/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate`,Google 改版可能失效。
- 多模态和 Function calling 是 best-effort;Gemini Web 端不保证稳定支持。
- 默认 0 账号启动,首次使用必须通过 WebUI 或 `config_tool.py` 导入 HAR 凭证。
