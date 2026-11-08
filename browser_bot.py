"""中通官网 / 在线客服的 Playwright 自动化封装。

- 只使用本机已安装的 Chrome / Edge（Chromium 内核），不下载 Playwright 自带浏览器。
- 浏览器操作全部在独立线程中执行，通过命令队列 / 事件队列与 tkinter 主线程通信。
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

HOME_URL = "https://www.zto.com/"
CHAT_HOST_HINT = "kf.zto.com"

# 读取 #messageCont 中的消息；根据气泡在容器中的水平位置判断是我方还是客服。
MESSAGE_JS = r"""
() => {
  const cont = document.querySelector('#messageCont');
  if (!cont) return [];
  const cr = cont.getBoundingClientRect();
  const midX = cr.left + cr.width / 2;
  const out = [];
  const boxes = Array.from(cont.querySelectorAll('.message-box'));
  const nodes = boxes.length ? boxes : Array.from(cont.children);
  for (const box of nodes) {
    if (box.classList && box.classList.contains('time')) continue;
    if (box.querySelector && box.querySelector('.message-guess')) continue;
    const msgEl = (box.querySelector && box.querySelector('.message')) || box;
    const text = (msgEl.innerText || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t]+\n/g, '\n')
      .trim();
    if (!text) continue;
    const r = msgEl.getBoundingClientRect();
    const side = (r.left + r.width / 2) > (midX + 30) ? 'user' : 'agent';
    out.push({ side: side, text: text.slice(0, 2000) });
  }
  return out;
}
"""


def _registry_browser_paths() -> list[str]:
    try:
        import winreg
    except ImportError:
        return []
    keys = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
    ]
    found = []
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value:
                    found.append(value.strip('"'))
        except OSError:
            continue
    return found


def find_browser_path(preferred: str = "") -> str:
    """返回本机 Chromium 内核浏览器路径，优先 Chrome，其次 Edge。"""
    candidates: list[str] = []
    for item in (preferred, os.environ.get("ZTO_BROWSER_PATH", "")):
        if item:
            candidates.append(item)

    roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    rel_paths = [
        r"Google\Chrome\Application\chrome.exe",
        r"Microsoft\Edge\Application\msedge.exe",
        r"Chromium\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for root in roots:
        if not root:
            continue
        for rel in rel_paths:
            candidates.append(os.path.join(root, rel))

    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe", "brave", "brave.exe"):
        path = shutil.which(name)
        if path:
            candidates.append(path)

    candidates.extend(_registry_browser_paths())

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


class BrowserWorker(threading.Thread):
    """在独立线程中运行 Playwright，接收命令并向界面发送事件。"""

    def __init__(self, events: queue.Queue, profile_dir: str):
        super().__init__(daemon=True, name="zto-browser-worker")
        self.events = events
        self.profile_dir = profile_dir
        self.commands: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()

        self._pw = None
        self._ctx = None
        self._home_page = None
        self._chat_page = None
        self._chat_frame = None
        self._messages: list[dict] = []

        self._poll_interval = 0.8
        self._next_poll = 0.0
        self._chat_pending_hinted = False
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------ 线程主体
    def run(self) -> None:
        try:
            with sync_playwright() as pw:
                self._pw = pw
                while not self._stop_event.is_set():
                    try:
                        cmd = self.commands.get(timeout=0.2)
                    except queue.Empty:
                        cmd = None
                    if cmd:
                        try:
                            self._handle_command(cmd)
                        except PlaywrightError as exc:
                            self._emit("error", message=f"浏览器操作失败：{exc}")
                        except Exception as exc:  # noqa: BLE001
                            self._emit("error", message=f"操作异常：{exc}")
                    try:
                        self._tick()
                    except PlaywrightError:
                        self._drop_chat()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            self._emit("error", message=f"Playwright 启动失败：{exc}")
        finally:
            self._close_browser(silent=True)

    def _emit(self, kind: str, **data) -> None:
        data["type"] = kind
        self.events.put(data)

    # ------------------------------------------------------------------ 命令分发
    def _handle_command(self, cmd: dict) -> None:
        kind = cmd.get("type")
        if kind == "launch":
            self._cmd_launch(cmd)
        elif kind == "goto_home":
            self._cmd_goto_home()
        elif kind == "login_done":
            self._cmd_login_done()
        elif kind == "open_service":
            self._cmd_open_service()
        elif kind == "detect_chat":
            self._detect_chat_manual()
        elif kind == "send":
            self._cmd_send(cmd.get("text", ""))
        elif kind == "close_browser":
            self._close_browser()
        elif kind == "shutdown":
            self._close_browser(silent=True)
            self._stop_event.set()

    # ------------------------------------------------------------------ 启动 / 页面
    def _cmd_launch(self, cmd: dict) -> None:
        if self._ctx is not None:
            self._emit("browser_ready", message="浏览器已在运行")
            return
        path = cmd.get("browser_path") or find_browser_path()
        if not path or not os.path.isfile(path):
            self._emit("error", message="未找到本机 Chrome / Edge，请在界面中手动选择浏览器程序")
            return
        os.makedirs(self.profile_dir, exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            executable_path=path,
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        self._ctx.on("page", lambda _page: self._emit("log", message="检测到新标签页"))
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._home_page = pages[0] if pages else self._ctx.new_page()
        self._chat_page = None
        self._chat_frame = None
        self._messages = []
        self._emit("browser_ready", message=f"浏览器已启动：{os.path.basename(path)}")
        self._cmd_goto_home()

    def _ensure_home_page(self):
        if self._home_page is not None and not self._home_page.is_closed():
            return self._home_page
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        for page in pages:
            if CHAT_HOST_HINT not in (page.url or ""):
                self._home_page = page
                return page
        self._home_page = pages[0] if pages else self._ctx.new_page()
        return self._home_page

    def _cmd_goto_home(self) -> None:
        if not self._ctx:
            self._emit("error", message="请先启动浏览器")
            return
        page = self._ensure_home_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)
        self._emit("browser_status", message=f"已回到首页：{page.url}")

    def _cmd_login_done(self) -> None:
        """用户确认已登录：先回首页，再自动点击“在线客服”。"""
        if not self._ctx:
            self._emit("error", message="请先启动浏览器")
            return
        try:
            self._cmd_goto_home()
            self._cmd_open_service()
        except PlaywrightError as exc:
            self._emit("error", message=f"进入客服失败：{exc}")

    def _cmd_open_service(self) -> None:
        page = self._ensure_home_page()
        if not (page.url or "").startswith(HOME_URL.rstrip("/")):
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#onlineServiceIcon", timeout=25000)
        page.wait_for_timeout(800)

        self._emit("browser_status", message="正在点击首页“在线客服”…")
        clicked = False
        button = page.locator(".bar-btn.online-service").filter(has_text="在线客服")
        if button.count():
            try:
                button.first.click(timeout=5000)
                clicked = True
            except PlaywrightError:
                clicked = False
        if not clicked:
            try:
                page.locator("#onlineServiceIcon").first.click(timeout=5000)
                clicked = True
            except PlaywrightError:
                clicked = False
        if not clicked:
            clicked = bool(
                page.evaluate(
                    "() => { const el = document.querySelector('#onlineServiceIcon');"
                    " if (el) { el.click(); return true; } return false; }"
                )
            )
        if not clicked:
            raise RuntimeError("未找到首页的“在线客服”按钮")
        self._wait_for_chat(timeout=30)

    def _wait_for_chat(self, timeout: int) -> bool:
        deadline = time.time() + timeout
        login_hinted = False
        while time.time() < deadline and not self._stop_event.is_set():
            page, frame = self._find_chat_frame()
            if frame is not None:
                self._chat_page, self._chat_frame = page, frame
                self._messages = []
                self._emit("chat_ready", message=f"已进入在线客服：{page.url}")
                self._poll_messages()
                return True
            if not login_hinted and self._login_modal_visible():
                login_hinted = True
                self._emit(
                    "need_login",
                    message="检测到登录弹窗：请先在浏览器中完成登录，然后点击“已登录”按钮",
                )
            time.sleep(0.6)

        self._emit("chat_pending", message="暂未检测到客服对话框；如已在浏览器中进入，请点击“检测客服页面”")
        return False

    def _login_modal_visible(self) -> bool:
        try:
            page = self._ensure_home_page()
            for text in ("扫码登录", "账号登录", "微信扫码"):
                loc = page.get_by_text(text, exact=False)
                if loc.count() and loc.first.is_visible():
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _detect_chat_manual(self) -> None:
        if not self._ctx:
            self._emit("error", message="请先启动浏览器")
            return
        page, frame = self._find_chat_frame()
        if frame is not None:
            self._chat_page, self._chat_frame = page, frame
            self._messages = []
            self._emit("chat_ready", message=f"已检测到客服页面：{page.url}")
            self._poll_messages()
        else:
            self._emit("chat_pending", message="未检测到客服输入框，请确认已打开 kf.zto.com 客服页面")

    def _find_chat_frame(self):
        if self._ctx is None:
            return None, None
        for page in list(self._ctx.pages):
            if page.is_closed():
                continue
            try:
                frames = page.frames
            except PlaywrightError:
                continue
            for frame in frames:
                try:
                    if frame.locator("#sendText").count():
                        return page, frame
                except Exception:  # noqa: BLE001
                    continue
        return None, None

    # ------------------------------------------------------------------ 发送
    def _cmd_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._emit("error", message="发送内容为空")
            return
        if not self._ctx:
            self._emit("error", message="浏览器未启动")
            return

        with self._send_lock:
            page, frame = self._find_chat_frame()
            if frame is None:
                self._emit("error", message="未找到客服输入框，请先打开在线客服")
                return
            self._chat_page, self._chat_frame = page, frame
            frame.fill("#sendText", text)
            frame.wait_for_timeout(300)
            try:
                frame.locator("#sendBtn").click(timeout=4000)
            except PlaywrightError:
                frame.press("#sendText", "Enter")
            frame.wait_for_timeout(400)

            try:
                remaining = (frame.input_value("#sendText") or "").strip()
            except PlaywrightError:
                remaining = ""
            if remaining == text:
                frame.press("#sendText", "Enter")
                frame.wait_for_timeout(300)
            self._emit("sent", text=text)

    # ------------------------------------------------------------------ 轮询消息
    def _tick(self) -> None:
        if self._ctx is None:
            return
        now = time.time()
        if now < self._next_poll:
            return
        self._next_poll = now + self._poll_interval

        try:
            pages = self._ctx.pages
        except PlaywrightError:
            pages = []
        if not pages:
            self._close_browser(silent=True)
            self._emit("browser_closed", message="浏览器窗口已关闭")
            return

        if self._chat_frame is None:
            page, frame = self._find_chat_frame()
            if frame is not None:
                self._chat_page, self._chat_frame = page, frame
                self._messages = []
                self._chat_pending_hinted = False
                self._emit("chat_ready", message=f"已检测到客服页面：{page.url}")
                self._poll_messages()
            elif not self._chat_pending_hinted:
                self._chat_pending_hinted = True
                self._emit("chat_pending", message="等待进入在线客服页面…")
            return

        try:
            if self._chat_frame.is_detached():
                raise PlaywrightError("frame detached")
            self._poll_messages()
        except PlaywrightError:
            self._drop_chat()

    def _poll_messages(self) -> None:
        if self._chat_frame is None:
            return
        data = self._chat_frame.evaluate(MESSAGE_JS)
        if not isinstance(data, list):
            return

        if len(data) < len(self._messages):
            self._messages = []
            self._emit("chat_reset")

        new_items = []
        for index in range(len(self._messages), len(data)):
            item = {
                "index": index,
                "side": data[index].get("side", "agent"),
                "text": (data[index].get("text") or "").strip(),
            }
            if not item["text"]:
                continue
            self._messages.append(item)
            new_items.append(item)
        if new_items:
            self._emit("chat_new", items=new_items)
            return

        if data and self._messages:
            last = data[-1]
            last_text = (last.get("text") or "").strip()
            if last_text and last_text != self._messages[-1]["text"]:
                self._messages[-1]["text"] = last_text
                self._emit(
                    "chat_update",
                    item={
                        "index": self._messages[-1]["index"],
                        "side": last.get("side", "agent"),
                        "text": last_text,
                    },
                )

    def _drop_chat(self) -> None:
        had_chat = self._chat_frame is not None
        self._chat_page = None
        self._chat_frame = None
        self._messages = []
        if had_chat:
            self._emit("chat_closed", message="客服对话框已断开")
            self._emit("chat_reset")

    # ------------------------------------------------------------------ 关闭
    def _close_browser(self, silent: bool = False) -> None:
        ctx = self._ctx
        self._ctx = None
        self._home_page = None
        self._chat_page = None
        self._chat_frame = None
        self._messages = []
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if not silent:
            self._emit("browser_closed", message="浏览器已关闭")
