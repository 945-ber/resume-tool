# -*- coding: utf-8 -*-
"""简历智造 · 一键启动器
双击即用：自动拉起本地服务 → 显示局域网直连（同一 WiFi 手机扫码访问）。
GUI 窗口保持服务在线；关闭窗口后服务保留后台运行。"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

try:
    import qrcode
    from PIL import Image, ImageTk
    QR_OK = True
except Exception:
    QR_OK = False

ROOT = os.path.dirname(os.path.abspath(__file__))
# 服务用同目录 python.exe 启动（pythonw 无控制台，uvicorn 在某些环境下起不来/不监听）
_alt_py = os.path.join(os.path.dirname(sys.executable), "python.exe")
PY = _alt_py if os.path.exists(_alt_py) else sys.executable
BACKEND = os.path.join(ROOT, "backend")
PORT = 8000
BG = "#0b1220"
CARD = "#131c2e"
TEXT = "#e6edf7"
SUB = "#8fa3bf"
ACCENT = "#2f6bff"
OK = "#34d399"
WARN = "#fbbf24"

CREATE_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
UV_LOG = os.path.join(ROOT, "data", "uvicorn.log")
LAUNCH_LOG = os.path.join(ROOT, "data", "launcher.log")


def log(msg):
    """启动器运行日志（排查问题时能直接看）"""
    try:
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        with open(LAUNCH_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def http_up():
    """HTTP 级探测：页面真能返回 200 才算服务就绪（比纯端口检测可靠，能识别僵尸进程）"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def uvicorn_ok():
    try:
        r = subprocess.run([PY, "-m", "uvicorn", "--version"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def kill_port_owner():
    """杀掉占用 8000 端口的进程（防止上次遗留的僵尸 uvicorn 占着端口却无响应）"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
        pids = set()
        for line in out.splitlines():
            if ":8000" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.Popen(["taskkill", "/f", "/pid", pid],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        if pids:
            log("清理占用 8000 的进程: " + ",".join(pids))
    except Exception:
        pass


def start_service():
    """拉起本地服务，输出到 uvicorn.log（失败时能读出真实原因）"""
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(UV_LOG, "w", encoding="utf-8") as flog:
        subprocess.Popen(
            [PY, "-m", "uvicorn", "main:app", "--app-dir", BACKEND,
             "--host", "0.0.0.0", "--port", str(PORT)],
            cwd=ROOT, creationflags=CREATE_FLAGS,
            stdout=flog, stderr=subprocess.STDOUT,
        )


def read_uvicorn_error():
    try:
        tail = open(UV_LOG, encoding="utf-8").read().strip()
        if tail:
            lines = [l for l in tail.splitlines() if l.strip()]
            return lines[-1][-90:]
    except Exception:
        pass
    return "（无日志输出）"


def lan_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


class Launcher:
    def __init__(self, root):
        self.root = root
        root.title("简历智造 · 启动器")
        root.configure(bg=BG)
        root.geometry("480x560")
        root.resizable(False, False)
        try:
            root.iconbitmap(default="")
        except Exception:
            pass

        self.f_title = tkfont.Font(family="Microsoft YaHei UI", size=20, weight="bold")
        self.f_sub = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self.f_body = tkfont.Font(family="Microsoft YaHei UI", size=11)
        self.f_small = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.f_url = tkfont.Font(family="Consolas", size=11, weight="bold")

        self._build()
        threading.Thread(target=self.worker, daemon=True).start()

    def _card(self, parent, y, h):
        return tk.Frame(parent, bg=CARD, highlightthickness=0)

    def _build(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=26, pady=(26, 6))
        tk.Label(head, text="简历智造", bg=BG, fg=TEXT, font=self.f_title).pack(anchor="w")
        tk.Label(head, text="智能简历改写工具 · 一键启动", bg=BG, fg=SUB, font=self.f_sub).pack(anchor="w", pady=(2, 0))

        # 服务状态
        c1 = self._card(self.root, 0, 0)
        c1.pack(fill="x", padx=26, pady=10)
        self.lbl_service = tk.Label(c1, text="●  本地服务：启动中…", bg=CARD, fg=WARN, font=self.f_body, anchor="w")
        self.lbl_service.pack(fill="x", padx=16, pady=12)

        # 局域网直连（手机同 WiFi）
        c3 = self._card(self.root, 0, 0)
        c3.pack(fill="x", padx=26, pady=(0, 10))
        self.lbl_lan = tk.Label(c3, text="●  局域网直连：检测中…", bg=CARD, fg=SUB, font=self.f_body, anchor="w")
        self.lbl_lan.pack(fill="x", padx=16, pady=(12, 2))
        self.qr_box = tk.Frame(c3, bg="#0e1730")
        self.qr_box.pack(fill="x", padx=16, pady=(2, 10))
        self.lbl_qr = tk.Label(self.qr_box, text="扫码直达（手机连同一 WiFi）", bg="#0e1730",
                               fg=SUB, font=self.f_small, anchor="w")
        self.lbl_qr.pack(side="left", fill="x", expand=True, padx=14, pady=8)
        self.qr_img = tk.Label(self.qr_box, bg="#0e1730")
        self.qr_img.pack(side="right", padx=12, pady=6)

        # 操作按钮
        ops = tk.Frame(self.root, bg=BG)
        ops.pack(fill="x", padx=26, pady=6)
        tk.Button(ops, text="打开应用", command=self.open_app, bg=ACCENT, fg="white",
                  activebackground="#2456d6", activeforeground="white", relief="flat",
                  font=self.f_body, cursor="hand2", padx=18, pady=8).pack(side="left", expand=True, fill="x")

        foot = tk.Label(self.root, text="手机访问：同一 WiFi 下用「局域网直连」扫码即可。\n关闭本窗口仅退出启动器，服务保留后台运行。\n本地服务异常时会自动检测并重新拉起。",
                        bg=BG, fg=SUB, font=self.f_small, wraplength=440, justify="center")
        foot.pack(side="bottom", pady=12)

    # ---------- 后台线程 ----------
    def worker(self):
        log("=== 启动器启动 ===")
        # 1. 确保本地服务就绪（HTTP 探测 + 僵尸清理 + 失败诊断）
        self.ensure_service()
        # 2. 局域网直连（不阻塞）
        ips = lan_ips()
        if ips:
            url = f"http://{ips[0]}:{PORT}"
            self.set_lan(f"●  局域网直连：手机连同一 WiFi，访问  {url}", url)
        else:
            self.set_lan("●  局域网直连：未检测到局域网 IP（手机需与电脑连同一 WiFi）", "")
        # 3. 服务守护线程（服务掉线自动拉起）
        threading.Thread(target=self.guard_loop, daemon=True).start()
        # 4. 就绪后自动打开应用
        self.root.after(1200, self.auto_open)

    def ensure_service(self):
        if http_up():
            self.set_service("运行中 ✓  http://127.0.0.1:8000", ok=True)
            log("服务已在线（HTTP 探测通过）")
            return
        if not uvicorn_ok():
            msg = "缺少 uvicorn 依赖，无法启动。请先运行：pip install uvicorn"
            self.set_service(msg, warn=True)
            log(msg)
            return
        # 服务没起来：先清僵尸端口占用者，再启动
        kill_port_owner()
        time.sleep(1)
        self.set_service("启动中…（拉起本地服务，首次约 5-15 秒）")
        start_service()
        for _ in range(60):
            time.sleep(1)
            if http_up():
                self.set_service("运行中 ✓  http://127.0.0.1:8000", ok=True)
                log("服务启动成功")
                return
        reason = read_uvicorn_error()
        self.set_service(f"启动失败：{reason}", warn=True)
        log("服务启动失败: " + reason)

    def guard_loop(self):
        """守护：启动器运行期间每 10 秒探测，服务掉线自动拉起"""
        while True:
            time.sleep(10)
            if http_up():
                continue
            log("守护：检测到服务掉线，自动拉起")
            kill_port_owner()
            time.sleep(1)
            start_service()
            recovered = False
            for _ in range(20):
                time.sleep(1)
                if http_up():
                    self.set_service("运行中 ✓  http://127.0.0.1:8000（已自动恢复）", ok=True)
                    recovered = True
                    break
            if not recovered:
                self.set_service("服务掉线且自动恢复失败，见 data\\uvicorn.log", warn=True)
                log("守护恢复失败: " + read_uvicorn_error())

    def auto_open(self):
        """服务就绪后自动打开应用（只自动打开一次）"""
        if not getattr(self, "_auto_opened", False) and http_up():
            self._auto_opened = True
            import webbrowser
            webbrowser.open("http://127.0.0.1:8000")
            log("已自动打开应用")

    # ---------- UI 更新 ----------
    def set_service(self, text, ok=False, warn=False):
        color = WARN if warn else (OK if ok else WARN)
        self.root.after(0, lambda: self.lbl_service.config(text=f"●  本地服务：{text}", fg=color))

    def set_lan(self, text, url=""):
        # 全部 UI 与二维码生成放主线程，避免 tkinter 跨线程崩溃
        self.root.after(0, lambda: self._set_lan_ui(text, url))

    def _set_lan_ui(self, text, url):
        self.lbl_lan.config(text=text, fg=SUB)
        if QR_OK and url:
            try:
                img = qrcode.make(url)
                img = img.resize((92, 92), Image.LANCZOS)
                self._qr_photo = ImageTk.PhotoImage(img)
                self.qr_img.config(image=self._qr_photo)
                self.lbl_qr.config(text="扫码直达（手机连同一 WiFi）", fg="#7dd3fc")
            except Exception:
                pass

    # ---------- 动作 ----------
    def open_app(self):
        import webbrowser
        webbrowser.open("http://127.0.0.1:8000")

    def on_close(self):
        # 立即关窗：服务保留后台，随后强制退出，绝不卡死
        log("关闭窗口：退出启动器，服务保留后台")
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


def main():
    root = tk.Tk()
    app = Launcher(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
