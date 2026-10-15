import os
import sys
import json
import uuid
import time
import socket
import threading
import subprocess
import shutil
import winreg
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog, scrolledtext
import urllib.request
import urllib.error
import urllib.parse

DEFAULT_SERVER_URL = "http://172.28.26.33:8000"

def get_codex_dir():
    if os.environ.get("CODEX_HOME"):
        return os.environ["CODEX_HOME"]
    userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(userprofile, ".codex")

def get_default_codex_path():
    return os.path.join(get_codex_dir(), "auth.json")

def get_config_path():
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    local_cfg = os.path.join(exe_dir, "hub_client_config.json")
    if os.path.exists(local_cfg):
        return local_cfg
    return os.path.join(get_codex_dir(), "hub_client_config.json")


def format_quota_window(window):
    if window.get("available") is False:
        return "未提供"
    remaining = window.get("remaining_percent")
    if remaining is None:
        return "暂无数据"
    reset = window.get("reset_after_text") or "重置时间未知"
    return f"{remaining:g}%  /  {reset}" if isinstance(remaining, (int, float)) else "暂无数据"

def refresh_windows_env_path():
    """重新从注册表及默认路径刷新当前进程的 PATH 环境变量，使得新装的 Node / npm / codex 立即生效"""
    paths = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
            sys_path, _ = winreg.QueryValueEx(key, "Path")
            paths.extend(sys_path.split(";"))
    except Exception:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            user_path, _ = winreg.QueryValueEx(key, "Path")
            paths.extend(user_path.split(";"))
    except Exception:
        pass

    extra = [
        r"C:\Program Files\nodejs",
        r"C:\Program Files (x86)\nodejs",
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "bin"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft VS Code", "bin"),
    ]
    for p in extra:
        if p and p not in paths and os.path.exists(p):
            paths.append(p)

    clean_paths = [p.strip() for p in paths if p.strip()]
    seen = set()
    dedup = []
    for p in clean_paths:
        norm = os.path.normcase(p)
        if norm not in seen:
            seen.add(norm)
            dedup.append(p)

    os.environ["PATH"] = ";".join(dedup)

def find_executable(name, extra_paths=None):
    """查找可执行文件（优先 PATH，再查找常见路径）"""
    found = shutil.which(name)
    if found:
        return found
    if extra_paths:
        for p in extra_paths:
            if os.path.isfile(p):
                return p
def format_plan_name(raw_plan):
    """将 OpenAI 后端晦涩的内部代号转换为通用直观的套餐名称"""
    if not raw_plan:
        return "Team 团队版"
    p = str(raw_plan).upper().strip()
    if any(k in p for k in ["PROLITE", "BUSINESS", "TEAM"]):
        return "Team 团队版"
    if "ENTERPRISE" in p:
        return "Enterprise 企业版"
    if "PLUS" in p:
        return "Plus 会员"
    if "PRO" in p:
        return "Pro 专业版"
    if "FREE" in p:
        return "Free 免费版"
    return raw_plan

def check_env_status():
    """全面体检本地环境：Node.js、Codex CLI、VS Code、VS Code 插件"""
    refresh_windows_env_path()
    res = {
        "node": {"ok": False, "version": "", "path": ""},
        "codex_cli": {"ok": False, "version": "", "path": ""},
        "vscode": {"ok": False, "version": "", "path": ""},
        "vscode_ext": {"ok": False, "version": "", "installed": False}
    }

    # 1. 检查 Node.js
    node_exe = find_executable("node.exe", [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe"
    ])
    if node_exe:
        try:
            p = subprocess.run([node_exe, "-v"], capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            if p.returncode == 0 and p.stdout.strip():
                res["node"]["ok"] = True
                res["node"]["version"] = p.stdout.strip()
                res["node"]["path"] = node_exe
        except Exception:
            pass

    # 2. 检查 Codex CLI (@openai/codex)
    codex_cmd = find_executable("codex.cmd", [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Volta", "bin", "codex.cmd")
    ])
    if codex_cmd:
        try:
            p = subprocess.run(["cmd.exe", "/c", codex_cmd, "--version"], capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            if p.returncode == 0 and p.stdout.strip():
                res["codex_cli"]["ok"] = True
                res["codex_cli"]["version"] = p.stdout.strip()
                res["codex_cli"]["path"] = codex_cmd
        except Exception:
            pass
    if not res["codex_cli"]["ok"]:
        try:
            p = subprocess.run(["cmd.exe", "/c", "codex", "--version"], capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            if p.returncode == 0 and p.stdout.strip():
                res["codex_cli"]["ok"] = True
                res["codex_cli"]["version"] = p.stdout.strip()
                res["codex_cli"]["path"] = "codex"
        except Exception:
            pass

    # 3. 检查 VS Code
    code_cmd = find_executable("code.cmd", [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "bin", "code.cmd"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft VS Code", "bin", "code.cmd"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft VS Code", "bin", "code.cmd")
    ])
    if code_cmd:
        res["vscode"]["ok"] = True
        res["vscode"]["path"] = code_cmd
        try:
            p = subprocess.run(["cmd.exe", "/c", code_cmd, "-v"], capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            if p.returncode == 0 and p.stdout.strip():
                res["vscode"]["version"] = p.stdout.strip().splitlines()[0]
        except Exception:
            pass

    # 4. 检查 VS Code 插件 openai.chatgpt
    if res["vscode"]["ok"]:
        try:
            p = subprocess.run(["cmd.exe", "/c", res["vscode"]["path"], "--list-extensions", "--show-versions"], capture_output=True, text=True, timeout=8, creationflags=0x08000000)
            if p.returncode == 0:
                for line in p.stdout.lower().splitlines():
                    if "openai.chatgpt" in line:
                        res["vscode_ext"]["ok"] = True
                        res["vscode_ext"]["installed"] = True
                        res["vscode_ext"]["version"] = line.strip()
                        break
        except Exception:
            pass

    return res

def download_package_file(server_url, filename, dest_path, progress_cb=None, log_cb=None, cancel_event=None):
    clean_server = server_url.strip().rstrip("/")
    url = f"{clean_server}/api/packages/download/{filename}"
    if log_cb:
        log_cb(f"正在从服务端下载: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "CodexClient/1.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 128 * 1024
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise Exception("下载已被取消")
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total > 0:
                    pct = int((downloaded / total) * 100)
                    progress_cb(downloaded, total, pct)
    if log_cb:
        size_mb = downloaded / (1024 * 1024)
        log_cb(f"下载完成: {filename} ({size_mb:.2f} MB)")
    return dest_path

class EnvInstallerDialog:
    def __init__(self, parent, server_url):
        self.parent = parent
        self.server_url = server_url
        self.top = tk.Toplevel(parent)
        self.top.title("🛠️ Codex 客户端运行环境体检与一键离线部署")
        self.top.geometry("780x640")
        self.top.minsize(720, 560)
        self.top.transient(parent)
        self.top.grab_set()

        self.env_status = None
        self.is_installing = False

        self.setup_ui()
        self.start_check_env()

    def setup_ui(self):
        # 头部说明
        header_frame = ttk.Frame(self.top, padding=(16, 12))
        header_frame.pack(fill=tk.X)

        title_lbl = ttk.Label(
            header_frame, 
            text="Codex 本地开发环境体检与局域网一键部署", 
            font=("Segoe UI", 12, "bold"),
            foreground="#1e293b"
        )
        title_lbl.pack(anchor=tk.W)

        sub_lbl = ttk.Label(
            header_frame,
            text=f"无需连外网！所有依赖安装包由内部服务端高速提供 (当前服务端: {self.server_url})",
            font=("Segoe UI", 9),
            foreground="#64748b"
        )
        sub_lbl.pack(anchor=tk.W, pady=(2, 0))

        # 中间检测卡片
        card_frame = ttk.LabelFrame(self.top, text=" 本地环境健康状态与组件检测 ", padding=12)
        card_frame.pack(fill=tk.X, padx=16, pady=6)

        # 1. Node.js
        row1 = ttk.Frame(card_frame)
        row1.pack(fill=tk.X, pady=4)
        ttk.Label(row1, text="1. Node.js 运行环境 (LTS):", width=26, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self.node_status_lbl = ttk.Label(row1, text="正在体检...", font=("Segoe UI", 9))
        self.node_status_lbl.pack(side=tk.LEFT, padx=6)
        self.btn_install_node = ttk.Button(row1, text="离线安装 Node.js", command=lambda: self.start_single_install("node"), state=tk.DISABLED)
        self.btn_install_node.pack(side=tk.RIGHT)

        # 2. Codex CLI
        row2 = ttk.Frame(card_frame)
        row2.pack(fill=tk.X, pady=4)
        ttk.Label(row2, text="2. Codex CLI (@openai/codex):", width=26, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self.cli_status_lbl = ttk.Label(row2, text="正在体检...", font=("Segoe UI", 9))
        self.cli_status_lbl.pack(side=tk.LEFT, padx=6)
        self.btn_install_cli = ttk.Button(row2, text="离线安装 Codex CLI", command=lambda: self.start_single_install("codex_cli"), state=tk.DISABLED)
        self.btn_install_cli.pack(side=tk.RIGHT)

        # 3. VS Code 主程序
        row3 = ttk.Frame(card_frame)
        row3.pack(fill=tk.X, pady=4)
        ttk.Label(row3, text="3. VS Code 主程序:", width=26, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self.vscode_status_lbl = ttk.Label(row3, text="正在体检...", font=("Segoe UI", 9))
        self.vscode_status_lbl.pack(side=tk.LEFT, padx=6)

        # 4. VS Code 插件
        row4 = ttk.Frame(card_frame)
        row4.pack(fill=tk.X, pady=4)
        ttk.Label(row4, text="4. Codex VS Code 插件:", width=26, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        self.ext_status_lbl = ttk.Label(row4, text="正在体检...", font=("Segoe UI", 9))
        self.ext_status_lbl.pack(side=tk.LEFT, padx=6)
        self.btn_install_ext = ttk.Button(row4, text="离线安装 VS Code 插件", command=lambda: self.start_single_install("vscode_ext"), state=tk.DISABLED)
        self.btn_install_ext.pack(side=tk.RIGHT)

        # 操作控制区
        act_frame = ttk.Frame(self.top, padding=(16, 6))
        act_frame.pack(fill=tk.X)

        self.btn_install_all = ttk.Button(
            act_frame,
            text="⚡ 一键全自动离线安装 (安装全部缺失环境)",
            command=self.start_install_all,
            state=tk.DISABLED
        )
        self.btn_install_all.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_recheck = ttk.Button(act_frame, text="🔄 重新体检环境", command=self.start_check_env)
        self.btn_recheck.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(act_frame, text="关闭窗口", command=self.top.destroy).pack(side=tk.RIGHT)

        # 进度条区
        prog_frame = ttk.Frame(self.top, padding=(16, 4))
        prog_frame.pack(fill=tk.X)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X)

        self.progress_lbl = ttk.Label(prog_frame, text="准备就绪", font=("Segoe UI", 8), foreground="#64748b")
        self.progress_lbl.pack(anchor=tk.W, pady=(2, 0))

        # 控制台实时输出区
        log_frame = ttk.LabelFrame(self.top, text=" 安装与部署实时日志 ", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 12))

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            bg="#0f172a",
            fg="#f8fafc",
            font=("Consolas", 9),
            insertbackground="white",
            relief=tk.FLAT
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_config("info", foreground="#38bdf8")
        self.log_text.tag_config("success", foreground="#4ade80")
        self.log_text.tag_config("warn", foreground="#facc15")
        self.log_text.tag_config("error", foreground="#f87171")
        self.log_text.tag_config("dim", foreground="#94a3b8")

    def log(self, text, tag="info"):
        now_str = datetime.now().strftime("%H:%M:%S")
        line = f"[{now_str}] {text}\n"
        self.top.after(0, self._append_log, line, tag)

    def _append_log(self, line, tag):
        self.log_text.insert(tk.END, line, tag)
        self.log_text.see(tk.END)

    def set_progress(self, pct, text):
        self.top.after(0, lambda: self._update_progress(pct, text))

    def _update_progress(self, pct, text):
        self.progress_var.set(pct)
        self.progress_lbl.configure(text=text)

    def start_check_env(self):
        if self.is_installing:
            return
        self.btn_recheck.configure(state=tk.DISABLED)
        self.btn_install_all.configure(state=tk.DISABLED)
        self.node_status_lbl.configure(text="正在检测...", foreground="#64748b")
        self.cli_status_lbl.configure(text="正在检测...", foreground="#64748b")
        self.vscode_status_lbl.configure(text="正在检测...", foreground="#64748b")
        self.ext_status_lbl.configure(text="正在检测...", foreground="#64748b")
        self.log("正在对本地机器执行环境全面体检...", "info")

        threading.Thread(target=self._worker_check_env, daemon=True).start()

    def _worker_check_env(self):
        status = check_env_status()
        self.env_status = status
        self.top.after(0, lambda: self._on_env_checked(status))

    def _on_env_checked(self, s):
        self.btn_recheck.configure(state=tk.NORMAL)

        # 1. Node.js
        if s["node"]["ok"]:
            self.node_status_lbl.configure(text=f"🟢 已就绪 ({s['node']['version']})", foreground="#059669")
            self.btn_install_node.configure(state=tk.DISABLED)
        else:
            self.node_status_lbl.configure(text="🔴 未安装 (缺少 Node.js 运行环境)", foreground="#dc2626")
            self.btn_install_node.configure(state=tk.NORMAL)

        # 2. Codex CLI
        if s["codex_cli"]["ok"]:
            self.cli_status_lbl.configure(text=f"🟢 已就绪 ({s['codex_cli']['version']})", foreground="#059669")
            self.btn_install_cli.configure(state=tk.DISABLED)
        else:
            self.cli_status_lbl.configure(text="🔴 未安装 (缺少 @openai/codex CLI)", foreground="#dc2626")
            self.btn_install_cli.configure(state=tk.NORMAL)

        # 3. VS Code 主程序
        if s["vscode"]["ok"]:
            ver_text = f" ({s['vscode']['version']})" if s['vscode']['version'] else ""
            self.vscode_status_lbl.configure(text=f"🟢 已检测到{ver_text}", foreground="#059669")
        else:
            self.vscode_status_lbl.configure(text="⚠️ 未检测到 VS Code (请先在电脑安装 VS Code)", foreground="#d97706")

        # 4. VS Code 插件
        if s["vscode_ext"]["ok"]:
            self.ext_status_lbl.configure(text="🟢 已就绪 (openai.chatgpt 插件已就绪)", foreground="#059669")
            self.btn_install_ext.configure(state=tk.DISABLED)
        else:
            if not s["vscode"]["ok"]:
                self.ext_status_lbl.configure(text="⚠️ 需先安装 VS Code 主程序", foreground="#d97706")
                self.btn_install_ext.configure(state=tk.DISABLED)
            else:
                self.ext_status_lbl.configure(text="🔴 未安装 (未安装 Codex – OpenAI 插件)", foreground="#dc2626")
                self.btn_install_ext.configure(state=tk.NORMAL)

        missing_any = (not s["node"]["ok"]) or (not s["codex_cli"]["ok"]) or (s["vscode"]["ok"] and not s["vscode_ext"]["ok"])
        if missing_any:
            self.btn_install_all.configure(state=tk.NORMAL)
            self.log("体检完毕：检测到部分组件缺失，可点击「一键全自动离线安装」快速部署！", "warn")
        else:
            self.btn_install_all.configure(state=tk.DISABLED)
            self.log("体检完毕：本地所有环境组件均已就绪，状态极佳！", "success")

    def _set_ui_installing(self, installing):
        self.is_installing = installing
        state = tk.DISABLED if installing else tk.NORMAL
        self.btn_install_all.configure(state=state)
        self.btn_recheck.configure(state=state)
        self.btn_install_node.configure(state=state)
        self.btn_install_cli.configure(state=state)
        self.btn_install_ext.configure(state=state)

    def start_single_install(self, comp_id):
        if self.is_installing:
            return
        self._set_ui_installing(True)
        threading.Thread(target=self._worker_single_install, args=(comp_id,), daemon=True).start()

    def _worker_single_install(self, comp_id):
        temp_dir = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "codex_offline_install")
        os.makedirs(temp_dir, exist_ok=True)
        try:
            if comp_id == "node":
                self._do_install_node(temp_dir)
            elif comp_id == "codex_cli":
                self._do_install_codex_cli(temp_dir)
            elif comp_id == "vscode_ext":
                self._do_install_vscode_ext(temp_dir)
        except Exception as e:
            self.log(f"安装过程发生异常: {e}", "error")
        finally:
            self.top.after(0, lambda: self._set_ui_installing(False))
            self.start_check_env()

    def start_install_all(self):
        if self.is_installing:
            return
        self._set_ui_installing(True)
        threading.Thread(target=self._worker_install_all, daemon=True).start()

    def _worker_install_all(self):
        temp_dir = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "codex_offline_install")
        os.makedirs(temp_dir, exist_ok=True)
        self.log(">>> 开始执行全自动一键离线部署流水线...", "info")

        try:
            status = check_env_status()
            # 1. Node.js
            if not status["node"]["ok"]:
                self.log(">>> [步骤 1/3] 检测到 Node.js 缺失，开始离线安装 Node.js...", "info")
                ok = self._do_install_node(temp_dir)
                if not ok:
                    self.log("Node.js 安装未成功，中止后续依赖安装。", "error")
                    return
                refresh_windows_env_path()
            else:
                self.log(">>> [步骤 1/3] Node.js 已安装，跳过。", "dim")

            # 2. Codex CLI
            status = check_env_status()
            if not status["codex_cli"]["ok"]:
                self.log(">>> [步骤 2/3] 检测到 Codex CLI 缺失，开始离线安装 @openai/codex...", "info")
                ok = self._do_install_codex_cli(temp_dir)
                if not ok:
                    self.log("Codex CLI 安装未成功。", "error")
                refresh_windows_env_path()
            else:
                self.log(">>> [步骤 2/3] Codex CLI 已安装，跳过。", "dim")

            # 3. VS Code 插件
            status = check_env_status()
            if status["vscode"]["ok"] and not status["vscode_ext"]["ok"]:
                self.log(">>> [步骤 3/3] 检测到 VS Code 插件缺失，开始离线安装 openai.chatgpt.vsix...", "info")
                self._do_install_vscode_ext(temp_dir)
            elif not status["vscode"]["ok"]:
                self.log(">>> [步骤 3/3] 未检测到 VS Code 主程序，跳过插件安装。", "warn")
            else:
                self.log(">>> [步骤 3/3] VS Code Codex 插件已安装，跳过。", "dim")

            self.log("🎉 一键离线安装流程全部执行完毕！", "success")
            self.set_progress(100, "离线安装全部完成！")
            self.top.after(0, lambda: messagebox.showinfo("安装成功", "🎉 Codex 离线环境全部安装完成！"))
        except Exception as e:
            self.log(f"一键安装流水线异常: {e}", "error")
        finally:
            self.top.after(0, lambda: self._set_ui_installing(False))
            self.start_check_env()

    def _do_install_node(self, temp_dir):
        msi_path = os.path.join(temp_dir, "node-v20-x64.msi")
        self.set_progress(10, "正在从服务端下载 Node.js 安装包...")
        self.log("正在下载 Node.js v20 (node-v20-x64.msi)...", "info")

        def p_cb(d, t, pct):
            self.set_progress(pct * 0.5, f"下载 Node.js: {d/(1024*1024):.1f} MB / {t/(1024*1024):.1f} MB ({pct}%)")

        try:
            download_package_file(self.server_url, "node-v20-x64.msi", msi_path, progress_cb=p_cb, log_cb=lambda m: self.log(m, "dim"))
        except Exception as e:
            self.log(f"下载 Node.js 失败: {e}", "error")
            return False

        self.set_progress(60, "正在静默安装 Node.js (msiexec)...")
        self.log("正在执行 Windows 安装程序: msiexec /i node-v20-x64.msi /passive /norestart ...", "info")

        try:
            p = subprocess.run(
                ["msiexec.exe", "/i", msi_path, "/passive", "/norestart"],
                capture_output=True,
                text=True,
                creationflags=0x08000000
            )
            refresh_windows_env_path()
            if p.returncode in (0, 3010):
                self.set_progress(100, "Node.js 安装成功！")
                self.log("✅ Node.js v20 离线安装成功！已自动生效 PATH。", "success")
                return True
            else:
                self.log(f"msiexec 返回码: {p.returncode}", "error")
                return False
        except Exception as e:
            self.log(f"执行 Node.js 安装程序异常: {e}", "error")
            return False

    def _do_install_codex_cli(self, temp_dir):
        tgz_path = os.path.join(temp_dir, "openai-codex.tgz")
        self.set_progress(10, "正在从服务端下载 Codex CLI 离线包...")
        self.log("正在下载 Codex CLI 离线包 (openai-codex.tgz)...", "info")

        def p_cb(d, t, pct):
            self.set_progress(pct * 0.5, f"下载 Codex CLI: {pct}%")

        try:
            download_package_file(self.server_url, "openai-codex.tgz", tgz_path, progress_cb=p_cb, log_cb=lambda m: self.log(m, "dim"))
        except Exception as e:
            self.log(f"下载 Codex CLI 失败: {e}", "error")
            return False

        refresh_windows_env_path()
        npm_cmd = find_executable("npm.cmd", [
            r"C:\Program Files\nodejs\npm.cmd",
            r"C:\Program Files (x86)\nodejs\npm.cmd"
        ])
        if not npm_cmd:
            self.log("未找到 npm.cmd，请确保先安装好 Node.js！", "error")
            return False

        self.set_progress(60, "正在执行 npm install -g 离线安装...")
        self.log(f"正在执行: npm install -g \"{tgz_path}\" ...", "info")

        try:
            p = subprocess.run(
                ["cmd.exe", "/c", npm_cmd, "install", "-g", tgz_path],
                capture_output=True,
                text=True,
                creationflags=0x08000000
            )
            refresh_windows_env_path()
            if p.returncode == 0:
                self.set_progress(100, "Codex CLI 安装成功！")
                self.log("✅ Codex CLI (@openai/codex) 离线全局安装成功！", "success")
                return True
            else:
                self.log(f"npm 安装失败: {p.stderr or p.stdout}", "error")
                return False
        except Exception as e:
            self.log(f"执行 npm 安装异常: {e}", "error")
            return False

    def _do_install_vscode_ext(self, temp_dir):
        code_cmd = find_executable("code.cmd", [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "bin", "code.cmd"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft VS Code", "bin", "code.cmd"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft VS Code", "bin", "code.cmd")
        ])
        if not code_cmd:
            self.log("未找到 VS Code (code.cmd)，无法安装插件！", "error")
            return False

        vsix_path = os.path.join(temp_dir, "openai.chatgpt.vsix")
        self.set_progress(5, "正在从服务端下载 VS Code 插件 (522 MB)...")
        self.log("正在从服务端高速下载 VS Code Codex 插件 (openai.chatgpt.vsix, 522 MB)...", "info")

        def p_cb(d, t, pct):
            d_mb = d / (1024 * 1024)
            t_mb = t / (1024 * 1024) if t else 522.0
            self.set_progress(pct * 0.7, f"下载 VS Code 插件: {d_mb:.1f} MB / {t_mb:.1f} MB ({pct}%)")

        try:
            download_package_file(self.server_url, "openai.chatgpt.vsix", vsix_path, progress_cb=p_cb, log_cb=lambda m: self.log(m, "dim"))
        except Exception as e:
            self.log(f"下载 VS Code 插件失败: {e}", "error")
            return False

        self.set_progress(75, "正在将插件安装至 VS Code (code --install-extension)...")
        self.log(f"正在安装 VS Code 插件: code --install-extension \"{vsix_path}\" --force ...", "info")

        try:
            p = subprocess.run(
                ["cmd.exe", "/c", code_cmd, "--install-extension", vsix_path, "--force"],
                capture_output=True,
                text=True,
                creationflags=0x08000000
            )
            if p.returncode == 0 or "successfully installed" in (p.stdout + p.stderr).lower():
                self.set_progress(100, "VS Code 插件安装成功！")
                self.log("✅ Codex VS Code 插件 (openai.chatgpt) 离线安装成功！", "success")
                return True
            else:
                self.log(f"VS Code 插件安装输出: {p.stdout or p.stderr}", "error")
                return False
        except Exception as e:
            self.log(f"执行插件安装异常: {e}", "error")
            return False

class CodexClientApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Codex Auth Hub · 团队账号工作台")
        self.root.geometry("1200x800")
        self.root.minsize(980, 720)

        # 读取本地配置
        saved_server = DEFAULT_SERVER_URL
        saved_user = ""
        saved_token = ""
        saved_display = ""
        saved_role = "user"
        saved_session_token = ""
        saved_active_pid = None
        saved_active_pname = ""
        cfg_file = get_config_path()
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    saved_server = cfg.get("server_url", saved_server)
                    saved_user = cfg.get("username", "")
                    saved_token = cfg.get("auth_token", "")
                    saved_display = cfg.get("display_name", "")
                    saved_role = cfg.get("role", "user")
                    saved_session_token = cfg.get("session_token", "")
                    saved_active_pid = cfg.get("active_profile_id", None)
                    saved_active_pname = cfg.get("active_profile_name", "")
            except Exception:
                saved_session_token = ""
                saved_active_pid = None
                saved_active_pname = ""

        self.server_url_var = tk.StringVar(value=saved_server)
        self.auth_token = saved_token
        self.current_user = saved_user
        self.current_display = saved_display
        self.current_role = saved_role
        self.hostname = socket.gethostname() or "LocalPC"
        self.session_token = saved_session_token or None
        self.active_profile_id = saved_active_pid
        self.active_profile_name = saved_active_pname or None
        self.is_heartbeat_running = False
        self.profiles_cache = []
        self._is_fetching = False

        self.user_status_var = tk.StringVar(value="未登录")
        if self.active_profile_name:
            self.active_account_var = tk.StringVar(value=self.active_profile_name)
        else:
            self.active_account_var = tk.StringVar(value="尚未使用账号")

        self.setup_ui()
        self.update_user_display()
        if self.active_profile_name and self.session_token:
            self.release_btn.configure(state=tk.NORMAL)
            self.is_heartbeat_running = True
            threading.Thread(target=self.heartbeat_loop, daemon=True).start()

        # 启动时若有保存的 token，异步验证
        if self.auth_token:
            threading.Thread(target=self.verify_saved_token, daemon=True).start()
        else:
            self.root.after(500, self.prompt_login_if_needed)

        # 异步非阻塞拉取
        self.refresh_profiles()

        # 自动轮询刷新（每6秒）
        self.poll_timer = None
        self.start_auto_refresh()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def save_config(self):
        try:
            os.makedirs(get_codex_dir(), exist_ok=True)
            with open(get_config_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "server_url": self.server_url_var.get().strip().rstrip("/"),
                    "username": self.current_user,
                    "auth_token": self.auth_token,
                    "display_name": self.current_display,
                    "role": self.current_role,
                    "session_token": self.session_token or "",
                    "active_profile_id": self.active_profile_id,
                    "active_profile_name": self.active_profile_name or ""
                }, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def setup_ui(self):
        self.root.configure(background="#f3f5fa")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root.option_add("*Toplevel.background", "#f3f5fa")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f3f5fa", foreground="#24324b")
        style.configure("TFrame", background="#f3f5fa")
        style.configure("TLabel", background="#f3f5fa")
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("Muted.TLabel", background="white", foreground="#78859a", font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background="white", foreground="#17243c", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Value.TLabel", background="white", foreground="#4357d9", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("TButton", background="white", foreground="#42516b", bordercolor="#dce2ee", lightcolor="white", darkcolor="white", padding=(14, 7), borderwidth=1)
        style.map("TButton", background=[("pressed", "#e3e8f7"), ("active", "#eef1fa")], foreground=[("disabled", "#a0aabc")])
        style.configure("Primary.TButton", background="#4f5fe7", foreground="white", bordercolor="#4f5fe7", lightcolor="#4f5fe7", darkcolor="#4f5fe7", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("disabled", "#dfe4f4"), ("pressed", "#3848c4"), ("active", "#6574f0")], foreground=[("disabled", "#8e9aba"), ("!disabled", "white")])
        style.configure("TEntry", fieldbackground="white", foreground="#24324b", bordercolor="#dce2ee", lightcolor="#dce2ee", darkcolor="#dce2ee", padding=7)
        style.map("TEntry", bordercolor=[("focus", "#6878ea")])
        style.configure("TCombobox", fieldbackground="white", padding=6)
        style.configure("TLabelframe", background="#f3f5fa", bordercolor="#dce2ee", relief="solid")
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"), foreground="#42516b")
        style.configure("Treeview", background="white", fieldbackground="white", foreground="#42516b", rowheight=40, borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background="#f5f7fc", foreground="#7b879d", font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 13), relief="flat", borderwidth=0)
        style.map("Treeview", background=[("selected", "#e8edff")], foreground=[("selected", "#3848b9")])
        style.map("Treeview.Heading", background=[("active", "#edf0f8")])
        style.configure("TScrollbar", background="#d8dfed", troughcolor="white", borderwidth=0, arrowsize=12)
        style.configure("Horizontal.TProgressbar", background="#4f5fe7", troughcolor="#e6eaf4", borderwidth=0)

        header = tk.Frame(self.root, bg="#18243c", padx=26, pady=10)
        header.pack(fill=tk.X)
        mark = tk.Label(header, text="C", bg="#5365eb", fg="white", font=("Segoe UI", 22, "bold"), padx=15, pady=2)
        mark.pack(side=tk.LEFT, padx=(0, 14))
        brand = tk.Frame(header, bg="#18243c")
        brand.pack(side=tk.LEFT)
        tk.Label(brand, text="Codex Auth Hub", bg="#18243c", fg="white", font=("Segoe UI", 19, "bold")).pack(anchor=tk.W)
        tk.Label(brand, text="团队账号工作台  /  认证分发与额度管理", bg="#18243c", fg="#a8b6d1", font=("Microsoft YaHei UI", 9)).pack(anchor=tk.W, pady=(3, 0))
        tk.Label(header, text=f"本地设备  ·  {self.hostname}", bg="#18243c", fg="#c8d2e7", font=("Microsoft YaHei UI", 9)).pack(side=tk.RIGHT)

        body = ttk.Frame(self.root, padding=(24, 10, 24, 10))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        connection = ttk.Frame(body, style="Card.TFrame", padding=(16, 12))
        connection.grid(row=0, column=0, sticky="ew")
        ttk.Label(connection, text="服务连接", style="Card.TLabel", font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT, padx=(0, 14))
        self.server_entry = ttk.Entry(connection, textvariable=self.server_url_var, width=32)
        self.server_entry.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(connection, text="刷新列表", command=self.refresh_profiles).pack(side=tk.LEFT)
        ttk.Button(connection, text="环境检测与安装", command=self.open_env_checker_window).pack(side=tk.RIGHT)

        stats = ttk.Frame(body)
        stats.grid(row=1, column=0, sticky="ew", pady=8)
        stats.columnconfigure((0, 1, 2), weight=1, uniform="stats")
        self.profile_count_var = tk.StringVar(value="—")
        self.usage_count_var = tk.StringVar(value="—")
        for index, (caption, variable, subtitle) in enumerate((
            ("团队账号", self.profile_count_var, "集中管理的 Codex 账号"),
            ("使用人数", self.usage_count_var, "当前账号占用人数合计"),
            ("我的当前账号", self.active_account_var, "切换账号后自动释放此前占用"),
        )):
            card = ttk.Frame(stats, style="Card.TFrame", padding=(18, 8))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 0 if index == 2 else 7))
            ttk.Label(card, text=caption, style="Muted.TLabel").pack(anchor=tk.W)
            label = ttk.Label(card, textvariable=variable, style="Value.TLabel")
            if index == 2:
                label.configure(font=("Microsoft YaHei UI", 11, "bold"), width=24, anchor=tk.W)
                self.active_banner_lbl = label
            label.pack(anchor=tk.W, pady=(5, 4))
            ttk.Label(card, text=subtitle, style="Muted.TLabel").pack(anchor=tk.W)

        accounts = ttk.Frame(body, style="Card.TFrame", padding=(16, 10))
        accounts.grid(row=2, column=0, sticky="nsew")
        list_header = ttk.Frame(accounts, style="Card.TFrame")
        list_header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(list_header, text="账号资源", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(list_header, text="选择账号，查看额度并应用到本机", style="Muted.TLabel").pack(side=tk.LEFT, padx=16)
        ttk.Label(list_header, text="每 6 秒自动同步", style="Muted.TLabel").pack(side=tk.RIGHT)

        table = ttk.Frame(accounts, style="Card.TFrame")
        table.pack(fill=tk.BOTH, expand=True)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        columns = ("id", "name", "plan", "quota_5h", "quota_week", "users_count", "online_users", "email")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse", height=3)
        for column, title, width, anchor in (
            ("id", "ID", 42, tk.CENTER),
            ("name", "账号名称", 145, tk.W),
            ("plan", "订阅", 88, tk.CENTER),
            ("quota_5h", "5 小时剩余额度 / 重置", 175, tk.W),
            ("quota_week", "本周剩余额度 / 重置", 175, tk.W),
            ("users_count", "使用人数", 80, tk.CENTER),
            ("online_users", "当前使用者", 145, tk.W),
            ("email", "绑定邮箱", 150, tk.W),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=width, anchor=anchor, stretch=column in ("name", "online_users", "email"))
        self.tree.tag_configure("even", background="white")
        self.tree.tag_configure("odd", background="#f8faff")
        self.tree.tag_configure("current", background="#edf8f4", foreground="#168263")
        vertical = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_item)
        self.empty_label = ttk.Label(table, text="正在获取团队账号…", style="Muted.TLabel", anchor=tk.CENTER)
        self.empty_label.place(relx=0.5, rely=0.42, anchor=tk.CENTER)

        actions = ttk.Frame(body, style="Card.TFrame", padding=(16, 10))
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.selected_label = ttk.Label(actions, text="选择一个账号开始使用", style="Card.TLabel", font=("Microsoft YaHei UI", 10, "bold"), wraplength=900)
        self.selected_label.pack(anchor=tk.W)
        ttk.Label(actions, text="应用后会备份并替换本机认证；VS Code 中重载窗口即可读取新账号。", style="Muted.TLabel").pack(anchor=tk.W, pady=(4, 8))
        buttons = ttk.Frame(actions, style="Card.TFrame")
        buttons.pack(fill=tk.X)
        self.apply_btn = ttk.Button(buttons, text="使用此账号", style="Primary.TButton", command=self.apply_selected_profile, state=tk.DISABLED)
        self.apply_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.release_btn = ttk.Button(buttons, text="释放当前账号", command=self.release_current_session, state=tk.DISABLED)
        self.release_btn.pack(side=tk.LEFT)
        self.vscode_help_btn = ttk.Button(buttons, text="VS Code 生效指引", command=self.reload_vscode_prompt)
        self.vscode_help_btn.pack(side=tk.RIGHT)

        identity = ttk.Frame(body, padding=(0, 8, 0, 0))
        identity.grid(row=4, column=0, sticky="ew")
        self.user_lbl = ttk.Label(identity, textvariable=self.user_status_var, foreground="#66758f", font=("Microsoft YaHei UI", 9))
        self.user_lbl.pack(side=tk.LEFT)
        user_buttons = ttk.Frame(identity)
        user_buttons.pack(side=tk.RIGHT)
        self.btn_login = ttk.Button(user_buttons, text="登录 / 注册", command=self.open_login_window)
        self.btn_login.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_add_user = ttk.Button(user_buttons, text="添加团队用户", command=self.open_admin_add_user_window)
        self.btn_add_user.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_logout = ttk.Button(user_buttons, text="退出登录", command=self.do_logout)
        self.btn_logout.pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="正在连接服务端…")
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W, padding=(24, 8), foreground="#78859a", font=("Microsoft YaHei UI", 9), wraplength=1100).pack(side=tk.BOTTOM, fill=tk.X, before=body)

    def update_user_display(self):
        if self.current_user:
            disp = self.current_display or self.current_user
            role_tag = " [管理员]" if self.current_role == "admin" else ""
            self.user_status_var.set(f"已登录 · {disp} ({self.current_user}){role_tag}")
            self.btn_login.configure(text="切换用户")
            self.btn_logout.configure(state=tk.NORMAL)
            if self.current_role == "admin":
                self.btn_add_user.pack(side=tk.LEFT, padx=(0, 6))
            else:
                self.btn_add_user.pack_forget()
        else:
            self.user_status_var.set("未登录 · 登录后可使用团队账号")
            self.btn_login.configure(text="登录 / 注册")
            self.btn_logout.configure(state=tk.DISABLED)
            self.btn_add_user.pack_forget()

    def prompt_login_if_needed(self):
        if not self.current_user:
            self.open_login_window()

    def open_login_window(self):
        win = tk.Toplevel(self.root)
        win.title("用户登录 / 注册")
        win.geometry("440x390")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        x = self.root.winfo_x() + (self.root.winfo_width() - 440) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 390) // 2
        win.geometry(f"+{x}+{y}")

        ttk.Label(win, text="欢迎使用 Codex Auth Hub", font=("Microsoft YaHei UI", 15, "bold"), foreground="#17243c").pack(pady=(24, 6))
        ttk.Label(win, text="登录团队账号，开始使用共享资源", foreground="#78859a", font=("Microsoft YaHei UI", 9)).pack()

        f = ttk.Frame(win, padding=16)
        f.pack(fill=tk.BOTH, expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="用户名:").grid(row=0, column=0, sticky=tk.W, pady=6)
        u_entry = ttk.Entry(f, width=24)
        u_entry.grid(row=0, column=1, sticky=tk.EW, padx=(12, 0), pady=6)
        u_entry.insert(0, self.current_user or "admin")

        ttk.Label(f, text="密  码:").grid(row=1, column=0, sticky=tk.W, pady=6)
        p_entry = ttk.Entry(f, width=24, show="•")
        p_entry.grid(row=1, column=1, sticky=tk.EW, padx=(12, 0), pady=6)
        if (self.current_user or "") == "admin":
            p_entry.insert(0, "admin123")

        ttk.Label(f, text="姓名/昵称:").grid(row=2, column=0, sticky=tk.W, pady=6)
        d_entry = ttk.Entry(f, width=24)
        d_entry.grid(row=2, column=1, sticky=tk.EW, padx=(12, 0), pady=6)
        d_entry.insert(0, self.current_display or "")
        ttk.Label(f, text="(仅注册新用户时填写)", font=("Segoe UI", 8), foreground="gray").grid(row=3, column=1, sticky=tk.W)

        btn_row = ttk.Frame(win, padding=10)
        btn_row.pack(fill=tk.X)

        def on_login():
            uname = u_entry.get().strip()
            pwd = p_entry.get().strip()
            if not uname or not pwd:
                messagebox.showwarning("提示", "请输入用户名和密码", parent=win)
                return
            server = self.server_url_var.get().strip().rstrip("/")

            def _login_thread():
                try:
                    body = json.dumps({"username": uname, "password": pwd}).encode("utf-8")
                    req = urllib.request.Request(f"{server}/api/auth/login", data=body, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    token = data.get("token", "")
                    user_info = data.get("user", {})
                    self.root.after(0, lambda: self._on_login_ok(token, user_info, win))
                except Exception as e:
                    self.root.after(0, lambda err=e: messagebox.showerror("登录失败", f"无法登录:\n{err}", parent=win))

            threading.Thread(target=_login_thread, daemon=True).start()

        def on_register():
            uname = u_entry.get().strip()
            pwd = p_entry.get().strip()
            disp = d_entry.get().strip() or uname
            if not uname or not pwd:
                messagebox.showwarning("提示", "请输入用户名和密码", parent=win)
                return
            server = self.server_url_var.get().strip().rstrip("/")

            def _reg_thread():
                try:
                    body = json.dumps({"username": uname, "password": pwd, "display_name": disp}).encode("utf-8")
                    req = urllib.request.Request(f"{server}/api/auth/register", data=body, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    token = data.get("token", "")
                    user_info = data.get("user", {})
                    self.root.after(0, lambda: self._on_register_ok(token, user_info, win))
                except Exception as e:
                    self.root.after(0, lambda err=e: messagebox.showerror("注册失败", f"注册异常:\n{err}", parent=win))

            threading.Thread(target=_reg_thread, daemon=True).start()

        ttk.Button(btn_row, text="登录", style="Primary.TButton", command=on_login).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btn_row, text="注册新用户", command=on_register).pack(side=tk.RIGHT, padx=6)

    def _on_login_ok(self, token, user_info, win):
        self.auth_token = token
        self.current_user = user_info.get("username", "")
        self.current_display = user_info.get("display_name", self.current_user)
        self.current_role = user_info.get("role", "user")
        self.update_user_display()
        self.save_config()
        self.refresh_profiles()
        win.destroy()
        messagebox.showinfo("登录成功", f"欢迎回来，{self.current_display}！")

    def _on_register_ok(self, token, user_info, win):
        self.auth_token = token
        self.current_user = user_info.get("username", "")
        self.current_display = user_info.get("display_name", self.current_user)
        self.current_role = user_info.get("role", "user")
        self.update_user_display()
        self.save_config()
        self.refresh_profiles()
        win.destroy()
        messagebox.showinfo("注册成功", f"用户 [{self.current_user}] 注册成功并已自动登录！")

    def open_admin_add_user_window(self):
        """管理员添加团队用户弹窗"""
        win = tk.Toplevel(self.root)
        win.title("管理员添加团队新成员")
        win.geometry("440x390")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        x = self.root.winfo_x() + (self.root.winfo_width() - 440) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 390) // 2
        win.geometry(f"+{x}+{y}")

        ttk.Label(win, text="添加团队成员", font=("Microsoft YaHei UI", 15, "bold"), foreground="#17243c").pack(pady=(24, 6))
        ttk.Label(win, text="创建登录身份并设置成员权限", foreground="#78859a", font=("Microsoft YaHei UI", 9)).pack()

        f = ttk.Frame(win, padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="用户名:").grid(row=0, column=0, sticky=tk.W, pady=6)
        u_entry = ttk.Entry(f, width=24)
        u_entry.grid(row=0, column=1, pady=6)

        ttk.Label(f, text="初始密码:").grid(row=1, column=0, sticky=tk.W, pady=6)
        p_entry = ttk.Entry(f, width=24)
        p_entry.grid(row=1, column=1, pady=6)
        p_entry.insert(0, "123456")

        ttk.Label(f, text="姓名/称呼:").grid(row=2, column=0, sticky=tk.W, pady=6)
        d_entry = ttk.Entry(f, width=24)
        d_entry.grid(row=2, column=1, pady=6)

        ttk.Label(f, text="角色:").grid(row=3, column=0, sticky=tk.W, pady=6)
        role_combo = ttk.Combobox(f, values=["user (普通成员)", "admin (管理员)"], state="readonly", width=22)
        role_combo.grid(row=3, column=1, pady=6)
        role_combo.current(0)

        btn_row = ttk.Frame(win, padding=10)
        btn_row.pack(fill=tk.X)

        def on_add():
            uname = u_entry.get().strip()
            pwd = p_entry.get().strip()
            disp = d_entry.get().strip() or uname
            role = "admin" if "admin" in role_combo.get() else "user"

            if not uname or not pwd:
                messagebox.showwarning("提示", "用户名与密码不能为空", parent=win)
                return

            server = self.server_url_var.get().strip().rstrip("/")

            def _add_thread():
                try:
                    body = json.dumps({"username": uname, "password": pwd, "display_name": disp, "role": role}).encode("utf-8")
                    req = urllib.request.Request(f"{server}/api/admin/users", data=body, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                    self.root.after(0, lambda: self._on_admin_add_user_ok(uname, win))
                except Exception as e:
                    self.root.after(0, lambda err=e: messagebox.showerror("添加失败", f"错误:\n{err}", parent=win))

            threading.Thread(target=_add_thread, daemon=True).start()

        ttk.Button(btn_row, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btn_row, text="确认创建", style="Primary.TButton", command=on_add).pack(side=tk.RIGHT, padx=6)

    def _on_admin_add_user_ok(self, uname, win):
        win.destroy()
        messagebox.showinfo("成功", f"团队成员 [{uname}] 创建成功！")
        self.refresh_profiles()

    def do_logout(self):
        if self.session_token:
            self.release_current_session(silent=True)
        self.auth_token = ""
        self.current_user = ""
        self.current_display = ""
        self.current_role = "user"
        self.update_user_display()
        self.save_config()
        self.active_account_var.set("尚未使用账号")
        messagebox.showinfo("已注销", "当前用户已退出登录。")

    def verify_saved_token(self):
        if not self.auth_token:
            return
        server = self.server_url_var.get().strip().rstrip("/")
        try:
            req = urllib.request.Request(f"{server}/api/auth/me", headers={"Authorization": f"Bearer {self.auth_token}"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            user = data.get("user", {})
            self.current_user = user.get("username", self.current_user)
            self.current_display = user.get("display_name", self.current_user)
            self.current_role = user.get("role", "user")
            self.root.after(0, self.update_user_display)

            act = data.get("active_session")
            if act:
                self.session_token = act.get("session_token")
                self.active_profile_id = act.get("profile_id")
                self.active_profile_name = act.get("profile_name")
                self.is_heartbeat_running = True
                threading.Thread(target=self.heartbeat_loop, daemon=True).start()
                self.root.after(0, lambda: self.active_account_var.set(self.active_profile_name))
                self.root.after(0, lambda: self.release_btn.configure(state=tk.NORMAL))
        except Exception:
            self.auth_token = ""
            self.root.after(0, self.update_user_display)

    def on_select_item(self, event):
        selected = self.tree.selection()
        if not selected:
            self.apply_btn.configure(state=tk.DISABLED)
            return

        item = self.tree.item(selected[0])
        values = item["values"]
        pid, pname, plan, q5, qw, users_cnt, online_u, email = values

        self.selected_label.configure(
            text=f"{pname}  ·  {plan}  ·  使用人数 {users_cnt}"
        )
        self.apply_btn.configure(state=tk.NORMAL)

    def refresh_profiles(self):
        """完全非阻塞异步拉取"""
        if self._is_fetching:
            return
        self._is_fetching = True
        threading.Thread(target=self._worker_fetch_profiles, daemon=True).start()

    def _worker_fetch_profiles(self):
        url = self.server_url_var.get().strip().rstrip("/") + "/api/profiles"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CodexClientGUI/1.2"})
            with urllib.request.urlopen(req, timeout=2.5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    self.root.after(0, lambda: self._on_fetch_success(data, url))
                    return
        except Exception as e:
            err_msg = str(e)
            self.root.after(0, lambda: self._on_fetch_error(err_msg, url))
        finally:
            self._is_fetching = False

    def _on_fetch_success(self, data, url):
        self.update_tree(data)
        self.status_var.set(f"🟢 已连接服务端 [{url}] | 账号列表已更新 ({len(data)} 个)")

    def _on_fetch_error(self, err_msg, url):
        if not self.profiles_cache:
            self.empty_label.configure(text="暂时无法连接服务端，请检查地址后刷新")
        self.status_var.set(f"连接失败: {err_msg} | 请检查服务端 IP ({url}) 与端口 8000 是否正确放通")

    def update_tree(self, profiles):
        self.profiles_cache = profiles
        scroll_position = self.tree.yview()[0]
        selected_id = None
        current_sel = self.tree.selection()
        if current_sel:
            selected_id = self.tree.item(current_sel[0])["values"][0]

        # 核心：自动扫描数据库返回的占用状态，同步当前用户的占用与心跳
        occupied_p = None
        occupied_user_info = None
        if self.current_user:
            for p in profiles:
                for u in (p.get("active_users") or []):
                    if u.get("client_user") == self.current_user:
                        occupied_p = p
                        occupied_user_info = u
                        break
                if occupied_p:
                    break

        if occupied_p:
            self.active_profile_id = occupied_p["id"]
            self.active_profile_name = occupied_p["name"]
            if occupied_user_info and occupied_user_info.get("session_token"):
                self.session_token = occupied_user_info["session_token"]
            self.active_account_var.set(occupied_p["name"])
            self.release_btn.configure(state=tk.NORMAL)
            if not self.is_heartbeat_running and self.session_token:
                self.is_heartbeat_running = True
                threading.Thread(target=self.heartbeat_loop, daemon=True).start()
            self.save_config()
        elif not self.session_token:
            self.active_profile_id = None
            self.active_profile_name = None
            self.active_account_var.set("尚未使用账号")
            self.release_btn.configure(state=tk.DISABLED)

        self.tree.delete(*self.tree.get_children())
        self.tree.configure(height=max(1, min(len(profiles), 3)))
        columns = tuple(self.tree["columns"])
        has_window_metadata = any("available" in ((p.get("quota") or {}).get("five_hour") or {}) for p in profiles)
        has_five_hour = any(((p.get("quota") or {}).get("five_hour") or {}).get("available") for p in profiles)
        self.tree.configure(displaycolumns=columns if not has_window_metadata or has_five_hour else tuple(c for c in columns if c != "quota_5h"))
        self.profile_count_var.set(str(len(profiles)))
        self.usage_count_var.set(str(sum(len(p.get("active_users") or []) for p in profiles)))
        if profiles:
            self.empty_label.place_forget()
        else:
            self.empty_label.configure(text="暂无团队账号，请联系管理员添加")
            self.empty_label.place(relx=0.5, rely=0.42, anchor=tk.CENTER)
        self.apply_btn.configure(state=tk.DISABLED)
        self.selected_label.configure(text="选择一个账号开始使用")
        for index, p in enumerate(profiles):
            q = p.get("quota") or {}
            q5 = q.get("five_hour") or {}
            qw = q.get("weekly") or {}

            q5_text = format_quota_window(q5)
            qw_text = format_quota_window(qw)

            active_users = p.get("active_users") or []
            u_names = []
            for u in active_users:
                d_name = u.get("display_name") or u.get("client_user")
                tag = f" ({u.get('online_tag', '在线')})" if u.get("online_tag") else ""
                u_names.append(f"{d_name}{tag}")
            u_str = ", ".join(u_names) if u_names else "闲置"

            limit_str = f"{len(active_users)}"
            if p.get("max_users", 0) > 0:
                limit_str += f"/{p['max_users']}"

            item_id = self.tree.insert(
                "", tk.END,
                tags=("current" if p["id"] == self.active_profile_id else ("odd" if index % 2 else "even"),),
                values=(
                    p["id"],
                    p["name"],
                    format_plan_name(p.get("plan_type")),
                    q5_text,
                    qw_text,
                    limit_str,
                    u_str,
                    p.get("account_email") or "-"
                )
            )

            if selected_id is not None and str(p["id"]) == str(selected_id):
                self.tree.selection_set(item_id)
                self.on_select_item(None)

        self.tree.yview_moveto(0 if len(profiles) <= 3 else scroll_position)

    def apply_selected_profile(self):
        if not self.current_user:
            messagebox.showwarning("提示", "请先点击上方「用户登录 / 注册」登录您的账号后再应用认证！")
            self.open_login_window()
            return

        selected = self.tree.selection()
        if not selected:
            return

        pid = self.tree.item(selected[0])["values"][0]
        pname = self.tree.item(selected[0])["values"][1]
        uname = self.current_user

        self.apply_btn.configure(state=tk.DISABLED)
        self.status_var.set(f"正在向服务端申请并写入本地认证 [{pname}]...")

        threading.Thread(
            target=self._worker_apply_profile, 
            args=(pid, pname, uname), 
            daemon=True
        ).start()

    def _worker_apply_profile(self, pid, pname, uname):
        # 释放旧的 session
        if self.session_token:
            self._worker_release_session(self.session_token, silent=True)

        session_token = str(uuid.uuid4())
        server_base = self.server_url_var.get().strip().rstrip("/")
        url = f"{server_base}/api/sessions/apply"
        body = json.dumps({
            "profile_id": pid,
            "client_user": uname,
            "client_hostname": self.hostname,
            "session_token": session_token
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                url, 
                data=body, 
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.root.after(0, lambda err=e: self._on_apply_error(err))
            return

        local_path = get_default_codex_path()
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            if os.path.exists(local_path):
                bak_path = local_path + ".bak"
                with open(local_path, "rb") as src, open(bak_path, "wb") as dst:
                    dst.write(src.read())

            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(result["auth_json"], f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.root.after(0, lambda err=e: messagebox.showerror("写入本地认证失败", f"写入 {local_path} 失败:\n{err}"))
            return

        self.root.after(0, lambda: self._on_apply_success(pid, pname, uname, session_token, local_path))

    def _on_apply_error(self, err):
        self.apply_btn.configure(state=tk.NORMAL)
        messagebox.showerror("申请失败", f"无法连接服务端或该账号人数已满:\n{err}")

    def _on_apply_success(self, pid, pname, uname, session_token, local_path):
        self.session_token = session_token
        self.active_profile_id = pid
        self.active_profile_name = pname
        self.is_heartbeat_running = True
        self.release_btn.configure(state=tk.NORMAL)
        self.apply_btn.configure(state=tk.NORMAL)
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

        self.active_account_var.set(pname)
        self.status_var.set(f"🟢 用户 [{uname}] 独占生效: [{pname}] | 本地认证已替换 | 维持心跳中")
        self.refresh_profiles()

        msg = (
            f"✅ 账号 [{pname}] 的认证凭据已成功下载并替换本地认证:\n{local_path}\n\n"
            f"已为您在数据库中自动清除此前占用的旧账号！\n\n"
            "💡【VS Code 插件免重启快速生效】:\n"
            "在 VS Code 窗口中按「Ctrl + Shift + P」\n"
            "输入「Reload Window」(开发人员: 重新加载窗口) 并回车，1秒内即可生效最新认证！"
        )
        messagebox.showinfo("本地认证替换成功", msg)

    def heartbeat_loop(self):
        server_base = self.server_url_var.get().strip().rstrip("/")
        url = f"{server_base}/api/sessions/heartbeat"
        while self.is_heartbeat_running and self.session_token:
            time.sleep(30)
            if not self.is_heartbeat_running:
                break
            try:
                body = json.dumps({"session_token": self.session_token}).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    pass
            except Exception as e:
                print("Heartbeat warning:", e)

    def release_current_session(self, silent=False):
        if not self.session_token:
            return
        token = self.session_token
        self.is_heartbeat_running = False
        self.session_token = None
        self.active_profile_id = None
        self.active_profile_name = None

        self.release_btn.configure(state=tk.DISABLED)
        self.active_account_var.set("尚未使用账号")
        self.status_var.set("已释放会话占用。")

        threading.Thread(target=self._worker_release_session, args=(token, silent), daemon=True).start()

    def _worker_release_session(self, token, silent=False):
        server_base = self.server_url_var.get().strip().rstrip("/")
        url = f"{server_base}/api/sessions/release"
        try:
            body = json.dumps({"session_token": token}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                pass
        except Exception:
            pass

        self.root.after(0, self.refresh_profiles)
        if not silent:
            self.root.after(0, lambda: messagebox.showinfo("已断开", "已成功释放该服务端的使用占位！"))

    def open_env_checker_window(self):
        server = self.server_url_var.get().strip().rstrip("/")
        EnvInstallerDialog(self.root, server)

    def reload_vscode_prompt(self):
        messagebox.showinfo(
            "VS Code 快速重载方法",
            "在 VS Code 中重新加载认证只需 1 秒：\n\n"
            "1. 切换到 VS Code 窗口\n"
            "2. 按快捷键「Ctrl + Shift + P」(Mac为 Cmd+Shift+P)\n"
            "3. 输入「Reload Window」(开发人员: 重新加载窗口) 并回车\n\n"
            "窗口瞬间刷新，插件会立即使用本地最新的 auth.json，完全无需重启 VS Code！"
        )

    def start_auto_refresh(self):
        self.refresh_profiles()
        self.poll_timer = self.root.after(6000, self.start_auto_refresh)

    def on_closing(self):
        self.save_config()
        # 窗口关闭时不释放账号，保留在本地 ~/.codex/auth.json 供 VS Code 正常开发使用。
        # 只有在界面显式点击「释放当前账号占用」时才退还给团队。
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = CodexClientApp(root)
    root.mainloop()
