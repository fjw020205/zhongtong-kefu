"""中通官网 / 在线客服的 Playwright 自动化封装。

- 只使用本机已安装的 Chrome / Edge（Chromium 内核），不下载 Playwright 自带浏览器。
- 浏览器操作全部在独立线程中执行，通过命令队列 / 事件队列与 tkinter 主线程通信。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

HOME_URL = "https://www.zto.com/"
CHAT_HOST_HINT = "kf.zto.com"
DEBUG_PORT = 9222
DEBUG_URL = f"http://127.0.0.1:{DEBUG_PORT}"

# 登录态快照：把 cookie / localStorage / sessionStorage 另存一份，
# 这样即使浏览器被关闭（会话级 cookie 会丢），下次启动也能恢复登录。
LOGIN_STATE_NAME = "login-state.json"
STATE_SAVE_INTERVAL = 20.0

# Chromium 退出码 21：本进程发现同一配置目录已有实例，把命令行移交后退出，
# 表现为“窗口打开后一闪就没了”。通常是上一次的浏览器实例还在占用配置目录。
RESULT_CODE_PROCESS_NOTIFIED = 21

# 按 origin 恢复 localStorage / sessionStorage（仅在当前值为空时写入，避免覆盖站点新数据）
RESTORE_STORAGE_TEMPLATE = """
(() => {
  const data = %s;
  const origin = location.origin;
  try {
    const ls = (data.local && data.local[origin]) || {};
    for (const k in ls) {
      if (localStorage.getItem(k) === null) localStorage.setItem(k, ls[k]);
    }
    const ss = (data.session && data.session[origin]) || {};
    for (const k in ss) {
      if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, ss[k]);
    }
  } catch (e) {}
})();
"""

# 读取 localStorage / sessionStorage（CDP 连接下 storage_state() 不含 origins，需自己取）
LOCAL_STORAGE_JS = """
() => {
  const out = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    out[k] = localStorage.getItem(k);
  }
  return out;
}
"""

SESSION_STORAGE_JS = """
() => {
  const out = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const k = sessionStorage.key(i);
    out[k] = sessionStorage.getItem(k);
  }
  return out;
}
"""

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


def _short_url(url: str) -> str:
    """去掉查询串，避免把登录 token / 手机号打印到界面日志。"""
    return (url or "").split("?", 1)[0]


class BrowserWorker(threading.Thread):
    """在独立线程中运行 Playwright，接收命令并向界面发送事件。"""

    def __init__(self, events: queue.Queue, profile_dir: str):
        super().__init__(daemon=True, name="zto-browser-worker")
        self.events = events
        self.profile_dir = profile_dir
        self.state_path = os.path.join(os.path.dirname(os.path.abspath(profile_dir)), LOGIN_STATE_NAME)
        self.commands: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()

        self._pw = None
        self._browser = None
        self._browser_proc: Optional[subprocess.Popen] = None
        self._browser_exe = ""
        self._launch_log = None
        self._ctx = None
        self._home_page = None
        self._chat_page = None
        self._chat_frame = None
        self._messages: list[dict] = []

        self._poll_interval = 0.8
        self._next_poll = 0.0
        self._next_state_save = 0.0
        self._state_saved_once = False
        self._last_state_error = ""
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
            # 只断开连接，不关闭浏览器，保证登录状态与窗口可以复用
            self._detach()

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
            self._detach()
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
        self._browser_exe = os.path.basename(path)

        # 1) 优先复用：已有一个带调试端口的浏览器在运行，直接连上，不新开窗口
        if self._connect_cdp(timeout=1500):
            self._emit("browser_status", message="已复用正在运行的浏览器窗口（不会新开窗口）")
        elif not self._spawn_and_connect(path):
            return
        # 复用 / 新启动都要发 browser_ready，否则界面的“登录”等按钮仍是禁用状态
        self._emit("browser_ready", message="浏览器已就绪，可以登录")

        self._load_login_state()
        self._next_state_save = time.time() + STATE_SAVE_INTERVAL

        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._home_page = pages[0] if pages else self._ctx.new_page()
        self._chat_page = None
        self._chat_frame = None
        self._messages = []
        self._cmd_goto_home()

    def _connect_cdp(self, timeout: int = 3000) -> bool:
        try:
            browser = self._pw.chromium.connect_over_cdp(DEBUG_URL, timeout=timeout)
        except Exception:  # noqa: BLE001
            return False
        contexts = list(browser.contexts)
        if not contexts:
            try:
                contexts = [browser.new_context()]
            except Exception:  # noqa: BLE001
                return False
        self._browser = browser
        self._ctx = contexts[0]
        try:
            self._ctx.on("page", lambda _page: self._emit("log", message="检测到新标签页"))
            browser.on("disconnected", lambda _b: self._emit("browser_closed", message="浏览器已断开"))
        except Exception:  # noqa: BLE001
            pass
        return True

    def _clear_singleton_locks(self) -> None:
        """清理上次被强制结束留下的配置文件锁，否则浏览器启动后会立刻退出。"""
        names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
        for _ in range(3):
            pending = False
            for name in names:
                path = os.path.join(self.profile_dir, name)
                if not os.path.exists(path):
                    continue
                try:
                    os.remove(path)
                except OSError:
                    # 文件正被别的浏览器进程占用，等一会儿再试
                    pending = True
            if not pending:
                return
            time.sleep(0.5)

    def _profile_browser_pids(self) -> list[int]:
        """返回正在使用本程序配置目录的浏览器进程 PID（不管它有没有调试端口）。"""
        if os.name != "nt":
            return []
        exe = self._browser_exe or os.path.basename(find_browser_path())
        if not exe:
            return []
        pattern = "*" + self.profile_dir + "*"
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"Get-CimInstance Win32_Process -Filter \"Name='{exe}'\" | "
            f"Where-Object {{ $_.CommandLine -like '{pattern}' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except Exception:  # noqa: BLE001
            return []
        return [int(line) for line in out.split() if line.strip().isdigit()]

    def _kill_pids(self, pids: list[int]) -> None:
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=20,
                )
            except Exception:  # noqa: BLE001
                continue

    def _spawn_and_connect(self, path: str) -> bool:
        """用固定配置目录启动本机浏览器，并连上调试端口。

        启动失败最常见的原因是：同一个配置目录被上一次遗留的浏览器实例占着
        （它没开调试端口，连不上；新进程又拿不到单例锁，退出码 21）。
        这里先结束这些遗留实例再重启；登录态会从本地快照恢复，不需要重新登录。
        """
        reason = ""
        for attempt in range(1, 4):
            # 端口已经在监听：说明有可复用的实例，先给足超时重连几次再考虑杀进程，
            # 避免把正在正常运行、已经登录的浏览器误杀。
            if self._probe_debug_endpoint():
                for _ in range(3):
                    if self._connect_cdp(timeout=5000):
                        self._emit("browser_status", message="已复用正在运行的浏览器窗口（不会新开窗口）")
                        return True
                    time.sleep(1)

            stale = self._profile_browser_pids()
            if stale:
                self._emit(
                    "log",
                    message=(
                        f"发现 {len(stale)} 个占用配置目录的遗留浏览器进程（无调试端口，无法复用），"
                        "正在重启为可复用模式；登录信息会从本地快照恢复"
                    ),
                )
                self._kill_pids(stale)
                time.sleep(1.5)

            self._clear_singleton_locks()
            self._start_browser_process(path)
            ok, reason, busy = self._wait_cdp(timeout=20)
            if ok:
                self._emit("browser_status", message=f"浏览器已启动：{os.path.basename(path)}")
                return True
            if busy:
                self._emit("log", message=f"{reason}，等待其释放配置目录后重试…")
                time.sleep(2)
            else:
                self._emit("log", message=f"{reason}，先结束这次拉起的浏览器再重试…")
                self._terminate_spawned_browser()
                time.sleep(2)
            if attempt < 3:
                self._emit("log", message=f"第 {attempt} 次启动浏览器未就绪，继续重试…")

        self._emit("error", message=f"启动浏览器失败：{reason}")
        return False

    def _start_browser_process(self, path: str) -> None:
        self._browser_exe = os.path.basename(path)
        args = [
            path,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={self.profile_dir}",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-blink-features=AutomationControlled",
        ]
        # 优先用 WMI 启动：进程完全脱离本程序（以及终端 / 沙箱的进程树），
        # 关掉本程序、关掉终端都不会带走浏览器（实测普通 Popen 启动的浏览器
        # 会被外部在二十秒左右终止，窗口“自己消失”）。
        if self._launch_detached(args):
            return

        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        log_path = os.path.join(os.path.dirname(self.profile_dir), "browser-launch.log")
        stdout = stderr = subprocess.DEVNULL
        try:
            self._launch_log = open(log_path, "ab", buffering=0)
            stdout = stderr = self._launch_log
        except OSError:
            self._launch_log = None
        self._browser_proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=flags,
        )

    def _launch_detached(self, args: list[str]) -> bool:
        """用 WMI 启动浏览器，让它不属于本进程的进程树。"""
        if os.name != "nt":
            return False
        cmdline = subprocess.list2cmdline(args).replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{ CommandLine = '{cmdline}' }};"
            "if ($r.ReturnValue -eq 0 -and $r.ProcessId -gt 0) { $r.ProcessId }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        if not out.isdigit():
            return False
        # WMI 启动没有进程句柄，后续按配置目录查进程
        self._browser_proc = None
        self._emit("log", message=f"已启动浏览器进程（PID {out}，独立于本程序运行）")
        return True

    def _terminate_spawned_browser(self, wait: bool = True) -> None:
        """结束占用本程序配置目录的浏览器进程（含 WMI 启动、没有句柄的实例）。"""
        pids = self._profile_browser_pids()
        proc = self._browser_proc
        self._browser_proc = None
        if proc is not None and proc.poll() is None:
            pids.append(proc.pid)
        if not pids:
            return
        self._kill_pids(sorted(set(pids)))
        if wait:
            time.sleep(1.5)

    def _wait_cdp(self, timeout: int = 20) -> tuple[bool, str, bool]:
        """等待调试端口就绪。

        返回 (是否连上, 失败原因, 配置目录是否被别的浏览器实例占用)。
        msedge.exe 有时只是启动器：把命令行交给已有实例后自己退出（退出码 0 / 21），
        所以子进程退出后还要再宽限几秒，确认端口仍不可用才判定失败。
        通过 WMI 启动时没有进程句柄，进程存活情况按配置目录查询。
        """
        deadline = time.time() + timeout
        port_up = False
        while time.time() < deadline and not self._stop_event.is_set():
            port_up = self._probe_debug_endpoint()
            if port_up and self._connect_cdp(timeout=3000):
                return True, "", False
            time.sleep(0.6)

        # 再宽限几秒：浏览器首次启动 / 清理旧配置目录时可能稍慢
        grace = time.time() + 5
        while time.time() < grace and not self._stop_event.is_set():
            port_up = self._probe_debug_endpoint()
            if port_up and self._connect_cdp(timeout=3000):
                return True, "", False
            time.sleep(0.6)

        proc = self._browser_proc
        code = proc.poll() if proc is not None else None
        alive = code is None and (proc is not None or bool(self._profile_browser_pids()))

        busy = code in (0, RESULT_CODE_PROCESS_NOTIFIED)
        if port_up:
            reason = f"调试端口 {DEBUG_PORT} 可访问但 Playwright 连接失败"
        elif busy:
            reason = f"浏览器退出码 {code}：配置目录正被另一个浏览器实例占用"
        elif code is not None:
            reason = f"浏览器进程已退出（退出码 {code}），调试端口 {DEBUG_PORT} 未监听"
        elif alive:
            reason = f"浏览器进程仍在运行但调试端口 {DEBUG_PORT} 未监听"
        else:
            reason = f"浏览器已退出，调试端口 {DEBUG_PORT} 未监听"
        return False, reason, busy

    def _probe_debug_endpoint(self) -> bool:
        try:
            with urllib.request.urlopen(f"{DEBUG_URL}/json/version", timeout=1.5) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _save_login_state(self) -> None:
        """把当前登录态（cookie / localStorage / sessionStorage）快照到本地文件。

        Chromium 的会话级 cookie（无过期时间）在浏览器关闭后会丢失，
        有了快照就可以在下次启动时把登录态补回去，避免反复登录。
        """
        ctx = self._ctx
        if ctx is None:
            return

        def fail(message: str) -> None:
            """保存失败必须可见：同一条错误只提示一次，避免每 20 秒刷屏。"""
            if message != self._last_state_error:
                self._last_state_error = message
                self._emit("log", message=message)

        # 只读 cookie 列表：不要用 ctx.storage_state()，它在本机 Edge 上会触发浏览器崩溃
        try:
            all_cookies = ctx.cookies()
        except Exception as exc:  # noqa: BLE001
            fail(f"保存登录状态失败（读取 cookie）：{exc}")
            return

        local_storage: dict[str, dict] = {}
        session_storage: dict[str, dict] = {}
        try:
            pages = [p for p in ctx.pages if not p.is_closed()]
        except Exception:  # noqa: BLE001
            pages = []
        for page in pages:
            try:
                origin = page.evaluate("() => location.origin")
                if "zto.com" not in origin:
                    continue
                local = page.evaluate(LOCAL_STORAGE_JS)
                if isinstance(local, dict) and local:
                    local_storage[origin] = local
                sess = page.evaluate(SESSION_STORAGE_JS)
                if isinstance(sess, dict) and sess:
                    session_storage[origin] = sess
            except Exception:  # noqa: BLE001
                continue

        cookies = [c for c in all_cookies if "zto.com" in (c.get("domain") or "")]
        if not cookies and not local_storage and not session_storage:
            fail("保存登录状态失败：当前没有读取到 zto.com 的 cookie，请确认已在浏览器中登录")
            return
        payload = {
            "saved_at": int(time.time()),
            "cookies": cookies,
            "local_storage": local_storage,
            "session_storage": session_storage,
        }
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception as exc:  # noqa: BLE001
            fail(f"写入登录状态失败（{self.state_path}）：{exc}")
            return

        self._last_state_error = ""
        if not self._state_saved_once:
            self._state_saved_once = True
            self._emit(
                "log",
                message=f"登录状态已保存到本地（{len(cookies)} 个 cookie），下次启动会自动恢复",
            )

    def _load_login_state(self) -> None:
        """启动时恢复上次快照里的登录态。"""
        ctx = self._ctx
        if ctx is None or not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return

        cookies = [c for c in data.get("cookies", []) if "zto.com" in (c.get("domain") or "")]
        if cookies:
            try:
                # 只补缺失的 cookie：浏览器里已有的是用户刚登录产生的新值，
                # 用旧快照覆盖会把刚登录的会话冲掉。
                existing = {
                    (c.get("name"), c.get("domain"), c.get("path"))
                    for c in ctx.cookies()
                    if "zto.com" in (c.get("domain") or "")
                }
                missing = [
                    c
                    for c in cookies
                    if (c.get("name"), c.get("domain"), c.get("path")) not in existing
                ]
                if missing:
                    ctx.add_cookies(missing)
                    self._emit("log", message=f"已恢复上次的登录状态（{len(missing)} 个 cookie）")
                elif existing:
                    self._emit("log", message="浏览器已处于登录状态，跳过 cookie 恢复")
            except Exception as exc:  # noqa: BLE001
                self._emit("log", message=f"恢复登录 cookie 失败：{exc}")

        local: dict[str, dict] = {
            k: v for k, v in (data.get("local_storage") or {}).items() if "zto.com" in k
        }
        if not local:
            # 兼容旧格式（storage_state 里的 origins）
            for origin in data.get("origins", []):
                key = origin.get("origin") or ""
                if "zto.com" not in key:
                    continue
                local[key] = {
                    item.get("name"): item.get("value") for item in origin.get("localStorage", [])
                }
        session = {
            k: v for k, v in (data.get("session_storage") or {}).items() if "zto.com" in k
        }
        if local or session:
            script = RESTORE_STORAGE_TEMPLATE % json.dumps(
                {"local": local, "session": session}, ensure_ascii=False
            )
            try:
                ctx.add_init_script(script=script)
            except Exception as exc:  # noqa: BLE001
                self._emit("log", message=f"恢复本地存储失败：{exc}")

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
        """用户确认已登录：已有客服会话就复用，没有才回首页点击“在线客服”。"""
        if not self._ctx:
            self._emit("error", message="请先启动浏览器")
            return
        try:
            # 用户此刻已登录，立刻存一份登录态，避免之后浏览器被关闭导致登录丢失
            self._next_state_save = time.time() + STATE_SAVE_INTERVAL
            self._save_login_state()
            page, frame = self._find_chat_frame()
            if frame is not None:
                # 复用已打开的客服标签页：再点一次“在线客服”会多开一个标签页，
                # 两个标签页的消息索引互相错位，客服回复就会被漏掉。
                self._chat_page, self._chat_frame = page, frame
                self._messages = []
                self._emit("chat_ready", message=f"已复用已打开的客服会话：{_short_url(page.url)}")
                self._poll_messages()
                return
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
                # 客服页 URL 带 authToken / 手机号，日志里只保留域名和路径
                self._emit("chat_ready", message=f"已进入在线客服：{_short_url(page.url)}")
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
            self._emit("chat_ready", message=f"已检测到客服页面：{_short_url(page.url)}")
            self._poll_messages()
        else:
            self._emit("chat_pending", message="未检测到客服输入框，请确认已打开 kf.zto.com 客服页面")

    def _frame_with_chat(self, page):
        try:
            frames = page.frames
        except PlaywrightError:
            return None
        for frame in frames:
            try:
                if frame.locator("#sendText").count():
                    return frame
            except Exception:  # noqa: BLE001
                continue
        return None

    def _find_chat_frame(self):
        if self._ctx is None:
            return None, None
        try:
            pages = [p for p in self._ctx.pages if not p.is_closed()]
        except PlaywrightError:
            return None, None
        # 先认当前正在用的客服标签页，避免在多个客服标签页之间来回切换
        if self._chat_page is not None and self._chat_page in pages:
            frame = self._frame_with_chat(self._chat_page)
            if frame is not None:
                return self._chat_page, frame
        # 否则取最新的客服标签页（ctx.pages 里越靠后越新）
        for page in reversed(pages):
            frame = self._frame_with_chat(page)
            if frame is not None:
                return page, frame
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
            self._detach()
            self._emit("browser_closed", message="浏览器窗口已关闭")
            return

        if now >= self._next_state_save:
            self._next_state_save = now + STATE_SAVE_INTERVAL
            self._save_login_state()

        if self._chat_frame is None:
            page, frame = self._find_chat_frame()
            if frame is not None:
                self._chat_page, self._chat_frame = page, frame
                self._messages = []
                self._chat_pending_hinted = False
                self._emit("chat_ready", message=f"已检测到客服页面：{_short_url(page.url)}")
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
    def _detach(self) -> None:
        """断开与浏览器的连接，但不关闭浏览器窗口（保留登录态，下次可复用）。"""
        self._save_login_state()
        self._browser = None
        self._ctx = None
        self._home_page = None
        self._chat_page = None
        self._chat_frame = None
        self._messages = []

    def _close_browser(self, silent: bool = False) -> None:
        browser = self._browser
        ctx = self._ctx if browser is None else None
        self._detach()
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        elif ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._terminate_spawned_browser()
        if not silent:
            self._emit("browser_closed", message="浏览器已关闭")
