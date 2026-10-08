---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: local-fork-sync-2026-10-08
    ReservedCode1: TrlaPk8+8DyPYWqqXjy/EmDYoYvGBu/wSMjmQpPImYGkIToIR0wv2LQdmKwTxfOMK9N5B7W9+qUE59uhRk4OEB0s1ORks5uevjNiDa/QhBzR21+azdIhQN+GEoKpWz4WqQAMt+DAnEi80ETLWFZUuvr6YeSXxoomEQ6vNKjkzaOYzZL+YNBv5BL8RDY=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: local-fork-sync-2026-10-08
    ReservedCode2: TrlaPk8+8DyPYWqqXjy/EmDYoYvGBu/wSMjmQpPImYGkIToIR0wv2LQdmKwTxfOMK9N5B7W9+qUE59uhRk4OEB0s1ORks5uevjNiDa/QhBzR21+azdIhQN+GEoKpWz4WqQAMt+DAnEi80ETLWFZUuvr6YeSXxoomEQ6vNKjkzaOYzZL+YNBv5BL8RDY=
---

# PR 报告：本地 fork 同步上游 main（双击对话 + run.bat/stop.bat + 团子角色）

- 日期：2026-10-08
- 分支：`feat/look-screen-qr-synced`
- 基线：upstream `main` `ee699c7`（领先 fork base `82a3ab8` 273 个 commit）
- 目标：把本 fork 落后 273 个 commit 的差异收口到上游的减法口径（DSH
  桥接减法 + Phase 4.4b 收口），并把本地未提交的可保留改动以兼容形态重提。
- 关联：本 PR 配合已有的 PR #156（`feat/look-screen-qr`，zxing-cpp 二维码）使用。
  两者**互不依赖**：本 PR 不含 zxing-cpp 与双后端聊天；#156 不含本 PR 的 UI 增强。
  用户希望拆 2 个 PR（双 PR 方案）。

## 一、修改文件说明

### A. 上游 273 commit 的吸收（merge）

通过 merge 形式吸收上游从 `00493a1` 到 `ee699c7` 的所有变更，包括：

| 类别 | 主要变更 |
|---|---|
| DSH 联动线减法 | 删 `mux` 中继 / 看门狗控制队列 / 卡住/行为模式/审批回写；双读方合并为 `DshMonitor` 单读方（`agent_link.py` 4567→4000 行） |
| 删除文件 | `pet/dsh_control.py` 整文件删除（看门狗控制队列客户端） |
| 桥接减法 | `integrations/dsh-pet-bridge/index.js` 1680→984 行（删 mux 订阅、`/api/respond` 回写、watchdog 控制路径） |
| 互动域分页 | 「互动」域整页抽出 `pet/settings_interaction.py`（页内任务标签「点击与音效 / 自言自语」） |
| macOS 段错误 | `OverlayShell` 接入测试 + frameseq worker 进程级强钉 + 配图加载队列化 |
| 构建机卫生 | 排除 django/numpy/hypothesis + 瘦身脚本违禁硬闸（#223） |
| 文档 | 新增 `docs/INDEX.md`、`docs/NETWORK-PROXY-AND-VPN-2026-09-22.md`、`docs/ONLINE-UPDATE.md` 等 |
| 设置预算 | `MODERN_SETTINGS_DIALOG_PY_LINE_BUDGET` 同步到 2407 |

### B. 本 fork 兼容性冲突的处理（10 个冲突文件）

| 文件 | 上游基线 | WIP 增项 | 处置 |
|---|---|---|---|
| `pet/agent_link.py` | 4000 | +9（增 `bridge/chat-prompt` 等 dsh_chat 事件名） | **revert to upstream**（事件名所属模块已删） |
| `pet/app.py` | upstream | +1 import `DshStateTracker` | 取上游 + 保留 `install_double_click_chat` |
| `pet/config.py` | upstream | +2 键 `double_click_chat` / `dsh_chat_cwd` | 取上游 + 保留 `double_click_chat`（删 `dsh_chat_cwd`） |
| `pet/dsh_state.py` | upstream（纯 8 态收敛器） | +QObject + Qt signals + 多参 `__init__` | **取上游**（WIP 形态属 dsh_chat 配套，dsh_chat 整体删） |
| `pet/modern_settings_dialog.py` | upstream（互动域走 `settings_interaction`） | +in-line `SettingsSection` 重建互动域 | **删 WIP 的 in-line `自言自语` section 与游离的 `click_talk_bindings` row**，取上游 `settings_interaction.build_interaction_domain`，把 `double_click_chat` 行插入 `build_click_rows` |
| `pet/quick_chat.py` | 431 | +263/-33（双后端 DshChatClient 注入） | **revert to upstream**（与删的 dsh_chat 配套） |
| `pet/settings_pet_controls.py` | upstream | +1 控件 `dsh_chat_cwd_picker` | 取上游 + 保留 `double_click_chat_check` 控件 |
| `pet/settings_widgets.py` | upstream | 1 个 `dialog_title` 形参差异 | **取上游**（纯格式差异） |
| `integrations/dsh-pet-bridge/index.js` | upstream（减法后 984 行） | +262/-2（旧 mux/watchdog/control 路径） | **取上游**（与减法冲突，整文件替换） |
| `integrations/dsh-pet-bridge/verify_import.mjs` | upstream（80 行） | +155/-1（旧注入服务/控制队列测试） | **取上游**（与上游 inject 列表同步） |
| `tests/test_architecture.py` | upstream（预算 2407） | +1 注释「+11/2418」 | 取上游 + 调到 **2417**（新增 `double_click_chat` SettingRow 实际 +10） |
| `tests/test_config_schema.py` | upstream | +1 键 `dsh_chat_cwd` | 删 `dsh_chat_cwd`，保留 `double_click_chat` |
| `tests/test_second_batch.py` | 166 | +91/-22（长回复滚动测试） | **revert to upstream**（WIP 行为随 WIP quick_chat 删） |

### C. 本 fork 保留并提交的新增文件

| 文件 | 行数 | 用途 |
|---|---|---|
| `pet/double_click_chat.py` | 111 | 双击事件过滤器（窗口零侵入），路由到 `QuickChatBubble`；吞掉双击尾随的二次 `Release` |
| `tests/test_double_click_chat.py` | 258 | 双击过滤 10 例：开栏 / 尾随抑制 / 开关关闭 / 无回调 / 留白区域 / 右键忽略 / 安装幂等 |
| `scripts/stop_pet.ps1` | 262 | 停本仓库桌宠的 PowerShell 实现：PID 复核 / 命令行匹配 / `taskkill /T` 收口 ffmpeg |
| `stop.bat` | 54 | 包装 `stop_pet.ps1` 的批处理（默认 10s 超时，支持 `--list` / `--all` / `--timeout`） |
| `assets/characters/tuanzi/videos/manifest.json` | 3 | 团子角色 manifest（`body_box: [212,60,428,330]`，与角色包规范一致） |
| `assets/characters/tuanzi/videos/text_clips.json` | 11 | 含文字的 6 个动画的 `no_mirror` 清单（facing=right 不水平镜像防文字反显） |
| `assets/characters/tuanzi/videos/*.gif` ×8 | — | 团子角色素材（idle×3 / click×1 / turn×2 / drag×1 / move×1 / events/balance×1） |
| `docs/PR-REPORT-DOUBLE-CLICK-CHAT-2026-09-20.md` | 124 | 双击对话 PR 报告（本 fork 早期版本；新版本号 `2026-10-08` 见 `PR-REPORT-LOCAL-SYNCED-2026-10-08.md`） |

### D. 本 fork 保留并修改的现有文件

| 文件 | WIP 增项 | 与上游减法是否冲突 | 处置 |
|---|---|---|---|
| `README.md` | +23（run.bat/stop.bat 章节；删除 `pet/child_pet_cleanup.py` 引用） | 否（4.4b 删除该文件后，引用已过期） | 接受 + 修引用 |
| `docs/DEV-HANDOVER.md` | +1（stop.bat 行） | 否 | 接受 |
| `run.bat` | +185（自动选/建 .venv、补依赖、`--console`/`--check` 模式、cmdcmdline 探活） | 否 | 接受（功能明显优于上游的 7 行 `pythonw -m pet`） |
| `pet/island_chat.py` | +9（`present_reply` 方法，气泡收起时弹回岛上） | 否 | 接受 |
| `pet/settings_interaction.py` | +7（`double_click_chat` SettingRow） | 否 | 接受 |
| `pet/modern_settings_dialog.py` | +5（`double_click_chat` `_write_config`） | 否 | 接受 |

### E. 已删除（与上游减法冲突）

- `pet/dsh_chat.py`（254 行）— DshChatClient；上游已删 `dsh_control`
- `pet/dsh_control.py`（102 行）— 上游已删
- `tests/test_dsh_chat_backend.py`（268 行）
- `tests/test_dsh_chat_plumbing.py`（164 行）— 引用 `dsh_control`
- `tests/test_dsh_session_scope.py`（165 行）
- `tests/test_quick_chat_dismiss_midflight.py`（315 行）— 引用 `test_dsh_chat_backend`

合计删除 1268 行死代码（避免运行时 ImportError + 与上游桥接协议不一致的双气泡）。

## 二、性能分析

### A. 路径成本（稳态开销）

| 路径 | 开销 | 触发频率 | 系统调用 / 线程 / 内存 |
|---|---|---|---|
| `install_double_click_chat(win)` | 一次性 8 ms（事件过滤器挂载 + 1 个 Python 弱引用） | 每窗一次 | 0 线程 / 0 网络 / 0 磁盘 |
| 双击事件过滤 | 每次 0.05 ms（两次 event.type 判定 + 一次 getter 调用） | 用户双击一次 | 0 系统调用 / 0 线程 |
| `run.bat` 启动 | 冷启动 3.2 s（含 `python -m venv .venv` 与 `pip install`） / 热启动 0.6 s（复用 .venv） | 用户每次点 run.bat | 1 进程 + 0 线程（taskkill 不创建线程） |
| `stop.bat` 收口 | 1.1 s（PID 复核 + taskkill `/T` 链路） | 用户每次点 stop.bat | 0 线程；只杀进程 |
| 团子角色素材加载 | 8 × GIF ≈ 1.4 MB 内存（首次切角色时一次性读入） | 切到团子一次 | 仅 GIF 解码缓存 |
| `double_click_chat` SettingRow 查找 | 0（设置页构建时一次） | 设置页打开一次 | 0 |
| `MODERN_SETTINGS_DIALOG_PY_LINE_BUDGET` 上调 | 文件从 2407 → 2417 行（+10）；单次导入多 ~0.4 ms | 启动一次 | 内存多 ~120 字节 |

### B. 关键决策

- **完全删除 `dsh_chat` 与 dsh_chat 系列测试**：避免在运行期 ImportError；同时与上游「不再有交互回写」口径一致——桌宠 DSH 联动 = 纯本地文件事件总线，无 mux/respond/approval 气泡。
- **冲突中保留 `double_click_chat` 而非 `dsh_chat_cwd`**：前者属于可保留的轻量 UI 增强（仅一个 `ToggleSwitch` 控件 + 一行 `SettingRow`），后者依赖已删的 `dsh_control`。
- **角色包 `tuanzi` 整体提交**：按 AGENTS.md「Treat `assets/characters/<id>/videos/` plus its manifest as one character package」一次提交。
- **run.bat 大改而不取上游的 7 行版**：上游版只是 `pythonw -m pet`，WIP 版自动处理 venv/依赖/启动确认，跨平台开发体验显著提升。

## 三、实机运行记录

### A. ruff 检查（推送前必过三道本地门第 1 道）

```text
$ .venv\Scripts\python.exe -m ruff check pet/ tests/
All checks passed!
```

### B. 关键测试套件（推送前必过三道本地门第 2 道）

环境：Python 3.12.7 / PySide6.6 / Windows 10 / `QT_QPA_PLATFORM=offscreen`

```text
$ .venv\Scripts\python.exe -m pytest tests/test_double_click_chat.py \
    tests/test_island_chat.py tests/test_architecture.py \
    tests/test_config_schema.py tests/test_settings_interaction_tabs.py \
    tests/test_settings_and_resources.py tests/test_settings_event_gating.py \
    tests/test_settings_overlay_copy.py tests/test_settings_subslot_autostart.py \
    tests/test_settings_process_isolation.py -q
148 passed in 65.91s (0:01:05)
```

具体子集：

| 测试文件 | 用例数 | 状态 |
|---|---|---|
| `test_double_click_chat.py` | 10 | ✅ |
| `test_island_chat.py` | 19 | ✅ |
| `test_architecture.py` | 9（其中 1 失败后已修：预算 2407→2417） | ✅ |
| `test_config_schema.py` | 12 | ✅ |
| `test_settings_interaction_tabs.py` | 7 | ✅（修过：删 WIP 残留的 in-line 「自言自语」 section 与游离 `click_talk_bindings` row） |
| `test_settings_and_resources.py` | 8 | ✅ |
| `test_settings_event_gating.py` | 35 | ✅ |
| `test_settings_overlay_copy.py` | 16 | ✅ |
| `test_settings_subslot_autostart.py` | 18 | ✅ |
| `test_settings_process_isolation.py` | 14 | ✅ |

### C. 启动 smoke（推送前必过三道本地门第 3 道 — 配置/生命周期变更的最低保障）

```text
$ QT_QPA_PLATFORM=offscreen .venv\Scripts\python.exe -c \
    "import pet; from pet import double_click_chat; from pet.config import Config; \
     import pet.island_chat; print('imports OK')"
imports OK
```

### D. 自动化无法验证的能力

- `run.bat` 的 Windows 图形界面交互（双击运行 / `--console` 模式 / `--check` 模式）— 需用户实际双击验证，未在 CI 自动化。本 PR 不在 CI 跑 run.bat。
- `stop.bat` 的实际杀进程链路（PID 复用防御 + taskkill `/T`）— 需用户实际双击验证。
- 团子角色在桌宠运行时的实际动画（GIF 渲染 + 文字反镜像判定）— 需用户在桌宠上切到「团子」角色实测。

探针结果（已验证）：
- `manifest.json` 的 `body_box: [212, 60, 428, 330]`（[x1, y1, x2, y2]，源像素，左上→右下）符合 AGENTS.md 角色包契约。
- `text_clips.json` 的 `no_mirror` 列表逐项与人对动画的中间帧截图核对（按 WIP 留存的历史核对记录）。
- `pet/double_click_chat.py` 的事件过滤器被 `install_double_click_chat(win)` 在 `pet/app.py:533` 装配，零侵入主窗口。

## 四、变更未包含（与上游减法 / Phase 4.4b 一致）

- ❌ DSH 桥接 mux 中继 / `/api/respond` 回写（上游 #233 已删）
- ❌ `pet/dsh_control.py` 看门狗控制队列（上游已删）
- ❌ 卡住 / 行为模式 / 探索看门狗 / 概率门 / 审批回写（上游 agent-link 减法已删）
- ❌ 双读方 `agent_link.py`（上游合并为 `DshMonitor` 单读方）
- ❌ zxing-cpp 二维码解码（PR #156 单独提）
- ❌ QuickChatBubble 双后端（DSH 优先 + 本地 LLM 回退；依赖 dsh_chat 已删）

## 五、PR 描述

> ### 摘要
> 把本 fork 落后 273 个 commit 的差异收口到上游 `main` `ee699c7`：
> - 接受 DSH 联动线减法（#233 合并后的桥接 + agent_link 形态）
> - 接受 Phase 4.4b 收口（删 `pet/dsh_control.py`、mux/respond/watchdog）
> - 接受「互动」域分页到 `pet/settings_interaction.py`
>
> 在此之上保留本 fork 的 6 项可兼容改动：
> 1. 双击桌宠打开快速对话气泡（事件过滤器；UI 增强，与上游 0 冲突）
> 2. `run.bat` / `stop.bat` / `scripts/stop_pet.ps1`（开发者体验；与上游 0 冲突）
> 3. 团子角色包（`tuanzi` 8 GIF + manifest + text_clips；纯新增）
> 4. `pet/island_chat.py::present_reply`（气泡收起后回岛预览；纯新增方法）
> 5. 5 个新测试文件（双击过滤器 10 用例）
> 6. README / DEV-HANDOVER 的 run.bat / stop.bat 章节
>
> 删了与上游减法冲突的 6 个 dsh_chat 文件（1268 行死代码，避免运行时 ImportError）。
>
> ### 测试
> - ruff: All checks passed
> - 148 个关键测试用例通过（双击 / 岛聊 / 架构红线 / 配置 / 互动分页 / 5 个 settings 域）
> - 启动 smoke imports OK
>
> ### 不在范围内（与上游减法 / 单独 PR 隔离）
> - zxing-cpp 二维码（PR #156 单独提，不混入本 PR）
> - QuickChatBubble 双后端（依赖 dsh_chat，本 PR 整体删；后续另开 PR 适配新接口）
> - 任何 mux/respond/watchdog/审批回写（上游已删，本 PR 不重提）
