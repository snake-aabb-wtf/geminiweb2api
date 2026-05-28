import json
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse, parse_qs, unquote

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SELF_DIR, ".env")

FIELD_LABELS = {
    "base_url": "目标地址",
    "chat_endpoint": "聊天端点",
    "cookies": "Cookie",
    "auth_header": "Authorization",
    "auth_type": "认证类型",
    "is_streaming": "流式支持",
    "has_websocket": "WebSocket",
    "has_pow": "PoW 挑战",
    "bl_param": "bl 参数",
    "f_sid": "f.sid",
    "hl": "语言",
    "at": "at 令牌",
    "sn_param": "sn 参数",
    "supported_params": "支持的参数",
    "content_field_path": "内容字段路径",
    "header_count": "请求头数量",
}


def parse_har_file(har_path: str) -> dict:
    with open(har_path, "r", encoding="utf-8") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])

    info = {"_errors": [], "_warnings": []}

    chat_entry = None
    chat_idx = -1
    for idx, entry in enumerate(entries):
        url = entry.get("request", {}).get("url", "")
        if "StreamGenerate" in url:
            chat_entry = entry
            chat_idx = idx
            break

    if not chat_entry:
        info["_errors"].append("未找到 Gemini StreamGenerate 端点，将使用第一个 POST 请求")
        for idx, entry in enumerate(entries):
            if entry.get("request", {}).get("method") == "POST":
                chat_entry = entry
                chat_idx = idx
                break

    if not chat_entry:
        info["_errors"].append("HAR 文件中没有 POST 请求")
        return info

    req = chat_entry.get("request", {})
    resp = chat_entry.get("response", {})
    url = req.get("url", "")
    parsed = urlparse(url)

    info["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
    info["chat_endpoint"] = parsed.path
    info["har_path"] = har_path

    qs = parse_qs(parsed.query)
    info["bl_param"] = qs.get("bl", [""])[0]
    info["f_sid"] = qs.get("f.sid", [""])[0]
    info["hl"] = qs.get("hl", ["zh-CN"])[0]

    for h in req.get("headers", []):
        if h.get("name", "").lower() == "cookie":
            info["cookies"] = h.get("value", "")
        if h.get("name", "").lower() == "authorization":
            info["auth_header"] = h.get("value", "")

    header_list = req.get("headers", [])
    info["header_count"] = len(header_list)

    post_data = req.get("postData", {})
    body_text = post_data.get("text", "")

    info["at"] = ""
    info["sn_param"] = ""
    if body_text:
        at_match = re.search(r"[?&]at=([^&]+)", url)
        if at_match:
            info["at"] = at_match.group(1)
        else:
            at_match = re.search(r"&at=([^&]+)", body_text)
            if at_match:
                info["at"] = at_match.group(1)

        if "f.req=" in body_text:
            parsed_body = parse_qs(body_text)
            freq_enc = parsed_body.get("f.req", [""])[0]
            freq_dec = unquote(freq_enc)
            try:
                freq_json = json.loads(freq_dec)
                inner_raw = freq_json[1]
                inner = json.loads(inner_raw)
                if len(inner) > 3:
                    sn = inner[3]
                    if isinstance(sn, str) and len(sn) > 10:
                        info["sn_param"] = sn
                if isinstance(inner[0], list) and len(inner[0]) > 1:
                    conv_id = inner[0][1]
                    if conv_id is not None:
                        info["_first_conv_id"] = str(conv_id)
            except (json.JSONDecodeError, IndexError, TypeError) as e:
                info["_warnings"].append(f"解析 f.req 失败: {e}")

    resp_text = resp.get("content", {}).get("text", "")
    info["is_streaming"] = False
    for h in resp.get("headers", []):
        if h.get("name", "").lower() == "content-type":
            if "text/event-stream" in h.get("value", ""):
                info["is_streaming"] = True

    if resp_text and resp_text.startswith(")]}'"):
        info["_has_data"] = True
        lines = resp_text.strip().split("\n")
        for i in range(0, len(lines), 2):
            if i + 1 >= len(lines):
                continue
            dl = lines[i + 1].strip()
            if not dl:
                continue
            try:
                outer = json.loads(dl)
                if isinstance(outer, list) and len(outer) > 0:
                    wrb = outer[0]
                    if isinstance(wrb, list) and len(wrb) >= 3 and isinstance(wrb[2], str):
                        inner_data = json.loads(wrb[2])
                        if isinstance(inner_data, list) and len(inner_data) > 1:
                            id_pair = inner_data[1]
                            if isinstance(id_pair, list) and len(id_pair) >= 2:
                                info["_conv_id"] = id_pair[0]
                                info["_resp_id"] = id_pair[1]
                        if len(inner_data) > 2 and isinstance(inner_data[2], dict):
                            val26 = inner_data[2].get("26")
                            if isinstance(val26, str) and val26:
                                info["_token26"] = val26
                        if len(inner_data) > 4 and isinstance(inner_data[4], list):
                            for item in inner_data[4]:
                                if isinstance(item, list) and len(item) >= 2:
                                    rc_id = item[0]
                                    if isinstance(rc_id, str) and rc_id.startswith("rc_"):
                                        info["_rc_id"] = rc_id
                                    cp = item[1]
                                    if isinstance(cp, list) and cp:
                                        txt = cp[0]
                                        if isinstance(txt, str) and len(txt) > 5:
                                            info["_response_preview"] = txt[:200]
                                            break
            except (json.JSONDecodeError, IndexError, TypeError):
                continue

    info["has_websocket"] = False
    for entry in entries:
        if entry.get("response", {}).get("status") == 101:
            info["has_websocket"] = True
            break

    info["has_pow"] = False
    for entry in entries:
        path = urlparse(entry.get("request", {}).get("url", "")).path
        if re.search(r"(challenge|pow|turnstile)", path, re.I):
            info["has_pow"] = True
            break

    info["auth_type"] = "none"
    if info.get("auth_header"):
        info["auth_type"] = "oauth"
    if info.get("has_pow"):
        info["auth_type"] = "pow"
    if info.get("cookies"):
        if not info.get("auth_header"):
            info["auth_type"] = "cookie"

    # Parse JSPB header for model selection fields
    for h in req.get("headers", []):
        if h.get("name", "").lower() == "x-goog-ext-525001261-jspb":
            try:
                jspb = json.loads(h.get("value", "[]"))
                if isinstance(jspb, list):
                    if len(jspb) > 14 and isinstance(jspb[14], int):
                        info["model_family"] = jspb[14]
                    if len(jspb) > 15 and isinstance(jspb[15], int):
                        info["thinking_mode"] = jspb[15]
                    if len(jspb) > 16 and isinstance(jspb[16], str):
                        info["session_uuid"] = jspb[16]
                    if len(jspb) > 4 and isinstance(jspb[4], str):
                        info["request_hash"] = jspb[4]
            except (json.JSONDecodeError, TypeError):
                pass

    return info


PROFILE_FIELDS = [
    "MODEL_FAMILY", "THINKING_MODE", "F_SID", "AT",
    "SN_PARAM", "BL_PARAM", "HL", "UUID", "HASH",
]

SERVER_FIELDS = {"HOST", "PORT", "API_KEY", "MODEL_NAMES", "DEFAULT_MODEL"}


def read_existing_env() -> dict:
    result = {}
    if not os.path.exists(ENV_PATH):
        return result
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def update_env_with_profile(auth_info: dict, model_name: str) -> str:
    existing = read_existing_env()

    # Build server config section (keep existing, or use defaults)
    server_lines = []
    defaults = {"HOST": "0.0.0.0", "PORT": "1800", "API_KEY": "sk-web2api-placeholder"}
    for f in ("HOST", "PORT", "API_KEY"):
        val = existing.get(f, defaults[f])
        server_lines.append(f"{f}={val}")

    # Update MODEL_NAMES list - add model_name if not present
    existing_names = set()
    raw_names = existing.get("MODEL_NAMES", "")
    if raw_names:
        existing_names = set(n.strip() for n in raw_names.split(",") if n.strip())
    existing_names.add(model_name)
    server_lines.append(f"MODEL_NAMES={','.join(sorted(existing_names))}")

    default = existing.get("DEFAULT_MODEL", model_name)
    server_lines.append(f"DEFAULT_MODEL={default}")
    server_lines.append("")

    # Build profile section
    suffix = f"_{model_name}"
    profile_lines = []
    # model_family/thinking_mode from JSPB
    mf = auth_info.get("model_family", 1)
    tm = auth_info.get("thinking_mode", 1)
    profile_lines.append(f"# Profile: {model_name}  (family={mf}, thinking={tm})")
    profile_lines.append(f"MODEL_FAMILY{suffix}={mf}")
    profile_lines.append(f"THINKING_MODE{suffix}={tm}")
    profile_lines.append(f"F_SID{suffix}={auth_info.get('f_sid', '')}")
    profile_lines.append(f"AT{suffix}={auth_info.get('at', '')}")
    profile_lines.append(f"SN_PARAM{suffix}={auth_info.get('sn_param', '')}")
    profile_lines.append(f"BL_PARAM{suffix}={auth_info.get('bl_param', '')}")
    profile_lines.append(f"HL{suffix}={auth_info.get('hl', 'zh-CN')}")
    profile_lines.append(f"UUID{suffix}={auth_info.get('session_uuid', '')}")
    profile_lines.append(f"HASH{suffix}={auth_info.get('request_hash', '')}")
    profile_lines.append("")

    # Build remaining profiles (keep existing ones that aren't this one)
    other_profiles = []
    for k, v in existing.items():
        if k.startswith("MODEL_NAMES") or k.startswith("DEFAULT_MODEL"):
            continue
        found = False
        for pf in PROFILE_FIELDS:
            if k.startswith(pf + "_") and k != pf + suffix:
                found = True
                break
        if found or k in SERVER_FIELDS:
            continue
        other_profiles.append(f"{k}={v}")

    result = "# ============================================\n"
    result += "# HOST / PORT / API_KEY\n"
    result += "# ============================================\n"
    result += "\n".join(server_lines) + "\n"
    result += "\n".join(profile_lines)
    if other_profiles:
        result += "# Other existing config\n"
        result += "\n".join(other_profiles) + "\n"
    return result


class ConfigToolGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gemini → OpenAI 代理配置工具")
        self.root.geometry("750x650")
        self.root.minsize(600, 500)

        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        main_frame = ttk.Frame(self.root, padding=16)
        main_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(main_frame, text="Gemini → OpenAI 代理配置",
                           font=("Segoe UI", 16, "bold"))
        header.pack(anchor=tk.W, pady=(0, 4))

        sub = ttk.Label(main_frame, text="选择 HAR 文件后自动解析并保存配置到 .env",
                        foreground="#666")
        sub.pack(anchor=tk.W, pady=(0, 16))

        # HAR file selection
        file_frame = ttk.Frame(main_frame)
        file_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(file_frame, text="HAR 文件:", font=("Segoe UI", 10, "bold"))\
            .pack(side=tk.LEFT, padx=(0, 8))

        self.har_path_var = tk.StringVar()
        self.har_entry = ttk.Entry(file_frame, textvariable=self.har_path_var)
        self.har_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(file_frame, text="浏览...", command=self._browse_har)\
            .pack(side=tk.LEFT)
        ttk.Button(file_frame, text="解析", command=self._parse)\
            .pack(side=tk.LEFT, padx=(4, 0))

        # Model name
        name_frame = ttk.Frame(main_frame)
        name_frame.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(name_frame, text="模型名:", font=("Segoe UI", 10, "bold"))\
            .pack(side=tk.LEFT, padx=(0, 8))

        self.model_name_var = tk.StringVar(value="gemini-3.5-flash")
        self.model_name_entry = ttk.Entry(name_frame, textvariable=self.model_name_var, width=30)
        self.model_name_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(name_frame, text="常用: gemini-3.5-flash / gemini-3.1-flash-lite",
                  foreground="#888", font=("Segoe UI", 8)).pack(side=tk.LEFT)

        # Notebook for results
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: extracted info
        self.info_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.info_frame, text="  解析信息  ")

        self.info_tree = ttk.Treeview(self.info_frame, columns=("key", "value"),
                                       show="tree headings", height=18)
        self.info_tree.heading("#0", text="字段")
        self.info_tree.heading("key", text="键")
        self.info_tree.heading("value", text="值")
        self.info_tree.column("#0", width=180, minwidth=120)
        self.info_tree.column("key", width=200, minwidth=120)
        self.info_tree.column("value", width=300, minwidth=200)

        vsb = ttk.Scrollbar(self.info_frame, orient=tk.VERTICAL, command=self.info_tree.yview)
        self.info_tree.configure(yscrollcommand=vsb.set)
        self.info_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Tab 2: .env preview
        self.env_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.env_frame, text="  .env 预览  ")

        self.env_text = tk.Text(self.env_frame, wrap=tk.NONE, font=("Consolas", 10),
                                 bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        env_vsb = ttk.Scrollbar(self.env_frame, orient=tk.VERTICAL, command=self.env_text.yview)
        env_hsb = ttk.Scrollbar(self.env_frame, orient=tk.HORIZONTAL, command=self.env_text.xview)
        self.env_text.configure(yscrollcommand=env_vsb.set, xscrollcommand=env_hsb.set)
        self.env_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        env_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        env_hsb.pack(side=tk.BOTTOM, fill=tk.X)

        # Save button area
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(12, 0))

        self.status_var = tk.StringVar(value="就绪")
        status_label = ttk.Label(btn_frame, textvariable=self.status_var,
                                  font=("Segoe UI", 9), foreground="#555")
        status_label.pack(side=tk.LEFT)

        ttk.Button(btn_frame, text="保存到 .env",
                   command=self._save_env).pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Button(btn_frame, text="启动代理服务器",
                   command=self._launch_server).pack(side=tk.RIGHT, padx=(8, 0))

        self._parsed_info = None
        self._env_content = ""

        # Auto-load existing har from .env
        self._try_load_existing()

        self.root.mainloop()

    def _try_load_existing(self):
        if os.path.exists(ENV_PATH):
            har_path = None
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("HAR_PATH="):
                        har_path = line.split("=", 1)[1]
                        break
            if har_path and os.path.exists(har_path):
                self.har_path_var.set(har_path)
                self._parse()
            elif har_path:
                self.har_path_var.set(har_path)

    def _browse_har(self):
        path = filedialog.askopenfilename(
            title="选择 HAR 文件",
            filetypes=[("HAR files", "*.har"), ("All files", "*.*")],
            initialdir=SELF_DIR,
        )
        if path:
            self.har_path_var.set(path)

    def _parse(self):
        har_path = self.har_path_var.get().strip()
        if not har_path:
            messagebox.showwarning("提示", "请先选择一个 HAR 文件")
            return
        if not os.path.exists(har_path):
            messagebox.showerror("错误", f"文件不存在:\n{har_path}")
            return

        self.status_var.set("正在解析...")
        self.root.update()

        try:
            info = parse_har_file(har_path)
            self._parsed_info = info
        except Exception as e:
            messagebox.showerror("解析失败", f"解析 HAR 文件时出错:\n{e}")
            self.status_var.set("解析失败")
            return

        # Show in tree
        self._populate_tree(info)

        # Auto-detect model family for name hint
        mf = info.get("model_family", 1)
        tm = info.get("thinking_mode", 1)
        name_hint = {1: "gemini-3.5-flash", 6: "gemini-3.1-flash-lite"}.get(mf, f"model-family-{mf}")
        if tm == 2:
            name_hint += "-adv"
        self.model_name_var.set(name_hint)

        # Build env - 只更新对应 profile，保留其他部分
        model_name = self.model_name_var.get().strip()
        self._env_content = update_env_with_profile(info, model_name)
        self.env_text.delete("1.0", tk.END)
        self.env_text.insert("1.0", self._env_content)
        self.env_text.see("1.0")

        errors = info.get("_errors", [])
        warnings = info.get("_warnings", [])
        msgs = []
        if errors:
            msgs.append(f"⚠ {len(errors)} 个错误")
        if warnings:
            msgs.append(f"⚠ {len(warnings)} 个警告")
        response_preview = info.get("_response_preview")
        if response_preview:
            msgs.append(f"✓ 响应预览: {response_preview[:60]}...")
        else:
            msgs.append("✓ 解析完成")
        self.status_var.set(" | ".join(msgs))

    def _populate_tree(self, info: dict):
        for item in self.info_tree.get_children():
            self.info_tree.delete(item)

        display = [
            ("目标地址", "base_url"),
            ("聊天端点", "chat_endpoint"),
            ("认证类型", "auth_type"),
            ("Cookie", "cookies"),
            ("Authorization", "auth_header"),
            ("bl 参数", "bl_param"),
            ("f.sid", "f_sid"),
            ("语言", "hl"),
            ("at 令牌", "at"),
            ("sn 参数", "sn_param"),
            ("会话ID (conv_id)", "_conv_id"),
            ("响应ID (resp_id)", "_resp_id"),
            ("内容引用ID (rc_id)", "_rc_id"),
            ("令牌 (token26)", "_token26"),
            ("模型家族", "model_family"),
            ("思考模式", "thinking_mode"),
            ("会话 UUID", "session_uuid"),
            ("请求 Hash", "request_hash"),
            ("流式支持", "is_streaming"),
            ("WebSocket", "has_websocket"),
            ("PoW 挑战", "has_pow"),
            ("请求头数量", "header_count"),
            ("响应预览", "_response_preview"),
        ]

        for label, key in display:
            val = info.get(key)
            if val is None:
                val = ""
            if isinstance(val, bool):
                val = "是" if val else "否"
            val_str = str(val)
            if len(val_str) > 120:
                val_str = val_str[:120] + "..."
            self.info_tree.insert("", tk.END, text=label, values=(key, val_str))

    def _save_env(self):
        if not self._env_content:
            messagebox.showwarning("提示", "请先解析一个 HAR 文件")
            return

        model_name = self.model_name_var.get().strip()
        if not model_name:
            messagebox.showwarning("提示", "请输入模型名")
            return

        # Rebuild env with current model name and info
        self._env_content = update_env_with_profile(self._parsed_info or {}, model_name)
        self.env_text.delete("1.0", tk.END)
        self.env_text.insert("1.0", self._env_content)

        try:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write(self._env_content)
            self.status_var.set(f"✓ Profile '{model_name}' 已保存到 {ENV_PATH}")
            messagebox.showinfo("保存成功",
                                f"Profile '{model_name}' 已保存到:\n{ENV_PATH}\n\n"
                                "现在可以启动代理服务器了。")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _launch_server(self):
        if not self._env_content:
            if not messagebox.askyesno("提示", "尚未保存配置，是否先保存？"):
                return
            self._save_env()

        try:
            import subprocess
            import sys
            server_script = os.path.join(SELF_DIR, "server.py")
            if not os.path.exists(server_script):
                messagebox.showerror("错误", f"找不到 server.py:\n{server_script}")
                return

            existing = read_existing_env()
            port = existing.get("PORT", "1800")
            dmodel = existing.get("DEFAULT_MODEL", "gemini-3.5-flash")

            proc = subprocess.Popen(
                [sys.executable, "-u", server_script, port],
                cwd=SELF_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            self.status_var.set(f"✓ 服务器已启动 (PID: {proc.pid})")
            models = existing.get("MODEL_NAMES", dmodel)
            messagebox.showinfo("服务器已启动",
                                f"代理服务器已在后台启动 (PID: {proc.pid})\n\n"
                                f"访问地址: http://localhost:{port}\n\n"
                                f"可用模型: {models}\n\n"
                                f"测试命令:\n"
                                f'curl http://localhost:{port}/v1/chat/completions -H "Content-Type: application/json" -d \'{{"model":"{dmodel}","messages":[{{"role":"user","content":"hi"}}]}}\'')
        except Exception as e:
            messagebox.showerror("启动失败", str(e))


if __name__ == "__main__":
    ConfigToolGUI()
