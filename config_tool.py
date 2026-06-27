"""Tkinter GUI for generating ``.env`` from a HAR file.

v1.0 changes
-------------
* Uses the shared :mod:`har_parser` instead of the previous in-file
  duplicate. If the new module ever drifts the GUI keeps working as long
  as ``parse_har()`` returns a ``GeminiHarAnalysis``.
* The "Start proxy" button now captures ``stdout``/``stderr`` and
  streams it into a Tkinter Text widget instead of silently
  detaching the process.
* Pre-flight port check via a transient socket — gives a friendly
  error before the user wastes time waiting for a connection-refused.
* Logging is routed through the shared :mod:`logger` so the GUI and
  the server emit consistent log lines.

Run with: ``python config_tool.py``
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from dotenv import load_dotenv
from har_parser import GeminiHarAnalysis, parse_har
from logger import get_logger

log = get_logger("config_tool")

APP_TITLE = "Gemini2API 配置工具 v1.0"
ENV_FILENAME = ".env"
ENV_EXAMPLE_FILENAME = ".env.example"

# ── .env helpers ─────────────────────────────────────────────────────

def read_existing_env(path: Path) -> list[tuple[str, str]]:
    """Return ``[(key, value), ...]`` for every ``KEY=VALUE`` line."""
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _suggest_model_name(family: int, thinking: int) -> str:
    family_part = {1: "gemini-3.5-flash", 3: "gemini-3.1-pro", 6: "gemini-3.1-flash-lite"}.get(family, "gemini-custom")
    suffix = "-adv" if thinking == 2 else ""
    return f"{family_part}{suffix}"


def build_env_lines(analysis: GeminiHarAnalysis, account_name: str, model_name: str) -> list[str]:
    """Compose the new ``.env`` content, preserving non-account keys."""
    here = Path(__file__).parent
    existing = read_existing_env(here / ENV_FILENAME)

    # Filter out ACCOUNT_* lines belonging to the account we're updating,
    # so we end up with one consolidated block.
    keep: list[tuple[str, str]] = []
    prefix = f"ACCOUNT_{account_name}_"
    for k, v in existing:
        if k.startswith(prefix):
            continue
        keep.append((k, v))

    # Ensure PROFILES / DEFAULT_MODEL include the new model.
    profiles_kv = dict(keep)
    profiles = [p.strip() for p in profiles_kv.get("PROFILES", "").split(",") if p.strip()]
    if model_name not in profiles:
        profiles.append(model_name)
    profiles_kv["PROFILES"] = ",".join(profiles)
    if not profiles_kv.get("DEFAULT_MODEL"):
        profiles_kv["DEFAULT_MODEL"] = model_name

    # Per-profile family / thinking.
    profiles_kv[f"MODEL_FAMILY_{model_name}"] = str(analysis.model_family)
    profiles_kv[f"THINKING_MODE_{model_name}"] = str(analysis.thinking_mode)
    # Make sure this account is bound to the model we just created.
    profiles_kv[f"ACCOUNT_{account_name}_F_SID"] = analysis.f_sid
    profiles_kv[f"ACCOUNT_{account_name}_AT"] = analysis.at
    profiles_kv[f"ACCOUNT_{account_name}_SN_PARAM"] = analysis.sn_param
    profiles_kv[f"ACCOUNT_{account_name}_BL_PARAM"] = analysis.bl_param
    profiles_kv[f"ACCOUNT_{account_name}_HL"] = analysis.hl
    profiles_kv[f"ACCOUNT_{account_name}_UUID"] = analysis.session_uuid
    profiles_kv[f"ACCOUNT_{account_name}_HASH"] = analysis.request_hash
    profiles_kv[f"ACCOUNT_{account_name}_ENABLED"] = "true"
    profiles_kv[f"ACCOUNT_{account_name}_MODELS"] = model_name

    # Serialise, ordered: known top-level first, then the rest.
    ordered_keys = [
        "HOST", "PORT", "API_KEY", "ADMIN_KEY", "ROTATION_STRATEGY",
        "MAX_ERRORS_BEFORE_DISABLE", "LOG_LEVEL", "GLOBAL_RATE_LIMIT_RPM",
        "PROFILES", "DEFAULT_MODEL", "PERSIST_ACCOUNTS", "CORS_ORIGINS",
    ]
    rendered: list[str] = []
    seen: set[str] = set()
    for k in ordered_keys:
        if k in profiles_kv:
            rendered.append(f"{k}={profiles_kv[k]}")
            seen.add(k)
    for k, v in profiles_kv.items():
        if k in seen:
            continue
        rendered.append(f"{k}={v}")
    return rendered


def write_env_file(lines: list[str], path: Path) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── GUI ──────────────────────────────────────────────────────────────

class ConfigToolGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x720")
        self.analysis: Optional[GeminiHarAnalysis] = None
        self.proc: Optional[subprocess.Popen] = None
        self.proc_thread: Optional[threading.Thread] = None
        self._build()

    # ── layout ──────────────────────────────────────────────────────

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm_top = ttk.Frame(self.root)
        frm_top.pack(fill=tk.X, **pad)

        ttk.Button(frm_top, text="选择 HAR 文件…", command=self._choose_har).pack(side=tk.LEFT)
        self.har_path_var = tk.StringVar()
        ttk.Entry(frm_top, textvariable=self.har_path_var, width=60).pack(side=tk.LEFT, padx=4)
        ttk.Button(frm_top, text="解析", command=self._parse_har).pack(side=tk.LEFT)

        frm_meta = ttk.LabelFrame(self.root, text="解析结果")
        frm_meta.pack(fill=tk.X, **pad)
        self.tree = ttk.Treeview(frm_meta, columns=("field", "value"), show="headings", height=10)
        self.tree.heading("field", text="字段")
        self.tree.heading("value", text="值")
        self.tree.column("field", width=160, anchor=tk.W)
        self.tree.column("value", width=720, anchor=tk.W)
        self.tree.pack(fill=tk.X, **pad)

        frm_cfg = ttk.LabelFrame(self.root, text="配置")
        frm_cfg.pack(fill=tk.X, **pad)
        ttk.Label(frm_cfg, text="账号名:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.account_name_var = tk.StringVar(value="account1")
        ttk.Entry(frm_cfg, textvariable=self.account_name_var, width=32).grid(row=0, column=1, sticky=tk.W)
        ttk.Label(frm_cfg, text="模型名:").grid(row=0, column=2, sticky=tk.W, **pad)
        self.model_name_var = tk.StringVar(value="gemini-default")
        ttk.Entry(frm_cfg, textvariable=self.model_name_var, width=32).grid(row=0, column=3, sticky=tk.W)
        ttk.Button(frm_cfg, text="保存到 .env", command=self._save_env).grid(row=0, column=4, **pad)
        ttk.Button(frm_cfg, text="启动代理服务器", command=self._start_server).grid(row=0, column=5, **pad)

        frm_preview = ttk.LabelFrame(self.root, text=".env 预览")
        frm_preview.pack(fill=tk.BOTH, expand=True, **pad)
        self.preview = scrolledtext.ScrolledText(frm_preview, height=14, bg="#101216", fg="#e1e4e8", insertbackground="#e1e4e8")
        self.preview.pack(fill=tk.BOTH, expand=True)

        frm_log = ttk.LabelFrame(self.root, text="服务器日志")
        frm_log.pack(fill=tk.BOTH, expand=True, **pad)
        self.log_box = scrolledtext.ScrolledText(frm_log, height=10, bg="#0a0d11", fg="#9bc995", insertbackground="#9bc995")
        self.log_box.pack(fill=tk.BOTH, expand=True)

    # ── actions ─────────────────────────────────────────────────────

    def _choose_har(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("HAR files", "*.har"), ("All", "*.*")])
        if path:
            self.har_path_var.set(path)

    def _parse_har(self) -> None:
        path = self.har_path_var.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror("错误", f"HAR 文件不存在:\n{path}")
            return
        try:
            self.analysis = parse_har(path)
        except Exception as exc:  # noqa: BLE001
            log.exception("parse_har_failed")
            messagebox.showerror("解析失败", str(exc))
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        rows = [
            ("bl_param", self.analysis.bl_param),
            ("f.sid", self.analysis.f_sid),
            ("hl", self.analysis.hl),
            ("at", self.analysis.at),
            ("sn_param", self.analysis.sn_param),
            ("model_family", self.analysis.model_family),
            ("thinking_mode", self.analysis.thinking_mode),
            ("session_uuid", self.analysis.session_uuid),
            ("request_hash", self.analysis.request_hash),
        ]
        for field_name, val in rows:
            self.tree.insert("", tk.END, values=(field_name, val))
        self.model_name_var.set(_suggest_model_name(self.analysis.model_family, self.analysis.thinking_mode))
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if not self.analysis:
            return
        lines = build_env_lines(self.analysis, self.account_name_var.get().strip() or "account1",
                                self.model_name_var.get().strip() or "gemini-default")
        self.preview.delete("1.0", tk.END)
        self.preview.insert(tk.END, "\n".join(lines))

    def _save_env(self) -> None:
        if not self.analysis:
            messagebox.showwarning("提示", "请先解析 HAR 文件")
            return
        account = self.account_name_var.get().strip() or "account1"
        model = self.model_name_var.get().strip() or "gemini-default"
        if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", account):
            messagebox.showerror("错误", "账号名只能包含字母数字、下划线、点、连字符")
            return
        try:
            lines = build_env_lines(self.analysis, account, model)
            write_env_file(lines, Path(__file__).parent / ENV_FILENAME)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", f"{ENV_FILENAME} 已更新。请重启代理服务器。")

    def _port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return True
        return False

    def _start_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("提示", "代理服务器已在运行")
            return
        here = Path(__file__).parent
        env_path = here / ENV_FILENAME
        if env_path.exists():
            load_dotenv(env_path, override=True)
        try:
            port = int(os.getenv("PORT", "1800"))
        except ValueError:
            port = 1800
        if self._port_in_use(port):
            messagebox.showerror("端口冲突", f"端口 {port} 已被占用,无法启动。\n请修改 .env 中的 PORT= 后重试。")
            return
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(here / "server.py"), str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=creationflags, cwd=str(here),
        )
        self.proc_thread = threading.Thread(target=self._drain_proc, daemon=True)
        self.proc_thread.start()
        self.log_box.insert(tk.END, f"--- 已启动 PID {self.proc.pid} (port={port}) ---\n")
        self.log_box.see(tk.END)

    def _drain_proc(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.root.after(0, self._append_log, line)
        rc = self.proc.wait() if self.proc else -1
        self.root.after(0, self._append_log, f"--- 进程退出 (rc={rc}) ---\n")

    def _append_log(self, line: str) -> None:
        self.log_box.insert(tk.END, line)
        self.log_box.see(tk.END)


# ── entry ────────────────────────────────────────────────────────────

def main() -> int:
    if os.name != "nt":
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
    root = tk.Tk()
    ConfigToolGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
