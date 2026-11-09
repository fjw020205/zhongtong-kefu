"""中通在线客服 + Dify AI 自动对话助手（tkinter 前端）。

使用流程：
1. 点击“启动浏览器”，在打开的浏览器里手动登录中通账号；
2. 点击“已登录 → 回首页并打开客服”，脚本会回到首页并自动点击“在线客服”；
3. 检测到客服对话框后，可手动发送测试消息，或填写目标后开始自动多轮对话；
4. 自动对话中，每一条发给中通客服的消息都由 Dify 生成。
"""

from __future__ import annotations

import json
import os
import queue
import random
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from browser_bot import BrowserWorker, find_browser_path
from dify_client import APP_CHAT, APP_COMPLETION, APP_WORKFLOW, DifyClient, DifyError

APP_HOME = Path(os.environ.get("APPDATA") or Path.home()) / "ZtoKefuBot"
CONFIG_PATH = APP_HOME / "config.json"
PROFILE_DIR = APP_HOME / "browser-profile"

APP_TYPES = {
    "聊天助手 / Chatflow（chat-messages）": APP_CHAT,
    "文本生成（completion-messages）": APP_COMPLETION,
    "工作流（workflows/run）": APP_WORKFLOW,
}
APP_TYPES_REVERSE = {value: label for label, value in APP_TYPES.items()}

DEFAULT_CONFIG = {
    "browser_path": "",
    "dify_base_url": "https://api.dify.ai/v1",
    "dify_api_key": "",
    "dify_app_type": APP_CHAT,
    "dify_user": "zto-kefu-bot",
    "dify_inputs": "{}",
    "dify_use_conversation": False,
    "goal": "",
    "first_message": "",
    "max_turns": 10,
    "reply_timeout": 90,
}

# 待投诉的中通运单号（脚本内维护）。列表为空或全无效时，随机模拟一个 12 位单号。
ORDER_NUMBERS = [
    "786543210987",
    "789012345678",
    "781234567890",
    "785533117799",
]

# 单条消息字数上限：超过则按标点拆成多条逐条发送。
MAX_REPLY_CHARS = 20


def split_message(text: str, limit: int = MAX_REPLY_CHARS) -> list[str]:
    """按句号/问号/逗号/换行把一段回复拆成多条短消息，每条不超过 limit 字。"""
    parts = re.split(r"[。！？；!?;\n]+|(?<=[，,、])", (text or "").strip())
    segments: list[str] = []
    for part in parts:
        piece = part.strip().strip("，,、 ")
        while len(piece) > limit:
            segments.append(piece[:limit])
            piece = piece[limit:]
        if piece:
            segments.append(piece)
    return segments


def pick_order_number() -> str:
    """从维护的列表里取一个有效单号，取不到就模拟一个 12 位数字单号。"""
    valid = [str(item).strip() for item in ORDER_NUMBERS if str(item).strip().isdigit()]
    if valid:
        return random.choice(valid)
    return "7" + "".join(random.choice("0123456789") for _ in range(11))


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            config.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except (OSError, ValueError):
        pass
    return config


def save_config(config: dict) -> None:
    """先写临时文件再原子替换，避免写入中断把 config.json 写坏。"""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.parent / (CONFIG_PATH.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, CONFIG_PATH)


def safe_int(value, default: int, minimum: int, maximum: int) -> int:
    """配置里的数字可能是字符串或脏数据，统一转换并夹到合法区间。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = load_config()
        self.events: queue.Queue = queue.Queue()
        self.ui_tasks: queue.Queue = queue.Queue()
        self.messages: list[dict] = []

        self.launched = False
        self.chat_ready = False

        self.auto_running = False
        self.auto_state = "idle"  # idle / waiting / generating
        self.auto_turn = 0
        self.auto_last_send_index = 0
        self.auto_max_turns = 10
        self.auto_reply_timeout = 90
        self.auto_goal = ""
        self.auto_order_no = ""
        self.auto_history: list[tuple[str, str]] = []
        self.auto_conversation_id: str | None = None
        self.last_sent = ""
        # 批次令牌：停止或开启新一轮后，旧的分段发送链 / 旧的 Dify 请求必须作废
        self.auto_send_token = 0
        self.auto_generation = 0

        self._reply_job = None
        self._debounce_job = None
        self._batch_job = None
        self._agent_buffer: dict[int, str] = {}
        self._closing = False

        self.worker = BrowserWorker(self.events, str(PROFILE_DIR))
        self.worker.start()

        self._build_ui()
        self._refresh_controls()
        self._refresh_status()
        self.root.after(120, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================================================================== 界面
    def _build_ui(self) -> None:
        root = self.root
        root.title("中通在线客服 · Dify 自动对话助手")
        root.geometry("1180x880")
        root.minsize(1020, 720)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 9))
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", foreground="#6b7280", font=("Microsoft YaHei UI", 9))

        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root, padding=(14, 12, 14, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="中通在线客服 · Dify 自动对话助手",
            font=("Microsoft YaHei UI", 16, "bold"),
            fg="#123a6b",
        ).grid(row=0, column=0, sticky="w")

        settings = ttk.Frame(root, padding=(14, 4, 14, 4))
        settings.grid(row=1, column=0, sticky="ew")
        settings.columnconfigure(0, weight=1, uniform="settings")
        settings.columnconfigure(1, weight=1, uniform="settings")

        self._build_browser_frame(settings)
        self._build_dify_frame(settings)

        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.grid(row=2, column=0, sticky="nsew", padx=14, pady=(4, 4))

        chat_frame = ttk.LabelFrame(paned, text="③ 对话记录", padding=8)
        paned.add(chat_frame, weight=3)
        self._build_chat_area(chat_frame)

        auto_frame = ttk.LabelFrame(paned, text="④ 自动对话", padding=8)
        paned.add(auto_frame, weight=1)
        self._build_auto_area(auto_frame)

        log_frame = ttk.LabelFrame(root, text="运行日志", padding=(8, 4, 8, 6))
        log_frame.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 4))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = ScrolledText(
            log_frame,
            height=6,
            wrap="word",
            font=("Microsoft YaHei UI", 9),
            state="disabled",
            background="#fbfbfd",
        )
        self.log_text.grid(row=0, column=0, sticky="ew")
        self.log_text.tag_config("error", foreground="#c62828")
        self.log_text.tag_config("warn", foreground="#b26a00")
        self.log_text.tag_config("info", foreground="#333333")

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 4)).grid(
            row=4, column=0, sticky="ew"
        )

    def _build_browser_frame(self, parent) -> None:
        frame = ttk.LabelFrame(parent, text="① 浏览器 / 登录（登录由您本人完成）", padding=8)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="浏览器路径").grid(row=0, column=0, sticky="w")
        self.browser_path_var = tk.StringVar(value=self.config.get("browser_path") or find_browser_path())
        ttk.Entry(frame, textvariable=self.browser_path_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="选择…", width=7, command=self._choose_browser).grid(row=0, column=2)

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.btn_launch = ttk.Button(buttons, text="启动浏览器", command=self._launch_browser)
        self.btn_launch.pack(side="left")
        self.btn_login = ttk.Button(buttons, text="已登录 → 回首页并打开客服", command=self._login_done)
        self.btn_login.pack(side="left", padx=6)
        self.btn_detect = ttk.Button(buttons, text="检测客服页面", command=self._detect_chat)
        self.btn_detect.pack(side="left")
        self.btn_close_browser = ttk.Button(buttons, text="关闭浏览器窗口", command=self._close_browser)
        self.btn_close_browser.pack(side="left", padx=6)

        ttk.Label(
            frame,
            text=(
                "提示：登录状态保存在独立配置目录中；关闭本程序不会关闭浏览器窗口，"
                "下次启动会直接复用同一个窗口，不需要重复登录。"
            ),
            style="Hint.TLabel",
            wraplength=520,
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_dify_frame(self, parent) -> None:
        frame = ttk.LabelFrame(parent, text="② Dify AI 配置（所有自动回复由 Dify 生成）", padding=8)
        frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=0)

        ttk.Label(frame, text="Base URL").grid(row=0, column=0, sticky="w")
        self.dify_base_var = tk.StringVar(value=self.config.get("dify_base_url", ""))
        ttk.Entry(frame, textvariable=self.dify_base_var).grid(row=0, column=1, columnspan=3, sticky="ew", padx=6)

        ttk.Label(frame, text="API Key").grid(row=1, column=0, sticky="w")
        self.dify_key_var = tk.StringVar(value=self.config.get("dify_api_key", ""))
        ttk.Entry(frame, textvariable=self.dify_key_var, show="*").grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=6
        )

        ttk.Label(frame, text="应用类型").grid(row=2, column=0, sticky="w")
        self.dify_app_var = tk.StringVar(
            value=APP_TYPES_REVERSE.get(self.config.get("dify_app_type", APP_CHAT), list(APP_TYPES)[0])
        )
        ttk.Combobox(frame, textvariable=self.dify_app_var, values=list(APP_TYPES), state="readonly").grid(
            row=2, column=1, sticky="ew", padx=6
        )
        ttk.Label(frame, text="用户标识").grid(row=2, column=2, sticky="w")
        self.dify_user_var = tk.StringVar(value=self.config.get("dify_user", "zto-kefu-bot"))
        ttk.Entry(frame, textvariable=self.dify_user_var, width=14).grid(row=2, column=3, sticky="ew", padx=(6, 0))

        ttk.Label(frame, text="输入变量").grid(row=3, column=0, sticky="w")
        self.dify_inputs_var = tk.StringVar(value=self.config.get("dify_inputs", "{}"))
        ttk.Entry(frame, textvariable=self.dify_inputs_var).grid(row=3, column=1, sticky="ew", padx=6)
        self.use_conversation_var = tk.BooleanVar(value=bool(self.config.get("dify_use_conversation")))
        ttk.Checkbutton(frame, text="保留 Dify 会话", variable=self.use_conversation_var).grid(
            row=3, column=2, columnspan=2, sticky="w"
        )

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="保存配置", command=self._save_config_clicked).pack(side="left")
        self.btn_test_dify = ttk.Button(actions, text="测试连接", command=self._test_dify)
        self.btn_test_dify.pack(side="left", padx=6)
        ttk.Label(actions, text="输入变量填 {} 即可，除非 Dify 应用定义了变量", style="Hint.TLabel").pack(
            side="left", padx=6
        )

    def _build_chat_area(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        self.chat_text = tk.Text(
            parent,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 10),
            background="#ffffff",
            relief="flat",
            padx=8,
            pady=6,
            spacing1=3,
            spacing3=3,
        )
        self.chat_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.chat_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_text.configure(yscrollcommand=scrollbar.set)
        self.chat_text.tag_config("agent", foreground="#0f7b3f", lmargin1=6, lmargin2=6)
        self.chat_text.tag_config("user", foreground="#1a5fb4", lmargin1=6, lmargin2=6)
        self.chat_text.tag_config("meta", foreground="#8a8f98", font=("Microsoft YaHei UI", 9))

        manual = ttk.Frame(parent)
        manual.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        manual.columnconfigure(0, weight=1)
        self.manual_var = tk.StringVar()
        manual_entry = ttk.Entry(manual, textvariable=self.manual_var)
        manual_entry.grid(row=0, column=0, sticky="ew")
        manual_entry.bind("<Return>", lambda _event: self._send_manual())
        self.btn_send = ttk.Button(manual, text="发送测试消息", command=self._send_manual)
        self.btn_send.grid(row=0, column=1, padx=(6, 0))

    def _build_auto_area(self, parent) -> None:
        parent.columnconfigure(0, weight=1)

        ttk.Label(parent, text="对话目标（交给 Dify 的诉求）").grid(row=0, column=0, sticky="w")
        self.goal_text = tk.Text(
            parent,
            height=5,
            wrap="word",
            font=("Microsoft YaHei UI", 9),
            relief="solid",
            borderwidth=1,
        )
        self.goal_text.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        self.goal_text.insert("1.0", self.config.get("goal", ""))

        ttk.Label(parent, text="首条消息（可选，留空则由 Dify 生成）").grid(row=2, column=0, sticky="w")
        self.first_message_var = tk.StringVar(value=self.config.get("first_message", ""))
        ttk.Entry(parent, textvariable=self.first_message_var).grid(row=3, column=0, sticky="ew", pady=(2, 8))

        options = ttk.Frame(parent)
        options.grid(row=4, column=0, sticky="ew")
        ttk.Label(options, text="最大轮数").pack(side="left")
        self.max_turns_var = tk.IntVar(value=safe_int(self.config.get("max_turns"), 10, 1, 50))
        ttk.Spinbox(options, from_=1, to=50, width=5, textvariable=self.max_turns_var).pack(side="left", padx=(4, 10))
        ttk.Label(options, text="回复超时(秒)").pack(side="left")
        self.reply_timeout_var = tk.IntVar(value=safe_int(self.config.get("reply_timeout"), 90, 10, 600))
        ttk.Spinbox(options, from_=10, to=600, width=5, textvariable=self.reply_timeout_var).pack(
            side="left", padx=(4, 0)
        )

        actions = ttk.Frame(parent)
        actions.grid(row=5, column=0, sticky="ew", pady=(10, 4))
        self.btn_auto_start = ttk.Button(actions, text="开始自动对话", command=self._start_auto)
        self.btn_auto_start.pack(side="left")
        self.btn_auto_stop = ttk.Button(actions, text="停止", command=lambda: self._stop_auto("手动停止"))
        self.btn_auto_stop.pack(side="left", padx=6)

        self.auto_status_var = tk.StringVar(value="自动对话：未开始")
        ttk.Label(parent, textvariable=self.auto_status_var, foreground="#7a3e9d", wraplength=300, justify="left").grid(
            row=6, column=0, sticky="w", pady=(2, 6)
        )
        ttk.Label(
            parent,
            text=(
                "说明：自动模式会把“目标 + 近期对话”发给 Dify，由 Dify 生成下一条要发给客服的话；"
                "Dify 输出 [DONE] 时自动结束。客服长时间不回复会按超时结束。"
            ),
            style="Hint.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=7, column=0, sticky="w")

    # ================================================================== 浏览器操作
    def _choose_browser(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Chrome / Edge 可执行文件",
            filetypes=[("浏览器程序", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.browser_path_var.set(path)

    def _launch_browser(self) -> None:
        path = self.browser_path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("未找到浏览器", "请先选择本机 Chrome 或 Edge 的可执行文件。")
            return
        self._log(f"正在启动浏览器：{path}")
        self.worker.commands.put({"type": "launch", "browser_path": path})

    def _login_done(self) -> None:
        self._log("已确认登录，正在回到首页并打开在线客服…")
        self.worker.commands.put({"type": "login_done"})

    def _detect_chat(self) -> None:
        self.worker.commands.put({"type": "detect_chat"})

    def _close_browser(self) -> None:
        self._stop_auto("关闭浏览器")
        self.worker.commands.put({"type": "close_browser"})

    # ================================================================== 消息发送
    def _send_manual(self) -> None:
        text = self.manual_var.get().strip()
        if not text:
            return
        if not self.chat_ready:
            messagebox.showinfo("尚未就绪", "还没有检测到客服对话框，请先进入在线客服。")
            return
        if self.auto_running:
            messagebox.showinfo("自动对话中", "自动对话进行中，请先停止自动对话再手动发送。")
            return
        self.manual_var.set("")
        self.worker.commands.put({"type": "send", "text": text})
        self._log(f"手动发送：{text}")

    # ================================================================== Dify
    def _make_dify_snapshot(self) -> dict:
        try:
            inputs = json.loads(self.dify_inputs_var.get().strip() or "{}")
        except ValueError as exc:
            raise DifyError(f"输入变量不是合法 JSON：{exc}") from exc
        if not isinstance(inputs, dict):
            raise DifyError("输入变量必须是 JSON 对象，例如 {}")
        return {
            "base_url": self.dify_base_var.get().strip(),
            "api_key": self.dify_key_var.get().strip(),
            "app_type": APP_TYPES.get(self.dify_app_var.get(), APP_CHAT),
            "user": self.dify_user_var.get().strip() or "zto-kefu-bot",
            "inputs": inputs,
            "use_conversation": bool(self.use_conversation_var.get()),
        }

    def _save_config_clicked(self) -> None:
        self._save_config()
        self._log("配置已保存")

    def _save_config(self) -> None:
        config = {
            "browser_path": self.browser_path_var.get().strip(),
            "dify_base_url": self.dify_base_var.get().strip(),
            "dify_api_key": self.dify_key_var.get().strip(),
            "dify_app_type": APP_TYPES.get(self.dify_app_var.get(), APP_CHAT),
            "dify_user": self.dify_user_var.get().strip(),
            "dify_inputs": self.dify_inputs_var.get().strip() or "{}",
            "dify_use_conversation": bool(self.use_conversation_var.get()),
            "goal": self.goal_text.get("1.0", "end").strip(),
            "first_message": self.first_message_var.get().strip(),
            "max_turns": int(self.max_turns_var.get() or 10),
            "reply_timeout": int(self.reply_timeout_var.get() or 90),
        }
        try:
            save_config(config)
        except OSError as exc:
            self._log(f"保存配置失败：{exc}", level="error")

    def _test_dify(self) -> None:
        try:
            snapshot = self._make_dify_snapshot()
        except DifyError as exc:
            messagebox.showwarning("配置有误", str(exc))
            return
        self.btn_test_dify.configure(state="disabled")
        self._log("正在测试 Dify 连接…")

        def worker() -> None:
            try:
                client = DifyClient(
                    snapshot["base_url"], snapshot["api_key"], snapshot["app_type"], snapshot["user"]
                )
                name = client.test_connection()
                self.ui_tasks.put(lambda: self._dify_test_done(True, f"Dify 连接成功：{name}"))
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                self.ui_tasks.put(lambda: self._dify_test_done(False, message))

        threading.Thread(target=worker, daemon=True).start()

    def _dify_test_done(self, ok: bool, message: str) -> None:
        self.btn_test_dify.configure(state="normal")
        self._log(message, level="info" if ok else "error")
        if ok:
            messagebox.showinfo("Dify", message)
        else:
            messagebox.showerror("Dify 连接失败", message)

    # ================================================================== 自动对话
    def _start_auto(self) -> None:
        if not self.chat_ready:
            messagebox.showinfo("尚未就绪", "请先打开中通在线客服对话框。")
            return
        goal = self.goal_text.get("1.0", "end").strip()
        if not goal:
            messagebox.showinfo("缺少目标", "请先填写本次要和客服沟通的目标。")
            return
        if not self.dify_key_var.get().strip():
            messagebox.showinfo("缺少 Dify 配置", "请先填写 Dify API Key 并测试连接。")
            return
        try:
            self._make_dify_snapshot()
        except DifyError as exc:
            messagebox.showwarning("配置有误", str(exc))
            return

        self.auto_running = True
        self.auto_state = "starting"
        self.auto_turn = 0
        self.auto_goal = goal
        self.auto_order_no = pick_order_number()
        self.auto_history = []
        self.auto_conversation_id = None
        self.last_sent = ""
        self.auto_last_send_index = len(self.messages)
        self._agent_buffer = {}
        # 开启新一轮：作废上一轮遗留的分段发送链与 Dify 请求
        self.auto_send_token += 1
        self.auto_generation += 1
        try:
            turns = int(self.max_turns_var.get())
        except (tk.TclError, ValueError):
            turns = 10
        try:
            timeout = int(self.reply_timeout_var.get())
        except (tk.TclError, ValueError):
            timeout = 90
        self.auto_max_turns = max(1, min(50, turns))
        self.auto_reply_timeout = max(10, min(600, timeout))
        self._cancel_timers()
        self._refresh_controls()
        self._set_auto_status(f"自动对话：已开始（最多 {self.auto_max_turns} 轮）")
        self._log(f"开始自动对话，目标：{goal}，订单号：{self.auto_order_no}")

        first = self.first_message_var.get().strip()
        if first:
            self._send_auto(first)
        else:
            self._request_dify(opening=True)

    def _stop_auto(self, reason: str = "") -> None:
        if not self.auto_running:
            return
        self.auto_running = False
        self.auto_state = "idle"
        self._cancel_timers()
        self._refresh_controls()
        self._set_auto_status("自动对话：已停止")
        if reason:
            self._log(f"自动对话结束：{reason}")

    def _cancel_timers(self) -> None:
        for attr in ("_reply_job", "_debounce_job", "_batch_job"):
            job = getattr(self, attr)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def _send_auto(self, text: str) -> None:
        if not self.auto_running:
            return
        if self.auto_turn >= self.auto_max_turns:
            self._stop_auto(f"已达到最大轮数（{self.auto_max_turns}）")
            return
        text = (text or "").strip()
        if not text:
            self._stop_auto("生成内容为空")
            return
        self._send_auto_batch([text])

    def _send_auto_batch(self, texts: list[str]) -> None:
        """把一条长回复拆成多条短消息，按顺序逐条发送，整批算一轮。"""
        if not self.auto_running:
            return
        if self.auto_turn >= self.auto_max_turns:
            self._stop_auto(f"已达到最大轮数（{self.auto_max_turns}）")
            return
        texts = [t for t in texts if t]
        if not texts:
            self._stop_auto("生成内容为空")
            return
        self.auto_turn += 1
        self.auto_state = "waiting"
        self.last_sent = texts[-1]
        self.auto_last_send_index = len(self.messages)
        self.auto_send_token += 1
        token = self.auto_send_token
        for item in texts:
            self.auto_history.append(("我方", item))
        self._set_auto_status(f"自动对话：已发送第 {self.auto_turn} 轮，等待客服回复…")

        def send_at(index: int) -> None:
            self._batch_job = None
            # 停止或已开始新一轮时，旧批次立刻作废，不能继续往新会话里发
            if not self.auto_running or token != self.auto_send_token:
                return
            if index >= len(texts):
                self._arm_reply_timeout()
                return
            self.worker.commands.put({"type": "send", "text": texts[index]})
            self._log(f"第 {self.auto_turn} 轮发送（{index + 1}/{len(texts)}）：{texts[index]}")
            self._batch_job = self.root.after(800, lambda: send_at(index + 1))

        send_at(0)

    def _arm_reply_timeout(self) -> None:
        if self._reply_job is not None:
            try:
                self.root.after_cancel(self._reply_job)
            except tk.TclError:
                pass
        self._reply_job = self.root.after(self.auto_reply_timeout * 1000, self._on_reply_timeout)

    def _on_reply_timeout(self) -> None:
        self._reply_job = None
        if self.auto_running and self.auto_state == "waiting":
            self._stop_auto(f"等待客服回复超时（{self.auto_reply_timeout} 秒）")

    def _schedule_agent_reply(self, index, text: str) -> None:
        if not text:
            return
        self._agent_buffer[int(index)] = text
        if self._debounce_job is not None:
            try:
                self.root.after_cancel(self._debounce_job)
            except tk.TclError:
                pass
        self._debounce_job = self.root.after(1500, self._on_agent_reply_ready)

    def _on_agent_reply_ready(self) -> None:
        self._debounce_job = None
        if not self.auto_running or self.auto_state != "waiting":
            self._agent_buffer = {}
            return
        if self._batch_job is not None:
            # 上一批分段还没发完，客服就回复了：等发完再处理，避免两批消息交错
            self._debounce_job = self.root.after(500, self._on_agent_reply_ready)
            return
        texts = [self._agent_buffer[key] for key in sorted(self._agent_buffer) if self._agent_buffer[key]]
        self._agent_buffer = {}
        if not texts:
            return
        if self._reply_job is not None:
            try:
                self.root.after_cancel(self._reply_job)
            except tk.TclError:
                pass
            self._reply_job = None
        reply = "\n".join(texts).strip()
        self.auto_history.append(("客服", reply))
        self.auto_state = "generating"
        self._set_auto_status("自动对话：客服已回复，正在请求 Dify…")
        self._request_dify(opening=False)

    def _build_prompt(self, opening: bool) -> str:
        lines = [
            "你是一名普通用户，正在通过中通快递官网的在线客服窗口与客服沟通。",
            f"【本次目标】{self.auto_goal}",
            "【对话记录】",
        ]
        history = self.auto_history[-12:]
        if history:
            lines.extend(f"{role}：{text}" for role, text in history)
        else:
            lines.append("（暂无）")
        lines.append("【你的任务】")
        if opening:
            lines.append("对话刚开始，请生成第一条发给客服的消息，直接说明诉求，不要寒暄。")
        else:
            lines.append(
                "客服刚刚发来新消息，请结合目标与对话记录，生成下一条发给客服的消息。"
                "如果目标已经完成，或客服明确表示无法继续处理，只输出 [DONE]。"
            )
        lines.extend(
            [
                "【输出要求】",
                "只输出要发送的消息正文；不要加引号、不要解释、不要出现“我方：”等前缀。",
            ]
        )
        return "\n".join(lines)

    def _build_kefu_input(self, opening: bool) -> str:
        """拼出 Dify workflow 的 kefuinput 变量：目标 + 订单号 + 对话上下文。"""
        lines = [f"目标：{self.auto_goal}", f"订单号：{self.auto_order_no}"]
        latest = ""
        for role, text in reversed(self.auto_history):
            if role == "客服":
                latest = text
                break
        if opening or not latest:
            # 开场必须明确“客服还没说话”，否则模型会把自己当成客服、反过来索要我方信息
            lines.append(
                "客服最新消息：（客服尚未发言。请直接输出你要对客服说的第一句话，"
                "不要索要订单号、对话记录等任何信息，不要出现变量名）"
            )
        else:
            lines.append(f"客服最新消息：{latest}")
        lines.append("最近对话记录：")
        history = self.auto_history[-12:]
        if history:
            lines.extend(f"{role}：{text}" for role, text in history)
        else:
            lines.append("（暂无）")
        return "\n".join(lines)

    def _request_dify(self, opening: bool) -> None:
        try:
            snapshot = self._make_dify_snapshot()
        except DifyError as exc:
            self._stop_auto(str(exc))
            return
        snapshot["conversation_id"] = self.auto_conversation_id
        # workflow 应用只认 inputs，必须把上下文放进 kefuinput 才能驱动多轮
        snapshot["inputs"] = {**snapshot["inputs"], "kefuinput": self._build_kefu_input(opening)}
        prompt = self._build_prompt(opening)
        self.auto_state = "generating"
        self.auto_generation += 1
        generation = self.auto_generation
        self._set_auto_status("自动对话：正在请求 Dify 生成消息…")
        threading.Thread(
            target=self._dify_worker, args=(prompt, snapshot, generation), daemon=True
        ).start()

    def _dify_worker(self, prompt: str, snapshot: dict, generation: int) -> None:
        try:
            client = DifyClient(
                snapshot["base_url"],
                snapshot["api_key"],
                snapshot["app_type"],
                snapshot["user"],
            )
            conversation_id = snapshot["conversation_id"] if snapshot["use_conversation"] else None
            answer, new_conversation_id = client.generate(prompt, snapshot["inputs"], conversation_id)
            self.ui_tasks.put(lambda: self._on_dify_success(answer, new_conversation_id, generation))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.ui_tasks.put(lambda: self._on_dify_failure(message, generation))

    def _on_dify_success(self, answer: str, conversation_id: str | None, generation: int) -> None:
        if generation != self.auto_generation:
            self._log("已忽略上一轮 Dify 的过期结果", level="warn")
            return
        if not self.auto_running or self.auto_state != "generating":
            return
        raw = (answer or "").strip()
        if not raw:
            self._stop_auto("Dify 返回空内容")
            return
        if "[DONE]" in raw.upper():
            self._stop_auto("Dify 判断目标已完成")
            return
        message = self._clean_ai_message(raw)
        if not message:
            self._stop_auto("Dify 返回内容为空")
            return
        if "kefuinput" in message.lower():
            self._stop_auto(f"Dify 回复里出现了变量名，未发送：{message[:60]}")
            return
        if self.auto_turn == 0 and re.search(r"请(提供|输入|告知|发送)", message):
            # 开场时模型把自己当成客服、向我方索要信息——这种话不能发给客服
            self._log("Dify 开场回复是在索要信息，已改用固定开场：你好", level="warn")
            message = "你好"
        segments = split_message(message, MAX_REPLY_CHARS)
        if not segments:
            self._stop_auto("Dify 返回内容为空")
            return
        if conversation_id:
            self.auto_conversation_id = conversation_id
        self._log(f"Dify 生成：{message}")
        self._send_auto_batch(segments)

    def _on_dify_failure(self, message: str, generation: int) -> None:
        if generation != self.auto_generation:
            return
        if not self.auto_running:
            return
        self._stop_auto(f"Dify 调用失败：{message}")

    @staticmethod
    def _clean_ai_message(text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        for prefix in ("我方：", "我方:", "回复：", "回复:", "下一条消息：", "消息："):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if len(text) >= 2 and text[0] in "\"'“”‘’" and text[-1] in "\"'“”‘’":
            text = text[1:-1].strip()
        return text

    # ================================================================== 事件循环
    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                try:
                    task = self.ui_tasks.get_nowait()
                except queue.Empty:
                    break
                try:
                    task()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"界面回调异常：{exc}", level="error")
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"处理事件失败（{event.get('type')}）：{exc}", level="error")
        finally:
            # 只要没关闭就续约，否则单条事件异常会让界面永久假死
            if not self._closing:
                self.root.after(120, self._poll_events)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        message = event.get("message", "")

        if kind == "browser_ready":
            self.launched = True
            self._log(message)
            self._refresh_controls()
        elif kind == "browser_closed":
            self.launched = False
            self.chat_ready = False
            self._stop_auto("浏览器已关闭")
            self._log(message)
            self._refresh_controls()
        elif kind == "browser_status":
            self._log(message)
        elif kind == "log":
            self._log(message)
        elif kind == "need_login":
            self._log(message, level="warn")
            messagebox.showwarning("需要登录", message)
        elif kind == "chat_ready":
            self.chat_ready = True
            # 浏览器侧每次绑定客服页都会把消息列表清零（索引从 0 重新计数），
            # 界面必须同步清空，否则 auto_last_send_index 会大于新索引，客服回复全被过滤。
            self.messages = []
            self._render_messages()
            self._agent_buffer = {}
            self.auto_last_send_index = 0
            self._log(message)
            self._refresh_controls()
        elif kind == "chat_pending":
            self._log(message)
        elif kind == "chat_closed":
            self.chat_ready = False
            self._stop_auto("客服对话已断开")
            self._log(message, level="warn")
            self._refresh_controls()
        elif kind == "chat_reset":
            self.messages = []
            self._render_messages()
            # 与 chat_ready 对齐：浏览器侧消息索引已从头开始，界面基准也要归零
            self._agent_buffer = {}
            self.auto_last_send_index = 0
        elif kind == "chat_new":
            self._on_chat_new(event.get("items", []))
        elif kind == "chat_update":
            self._on_chat_update(event.get("item") or {})
        elif kind == "sent":
            self._log(f"已发送：{event.get('text', '')}")
        elif kind == "error":
            self._log(message, level="error")
            if self.auto_running and self.auto_state in ("waiting", "generating"):
                self._stop_auto(message)
        self._refresh_status()

    def _on_chat_new(self, items: list[dict]) -> None:
        if not items:
            return
        for item in items:
            item["ts"] = time.strftime("%H:%M")
            self.messages.append(item)
        self._render_messages()
        if not self.auto_running:
            return
        new_agent = [
            item
            for item in items
            if item.get("side") == "agent" and item.get("index", 0) >= self.auto_last_send_index
        ]
        if not new_agent:
            return
        if self.auto_state == "waiting":
            for item in new_agent:
                self._schedule_agent_reply(item.get("index", 0), item["text"])
        elif self.auto_state == "generating":
            for item in new_agent:
                if item["text"].strip():
                    self.auto_history.append(("客服", item["text"].strip()))

    def _on_chat_update(self, item: dict) -> None:
        index = item.get("index")
        for stored in self.messages:
            if stored.get("index") == index:
                stored["text"] = item.get("text", stored.get("text", ""))
                break
        self._render_messages()
        if not self.auto_running or self.auto_state != "waiting":
            return
        if item.get("side") == "agent" and item.get("index", 0) >= self.auto_last_send_index:
            self._schedule_agent_reply(item.get("index", 0), item.get("text", ""))

    def _render_messages(self) -> None:
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        for item in self.messages:
            side = item.get("side", "agent")
            prefix = "客服" if side == "agent" else "我方"
            text = item.get("text", "")
            self.chat_text.insert("end", f"[{item.get('ts', '')}] {prefix}：{text}\n", "agent" if side == "agent" else "user")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    # ================================================================== 状态 / 日志
    def _refresh_controls(self) -> None:
        self.btn_login.configure(state="normal" if self.launched else "disabled")
        self.btn_detect.configure(state="normal" if self.launched else "disabled")
        self.btn_close_browser.configure(state="normal" if self.launched else "disabled")
        self.btn_send.configure(state="normal" if self.chat_ready and not self.auto_running else "disabled")
        self.btn_auto_start.configure(state="normal" if self.chat_ready and not self.auto_running else "disabled")
        self.btn_auto_stop.configure(state="normal" if self.auto_running else "disabled")

    def _refresh_status(self) -> None:
        browser = "已启动" if self.launched else "未启动"
        chat = "已连接" if self.chat_ready else "未连接"
        auto = "进行中" if self.auto_running else "未开始"
        self.status_var.set(f"浏览器：{browser}    客服会话：{chat}    自动对话：{auto}")

    def _set_auto_status(self, text: str) -> None:
        self.auto_status_var.set(text)

    def _log(self, message: str, level: str = "info") -> None:
        if not message:
            return
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"
        self.log_text.configure(state="normal")
        # 显式带上 level 标签，避免接着上一行（可能是 warn/error）继承颜色
        self.log_text.insert("end", line, (level,))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    # ================================================================== 关闭
    def _on_close(self) -> None:
        self._closing = True
        self._save_config()
        self._stop_auto("程序关闭")
        try:
            self.worker.commands.put({"type": "shutdown"})
        except Exception:  # noqa: BLE001
            pass
        self.root.after(300, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
