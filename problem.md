# 问题记录与复盘（problem.md）

用途：每次出现问题后，在这里记录「现象 → 日志关键行 → 根因 → 修复 → 验证」，并记录打标签（tag）的节点。
长期约束（不可回退）：

1. 只使用本机 Chromium 内核浏览器（本机 Edge：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`），不下载 Playwright 自带浏览器。
2. 浏览器窗口必须复用，用户不能反复登录。
3. 关闭本程序不能关闭浏览器窗口。

---

## 一、问题索引

| 编号 | 首次出现 | 现象 | 根因 | 状态 | 对应标签 |
| --- | --- | --- | --- | --- | --- |
| P-01 | 11-08 14:xx | 重启程序后登录态丢失，需要重新登录 | 中通 `.zto.com` 的 `OFFICIAL_TOKEN` 是会话级 cookie，浏览器退出即丢；程序只依赖配置目录 | 已修复 | v0.2.0 |
| P-02 | 11-08 19:31 | 浏览器启动约 20 秒后自己消失（"浏览器已断开"） | 周期保存调用 `ctx.storage_state()`，在**本机 Edge 上会直接崩掉浏览器** | 已修复 | v0.2.0 |
| P-03 | 11-08 20:05 | 启动失败：`Failed to create a ProcessSingleton ... Lock file can not be created! Error code: 5`，浏览器退出码 21 | 同一配置目录已有实例占用；`Popen` 启动的进程挂在外部作业对象上，被回收 | 已修复 | v0.2.0 |
| P-04 | 11-08 20:46 | 复用已打开的窗口后，界面"已登录"按钮一直禁用 | 复用分支只发了 `browser_status`，没发 `browser_ready`，`main.py` 的 `self.launched` 一直是 False | 已修复 | v0.2.0 |
| P-05 | 11-08 20:44 | 启动超时误报"浏览器进程已退出" | 改用 WMI 启动后没有 `Popen` 句柄，`_wait_cdp` 无法判活 | 已修复 | v0.2.0 |
| P-06 | 11-08 20:44 | 有可能在浏览器只是"慢"的时候把已登录的正常实例杀掉 | `_spawn_and_connect` 一上来就清理旧进程 | 已修复 | v0.2.0 |
| P-07 | 11-08 20:3x | 运行报错 `name 'json' is not defined` | 回退代码时丢了 `import json` | 已修复 | v0.2.0 |
| P-08 | 11-08 20:53 | `login-state.json` 不再更新，界面与日志完全无提示 | `_save_login_state()` 在周期调用时（`reason=""`）所有失败都静默 return | 已修复 | v0.2.1 |
| P-09 | 11-08 20:50 | 测试脚本把"45 秒稳定"误判为"27.5 秒断开" | 测试脚本的 `break` 条件写在 `events.get` 成功分支后面，循环不按时间退出 | 已修复（测试脚本） | — |
| P-10 | 11-08 23:20 | Dify 云端接入：答复里带 `<think>` 思维链；workflow 无输入变量；单次 17~26 秒 | 推理模型把思维链写进 `output`；应用 Start 节点没定义变量；`reply_timeout=30` 偏紧 | 思维链/超时已修复，输入变量待 Dify 侧补 | v0.2.3 |
| P-11 | 11-09 08:45 | 客服已经回复，程序仍判定"等待客服回复超时" | 同时存在多个客服标签页；界面收到 `chat_ready` 时只把 `chat_ready` 置真、没清空消息列表，而浏览器侧已把索引归零 → `auto_last_send_index` 大于新消息索引，客服回复被全部过滤 | 已修复 | v0.2.6 |

---

## 二、详细记录

### P-01 重启后登录态丢失

- 现象：关掉程序/浏览器后再次启动，中通客服需要重新登录。
- 日志关键行：`[xx:xx] 已回到首页：https://www.zto.com/`，随后客服页要求登录。
- 原因分析：中通把登录凭证放在会话级 cookie（`OFFICIAL_TOKEN`，无 `expires`）。配置目录只保存磁盘上的 cookie，会话 cookie 在浏览器进程退出时必然丢失。
- 修复：新增 `login-state.json` 快照（cookie + localStorage + sessionStorage），周期 20 秒保存；启动时 `ctx.add_cookies()` + `ctx.add_init_script()` 恢复。
- 验证：重启后日志出现 `已恢复上次的登录状态（N 个 cookie）`，客服页不再要求登录。

### P-02 `storage_state()` 导致本机 Edge 崩溃

- 现象：浏览器启动约 20～24 秒后窗口自己消失，程序提示"浏览器已断开"。
- 关键证据：`%APPDATA%\ZtoKefuBot\browser-profile\Crashpad\reports\*.dmp` 有多条 20MB 崩溃转储（19:31、19:36、19:39、20:13、20:21、20:37、20:38），时间点与断开完全吻合。
- 原因分析：`ctx.storage_state()` 在该版本 Edge 上会触发渲染进程崩溃；且它每次都会做一次全量导出。
- 修复：改为只读数据 —— `ctx.cookies()` + 页内 `evaluate` 读取 localStorage / sessionStorage。
- 验证：修改后保持 45 秒、90 秒、3 分钟以上均不再断线；崩溃转储不再新增。
- 反思：以后遇到"定时/周期性"断线，先看 Crashpad 转储与时间点对齐，不要凭直觉怀疑业务代码。

### P-03 启动即失败：ProcessSingleton / 退出码 21

- 现象：点击"启动浏览器"后窗口一闪而过。
- 日志关键行：`Lock file can not be created! Error code: 5`、`浏览器进程退出码：21`。
- 原因分析：① 同一 `--user-data-dir` 已有实例，新进程把命令行移交后自己退出（退出码 21 = `RESULT_CODE_PROCESS_NOTIFIED`）；② `subprocess.Popen` 启动的浏览器挂在外部作业对象里，会被连带回收。
- 修复：① 启动前按命令行匹配该 profile 的进程并清理 `SingletonLock/Cookie/Socket`；② 识别退出码 0/21 为"移交成功"，不当作崩溃；③ 改用 WMI `Invoke-CimMethod Win32_Process Create` 启动，让浏览器脱离本程序进程树（`Popen` 仅作兜底）。
- 验证：程序退出后 `Get-Process msedge` 仍在，9222 端口仍可访问，页面仍是 zto/kf。
- 反思：**"程序退出"不该等于"浏览器退出"**，这是需求层面的硬约束，必须由进程模型保证，而不是靠 `finally` 不关。

### P-04 复用窗口后登录按钮禁用

- 现象：浏览器已在运行、界面也提示已复用，但"已登录 → 回首页并打开客服"按钮是灰的。
- 原因分析：按钮可用性由 `main.py` 的 `self.launched` 控制，只有收到 `browser_ready` 才置 True；复用分支只发了 `browser_status`。
- 修复：`_cmd_launch` 统一在"复用成功 / 新启动成功"之后发一次 `browser_ready`。
- 验证：界面日志出现"浏览器已就绪，可以登录"，按钮可用。

### P-05 / P-06 启动超时误报与误杀

- 现象：WMI 启动路径下，`_wait_cdp` 超时后报"浏览器进程已退出"；以及浏览器只是启动慢时可能被 `taskkill`。
- 原因分析：WMI 启动没有 `Popen` 句柄，`proc.poll()` 拿不到退出码；清理逻辑过于激进。
- 修复：① 用 `_profile_browser_pids()` 判断存活，并区分"端口可访问但连接失败 / 退出码 21 / 进程仍在但端口未监听"等原因；② 端口已在监听时先延长重连（3 次 × 5s，间隔 1s）再考虑清理；③ 增加 5 秒宽限期。
- 验证：复用场景日志稳定输出"已复用正在运行的浏览器窗口（不会新开窗口）"。

### P-08 周期保存静默失败（本轮）

- 现象：`%APPDATA%\ZtoKefuBot\login-state.json` 的 `LastWriteTime` 长时间不更新，但界面没有任何错误提示；同一时刻用独立脚本连接同一个浏览器却能正常写入。
- 日志关键行：无。**最严重的问题是"没有日志"**。
- 原因分析：`_save_login_state()` 只在 `reason` 非空时才 `emit` 错误；周期调用传的是 `reason=""`，于是读取 cookie 失败、没有匹配 cookie 提前 return、写文件失败，全部被吞掉；即使抛出意料之外的异常，也会被 `run()` 里的 `except Exception: pass` 吃掉。
- 修复：
  1. 失败必须可见：`fail()` 内部去重后 `emit("log", ...)`，不再依赖 `reason`；
  2. 首次成功保存也提示一次：`登录状态已保存到本地（N 个 cookie），下次启动会自动恢复`；
  3. 写文件从只捕获 `OSError` 放宽到 `Exception`，避免遗漏。
- 验证（GUI 级，走 `main.py` 真实路径，自动点击"启动浏览器"）：
  ```
  开始: 21:01:18 登录态文件: 20:53:50 / 3455B / 9 cookie
  [  8.0s] 8s: 20:53:50 / 3455B / 9 cookie
  [ 28.0s] 28s: 21:01:40 / 3455B / 9 cookie
  [ 48.0s] 48s: 21:02:00 / 3455B / 9 cookie
  [ 68.0s] 最终: 21:02:21 / 3455B / 9 cookie
  [21:01:20] 已复用正在运行的浏览器窗口（不会新开窗口）
  [21:01:20] 浏览器已就绪，可以登录
  [21:01:20] 已恢复上次的登录状态（9 个 cookie）
  [21:01:21] 已回到首页：https://www.zto.com/
  [21:01:40] 登录状态已保存到本地（9 个 cookie），下次启动会自动恢复
  ```
  文件每 20 秒更新一次；程序退出后浏览器 PID 17904 仍存活，`kf.zto.com` 页面仍在。
- 反思：**静默兜底 = 无法排查**。凡"后台周期任务"，失败至少要留下一条可读日志；`except: pass` 只能用于确实无关紧要的清理代码。

### P-09 测试脚本自身缺陷导致误判

- 现象：测试脚本输出"保持 45 秒稳定"，实际是浏览器在 27.5 秒断开。
- 原因分析：`break` 条件写在 `events.get()` 成功分支之后，拿到事件就继续循环，不按时间退出。
- 修复：时间判断放到 `while` 条件里，先算 `deadline`。
- 反思：验证脚本本身也要验证；不要把"测试通过"当成结论，要看它到底测了什么。

### P-10 Dify 云端 Workflow 接入

- 现象：
  1. `/workflows/run` 返回的 `outputs.output` 里整段是 `<think>…<!--dify-deepseek-reasoning-->…</think>` 推理过程；
  2. `GET /parameters` 的 `user_input_form` 是空数组，客服的话无法作为 `inputs` 传入；
  3. 单次调用实测 16.8s / 18.2s / 25.5s，而界面 `reply_timeout` 只有 30 秒。
- 日志关键行：
  ```
  GET /parameters      200，user_input_form = []
  POST /workflows/run  200 succeeded，elapsed 16.8s，outputs keys: ['output']
  POST /chat-messages  400 {"code":"not_chat_app"}   ← 说明确实是 Workflow 应用，不是 Chatflow
  ```
- 原因分析：
  1. 推理模型（DeepSeek）把思维链一起写进了输出变量，直接发出去会把推理过程暴露给对方；
  2. 应用本身是 Workflow，开始节点没有定义变量，接口层只能传 `inputs={}`，因此每轮拿到的都是固定流程的结果；
  3. 工作流节点多、模型推理慢，30 秒不够。
- 修复：
  1. `dify_client.py` 新增 `strip_reasoning()`：正则去掉成对的 `<think>` / `<thinking>`，未闭合的直接截断；`generate()` 的三个分支（completion / workflow / chat）返回前统一清洗；
  2. `config.json` 的 `reply_timeout` 由 30 调到 60；
  3. 输入变量属于 Dify 应用侧改动（在开始节点加文本变量，例如 `query`），程序侧等变量名确定后配置 `dify_inputs`。
- 验证：
  ```
  PASS '<think>推理过程</think>最终答复' -> '最终答复'
  PASS '<THINKING>a\nb</THINKING>\n\n最终答复' -> '最终答复'
  PASS '<think>只有推理没有闭合' -> ''
  PASS '正常答复' -> '正常答复'
  workflow 调用 25.5s，清理后长度 3086，含 <think>：False
  ```
- 反思：对接第三方模型时，**"能返回结果"不等于"结果可直接用"**。先看原始响应结构（`outputs` 的键名、是否含思维链、耗时），再决定映射规则；把清洗做在客户端，不依赖应用侧配置。

### P-11 客服已回复，程序仍报"等待客服回复超时"

- 现象：第 1 轮发「你好」后 60 秒判定超时；但浏览器里客服在同一分钟内已经回了「嘻嘻您好呀，我是小通~请问有什么可以帮到您？」。
- 日志关键行：
  ```
  [PROBE] t=08:44:30 条数=25 页面数=6 page=https://kf.zto.com/   ← 先绑定了旧客服标签页
  [info] 已检测到客服页面：https://kf.zto.com/
  [info] 第 1 轮发送（1/1）：你好
  [info] 已回到首页：https://www.zto.com/                        ← “已登录”按钮又回首页点了一次客服
  [info] 正在点击首页“在线客服”…                                  ← 于是多开了一个客服标签页（页面数 6→7）
  [info] 已进入在线客服：https://kf.zto.com/
  [info] 自动对话结束：等待客服回复超时（60 秒）
  ```
- 原因分析：
  1. `_cmd_login_done` 无条件回首页再点「在线客服」，已有客服会话时也会**新开一个标签页**，浏览器里同时存在多个 `kf.zto.com` 标签页；
  2. `_find_chat_frame` 取的是 `ctx.pages` 里第一个带 `#sendText` 的页面，可能命中旧标签页；
  3. 浏览器侧每次绑定客服页都会 `self._messages = []`（索引从 0 重新计数），但界面侧收到 `chat_ready` 时**没有清空 `self.messages`**；于是 `auto_last_send_index = len(self.messages)` 远大于新消息索引，`_on_chat_new` 的 `index >= auto_last_send_index` 过滤条件把客服回复全部丢弃。
- 修复：
  1. `browser_bot.py`：`_cmd_login_done` 发现已有客服页时直接复用（不再回首页、不再新开标签页）；
  2. `browser_bot.py`：`_find_chat_frame` 先认当前绑定的 `_chat_page`，否则取**最新**的客服标签页（`reversed(pages)`）；
  3. `main.py`：`chat_ready` 时同步清空 `messages` / `_agent_buffer` / `auto_last_send_index`，保证与浏览器侧索引基准一致。
- 验证（08:47 实测，3 轮）：
  ```
  [info] 已复用已打开的客服会话：https://kf.zto.com/
  [PROBE] 条数=39 → 40 → 41 → 42（客服回复被正常识别）
  第 2 轮发送（1/1）：人工客服
  Dify 生成：人工客服 → 第 3 轮发送（1/1）：人工客服
  客服：[agent] 会话被客服代表苑萱接起 / Hi，风里雨里，终于等到您，我是人工很高兴为您效劳~
  客服：[agent] 亲亲这边是人工
  ```
  人工客服已在第 3 轮接入，不再出现"客服已回复却超时"。
- 08:59 复测（Dify 提示词更新、`kefuinput` 最大长度放开后）：`你好` → `人工客服` → `人工客服`，第 3 轮「会话被客服代表离墨接起」；Dify 已生成第 4 轮「785533117799快递站拒收请处理」（19 字，未超限），因 `max_turns=3` 正常结束。全链路（发送 → 收客服消息 → 调 Dify → 拆分发送）无异常。
- 反思：**消息列表的索引基准必须由单一来源维护**。浏览器侧清零时，界面侧必须同步清零，否则"新消息索引 ≥ 本轮发送位置"这种相对判断会静默失效——它不报错，只表现为"永远等不到回复"。

---

## 三、打标签节点

| 标签 | 提交说明 | 覆盖内容 |
| --- | --- | --- |
| v0.1.0 | 初始版本 | 界面、浏览器启动、客服页面检测、Dify 调用、自动对话 |
| v0.2.0 | 复用本机浏览器窗口并保留登录态 | P-01 ~ P-07：WMI 脱离进程树、复用窗口、`browser_ready` 补发、`login-state.json` 保存/恢复、`storage_state()` 崩溃修复 |
| v0.2.1 | 登录态保存失败可见 | P-08：保存失败必须打日志、首次成功提示、异常捕获放宽 |
| v0.2.2 | Dify 云端接入前的基线 | 浏览器复用、登录态保存/恢复、关闭程序不关浏览器均已验证；Dify 云端连通性独立测试通过 |
| v0.2.3 | 剥离思维链并放宽回复超时 | P-10：`strip_reasoning()`、`reply_timeout` 30 → 60 |
| v0.2.4 | 接入 `kefuinput` 变量并加 20 字发送硬校验 | Dify Workflow 变量传参、超出 20 字不再直接发送 |
| v0.2.5 | 不再用旧快照覆盖已登录 cookie，并给客服页 URL 脱敏 | P-01 衍生问题：只补缺失 cookie；日志不再打印 `authToken` |
| v0.2.6 | 修复"客服已回复却超时"：多标签页与消息索引错位 | P-11：复用已打开的客服会话、优先最新客服标签页、`chat_ready` 同步清空消息索引；长回复按标点拆分成多条（每轮多条，单条 ≤ 20 字）；开场索要信息/变量名泄漏拦截 |

规范：每次修复并验证通过后 → 提交代码 → 打 tag（`v0.x.y`）→ 在本文件补一条记录，写清"现象/根因/修复/验证"。

---

## 四、新增记录模板

```markdown
### P-XX 一句话标题

- 现象：
- 日志关键行：
- 原因分析：
- 修复：
- 验证：
- 反思：
```
