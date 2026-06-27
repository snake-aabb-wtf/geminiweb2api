# Changelog

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
