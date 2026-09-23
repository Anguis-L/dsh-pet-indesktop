# -*- coding: utf-8 -*-
"""Phase 4.1a 产品壳：overlay 拓扑（单合成窗）的 AppShell 挂载层。

设计稿：.scratch/single-overlay-window/PHASE4_DESIGN.md（T1/T4/T5 + §5 4.1a 行
+ v1.1 §10 屏热插拔/会话事件升级项）。本模块只做骨架与生命周期：

- 主屏 OverlayWindow + 主 sprite（MovieLibrary 经主 PetInstance._create_library
  创建——per-pet 库是 T3 定论，禁止跨 sprite 共享 clip 对象）；
- 进程级一组三控制器（BehaviorController/SpriteCollisionWorld/
  ThrowPhysicsController），挂在**统一 tick 驱动器**（tick_driver.TickDriver，
  M-2/T2）上：驱动器持有控制器与 tick 时钟，调
  ``tick_sim(全部 overlays 的 sprites, dt)``，tick 顺序协议语义不变
  （行为 → 碰撞 → 抛掷物理）；overlay 只负责 advance+paint/脏矩形。
  接线口径同 .scratch/single-overlay-window/run_overlay_demo.py；
- 屏事件：screenAdded/screenRemoved/primaryScreenChanged/geometryChanged →
  overlay 重建或几何同步，sprite 位置按 rx/ry（中心相对可用区比例）语义
  迁移；主屏 DPR 变化重喂 sprite.set_dpr；拖拽中拔屏先收尾拖拽再迁移；
- 会话结束（Windows 关机/注销）：复用 session_watcher 的闸门与信号链，
  收口本路径 MovieLibrary 的全部 clip（issue #111 等价链，D10 落点）。

T5 切换策略：PET_RENDER_TOPOLOGY=overlay 环境变量是开发期一次性分流 flag，
读取收口在本模块 is_overlay_topology()，不进 Config/设置页/schema。
4.1a 明确不做：多 sprite（4.2）、位置持久化（4.2a，本刀恒走默认右下角）、
岛/气泡/聊天/菜单全量 parity（4.1b/4.1c）、捕获模式切换（T4 后续）。

4.2b（本刀后段）：多 sprite 生命周期按 D5/D6 落地——spawn 分配 slot 身份并写
活跃宠清单（``overlay-active-pets.json``，运行时状态文件）；退出即出清单
（slot 配置保留）；重启严格按清单复活（含各自 rx/ry/facing/scale）；主宠退出
按 app.py P1-3 语义提升列表首只子宠为主（接管主身份/持久化身份）。清单、无锁
身份分配、每身份几何读写的纯逻辑在 ``overlay_spawn_state``（零 Qt，便于单测）。

4.2c 后段（本刀）：托盘聚合（单托盘 + 逐只子菜单，对齐 app.py:3267-3301 的
单托盘多窗语义）、D12「退出子肥鱼」指令通道消费（独立设置进程写指令文件 →
本壳经 config 目录 watcher + 轮询消费，纯逻辑在 ``overlay_settings_command``）、
D13 逐 sprite 设置路由（菜单按被点 sprite 的 config 身份传 ``--instance``）。

4.3 后半（本刀）：把本壳做成进程级共享子系统（agent_link / proactive /
全屏 watcher）的**呈现扇出目标**——overlay 拓扑下 ``instances[].win`` 恒为
None，扇出集合为空等于联动/自说自话静默缺失（D0 的另一半，见
``multi_window_shared.presentation_targets``）。本类因此补齐 PetWindow 的
呈现等价面：

- 气泡：``show_bubble`` / ``hold_bubble`` / ``hide_bubble`` 落主 sprite 头顶
  （``SpriteBubbleFollower.show``）；提醒队列 ``show_alert`` / ``resolve_alert``
  / ``clear_alerts`` 直接复用 ``window_alerts`` 的 host 形函数（同一份队列
  语义，不重复造）；
- 聚合状态：``isVisible`` / ``_dragging`` / ``_physics_mode`` /
  ``_click_effect_phase`` / ``mouse_through`` / ``_bubble_busy_until`` /
  ``_bubble_suppressed``（proactive G1 守卫与联动节流门读的就是这些）；
- 联动动作：``request_link_anim`` / ``request_link_idle`` / ``switch_clip`` /
  ``cats`` / ``idles`` 映射到 ``BehaviorController``；
- 显隐：``set_pet_visible`` 同步 pause/resume proactive 与 agent_link
  （``window.py:1214-1250`` 语义；共享实例的 pause/resume 是 no-op，见
  multi_window_shared 的说明——G1 逐 tick 读可见性）；
- 快速对话：气泡可点 → ``open_quick_chat``，``QuickChatBubble`` 锚定被点
  sprite（``_SpriteChatAnchor`` 提供 pet 形的 ``visible_content_rect`` /
  ``on_open_chat``），回车发送走既有 ChatService/SessionStore 链路；
  无聊天打包变体（``pet.chat`` 被排除）按 app.py 既有 ImportError 守卫
  静默降级为不可点。
"""

from __future__ import annotations

import logging
import time
from collections import deque

from PySide6.QtCore import QObject, QPointF, QRect, QTimer, Qt
from PySide6.QtWidgets import QMenu, QStyle, QSystemTrayIcon

from . import catalog
from . import overlay_settings_command
from . import overlay_spawn_state
from . import slot_manager
from . import window_alerts
from .overlay_peripherals import FullscreenCursorWatcher
from .overlay_window import OverlayWindow
from .pet_sprite import INTERACTION_DRAG, INTERACTION_THROWN, PetSprite
from .session_watcher import install_session_watcher
from .sprite_behavior import (
    STATE_ACTS,
    STATE_CLICK,
    STATE_MOVE,
    BehaviorController,
)
from .sprite_bubble import SpriteBubbleFollower, sprite_anchor_rect_global
from .sprite_collision import SpriteCollisionWorld
from .sprite_physics import ThrowPhysicsController
from .sprite_sound import SpriteSoundPlayer
from .tick_driver import TickDriver

ENV_TOPOLOGY = overlay_settings_command.ENV_TOPOLOGY
TOPOLOGY_OVERLAY = overlay_settings_command.TOPOLOGY_OVERLAY

# 联动动作链里「一次性动作正在播」（window.py _is_one_shot_playing 的 sprite 等价物）：
# 动作池 / 点击回应 / 移动三类都不可被打断，联动请求排进待播槽。
_LINK_ONESHOT_STATES = (STATE_ACTS, STATE_CLICK, STATE_MOVE)


def _import_quick_chat():
    """取 ``QuickChatBubble``；无聊天打包变体返回 None（app.py 既有守卫惯例）。

    打包变体以 ``excludes=['pet.chat']`` 排除聊天模块（见 ``pet/__main__.py
    _chat_available``），此时 ``pet.quick_chat`` 的模块级 ``from .chat.service
    import ChatService`` 抛 ImportError。只吞 ``pet.chat`` 系的 ImportError
    （返回 None = 无聊天变体）；其它导入错误照旧抛出，由壳层统一降级并落
    日志，不在这里静默吞掉（不掩盖真实故障）。
    """
    try:
        from .quick_chat import QuickChatBubble
    except ImportError as exc:
        if str(getattr(exc, "name", "") or "").startswith("pet.chat"):
            return None
        raise
    return QuickChatBubble


class _SpriteChatAnchor:
    """快速对话气泡的 pet 形锚点（``QuickChatBubble.position_near_pet`` 只读两面）。

    ``quick_chat.py`` 只用 ``visible_content_rect()`` 定位（242-285）与
    ``on_open_chat`` 打开完整聊天窗（445-450），不需要 PetWindow 的其余面；
    每个 sprite 一个锚点，被点中的是哪一只就锚在哪一只头顶。
    """

    __slots__ = ("_shell", "_sprite")

    def __init__(self, shell, sprite) -> None:
        self._shell = shell
        self._sprite = sprite

    def visible_content_rect(self) -> QRect:
        """身体框全局矩形（气泡锚点，口径同 SpriteBubbleFollower.anchor）。"""
        overlay = getattr(self._shell, "overlay", None)
        if overlay is None:
            return QRect()
        return sprite_anchor_rect_global(self._sprite, overlay.geometry().topLeft())

    def on_open_chat(self) -> None:
        """气泡内「完整聊天窗」入口（QuickChatBubble._open_full_chat 调用面）。"""
        self._shell.open_full_chat()


class _LinkAnimChain:
    """联动动作链接续（``window.py _on_anim_ended`` → ``_link_next_provider`` 等价物）。

    PetWindow 靠动画结束回调接续联动动作；sprite 世界的行为控制器没有结束
    回调面（``sprite_behavior`` 不在本刀范围），改由驱动器 extras 每 tick
    观测「一次性动作结束」的下降边沿，等价地接续待播动作/下一个联动动作。
    零新线程、每 tick 一次 ``state_of`` 查询（字典取值）。
    """

    def __init__(self, shell) -> None:
        self._shell = shell
        self._was_busy = False

    def tick(self, sprites, dt: float) -> None:
        shell = self._shell
        busy = shell._link_anim_busy()
        was_busy, self._was_busy = self._was_busy, busy
        if busy or not was_busy:
            return
        # 一次性动作刚播完：待播优先，否则向 provider 要下一个
        # （顺序同 legacy _on_anim_ended：先消费待播，再问联动链）
        if shell._pending_link_anim:
            shell._play_pending_link_anim()
            return
        provider = shell._link_next_provider
        if not callable(provider):
            return
        try:
            nxt = provider()
        except Exception:
            logging.debug("overlay: 联动动作链取下一个动作失败", exc_info=True)
            return
        if nxt:
            shell.request_link_anim(str(nxt))


def is_overlay_topology() -> bool:
    """唯一拓扑判定入口（T5：dev flag，不进 Config/设置页/schema）。

    实现在零 Qt 的 ``overlay_settings_command``：设置进程（``--settings``，
    ``pet/__main__.py`` 明确禁止导入 pet.app/overlay_shell）也要按拓扑决定
    D12 指令通道走不走，故 env 读取的实现必须落在两侧都能 import 的模块；
    本函数保留为对外唯一入口名（app.py/overlay_instance_gate 照旧转发）。
    """
    return overlay_settings_command.is_overlay_topology()


class _SettingsIdentityConfig:
    """身份载体的 config 面（``open_settings_process`` 只读 ``instance_id``）。"""

    __slots__ = ("instance_id",)

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id


class _SettingsIdentity:
    """D13：喂给 ``AppShell.open_settings_process`` 的身份载体（鸭子类型）。

    该入口只读 ``instance.config.instance_id``（app.py:1518-1523）：主身份与
    进程级 ``DSH_PET_INSTANCE`` 同值 → 命令逐字不变；子 sprite 的 ``slot-N``
    不等于 env → 追加 ``--instance slot-N``，独立设置进程因此打开该子肥鱼的
    ``config-slot-N.json`` 而不是静默打开主宠配置。这里不建 Config、不碰磁盘。
    """

    __slots__ = ("config",)

    def __init__(self, instance_id: str) -> None:
        self.config = _SettingsIdentityConfig(instance_id)


def _screen_name(screen) -> str:
    try:
        return str(screen.name())
    except Exception:
        return "?"


class ShellOverlayWindow(OverlayWindow):
    """4.1a 产品 overlay：挂进程级驱动器 + 单击/拖拽区分。

    仿真段不再经本类（M-2：驱动器独立持有控制器，tick 顺序协议在
    tick_driver.TickDriver.tick_sim 固化）；overlay 只把 advance+paint 段
    与鼠标路由做完。behavior 属性由壳层挂载，contextMenuEvent（基类）查表
    读它。点击 vs 拖拽的阈值判定口径同 run_overlay_demo.py：按下位移小于
    DRAG_THRESHOLD*scale 视为单击。
    """

    def __init__(self, screen, *, driver: TickDriver | None = None) -> None:
        super().__init__(screen=screen, driver=driver)
        self._press_pos = None
        self.behavior = None  # OverlayShell 挂载；contextMenuEvent 查表读它
        self.edge_probe = None  # OverlayShell 挂载；拖拽/点击事件接线
        self.setAcceptDrops(True)  # 4.1c 投喂（命中 sprite 才 accept）

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._press_pos = event.position()
        super().mousePressEvent(event)
        if (self._mouse_grab is not None and self.edge_probe is not None
                and not self.slingshot.aiming):
            self.edge_probe.on_sprite_drag_started(self._mouse_grab)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self.slingshot.aiming:
            # 弹弓瞄准中的左键松手 = 发射（基类已消费），不进单击/拖拽判别
            self._press_pos = None
            super().mouseReleaseEvent(event)
            return
        grab = self._mouse_grab
        press, self._press_pos = self._press_pos, None
        super().mouseReleaseEvent(event)
        if grab is None or press is None or self.behavior is None:
            return
        threshold = catalog.DRAG_THRESHOLD * getattr(grab, "scale", 1.0)
        if (event.position() - press).manhattanLength() < threshold:
            # 边缘探头消费点击（PEEKING 拉直/STRAIGHTENED 重置倒计时）时，
            # 抑制点击反应与点击音效——拉直本身就是反馈
            if self.edge_probe is not None and self.edge_probe.on_sprite_clicked(grab):
                return
            self.behavior.on_sprite_clicked(grab)
            squash = getattr(grab, "squash", None)
            if callable(squash):
                squash()  # 4.1c 点击 Q 弹（window.py:3473 语义）
            cb = getattr(self, "click_feedback", None)
            if callable(cb):
                cb()  # 4.1c 点击音效（有无 click 素材都发声，同旧架构）
        elif self.edge_probe is not None:
            # 真拖拽释放（非单击）：通知探头按 tick 静止判定重新评估进入
            self.edge_probe.on_sprite_drag_released(grab)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        feeding = getattr(self, "_feeding", None)
        if feeding is not None:
            feeding.handle_drag_enter(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        feeding = getattr(self, "_feeding", None)
        if feeding is not None:
            feeding.handle_drop(event)
        else:
            event.ignore()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """4.1c：有全量菜单建造器（产品壳）走 facade 菜单，否则基类最小集。

        D13：把**被点中的 sprite** 透传给建造器——菜单里的设置入口要按它自己的
        config 身份打开（否则右击子肥鱼的「桌宠设置」会静默打开主宠配置）。
        """
        builder = getattr(self, "_full_menu_builder", None)
        if builder is None:
            super().contextMenuEvent(event)
            return
        target = self.sprite_at(event.pos())
        if target is None:
            event.ignore()
            return
        menu = builder(target)
        menu.exec(event.globalPos())
        event.accept()


class OverlayShell(QObject):
    """overlay 拓扑产品壳：主屏 overlay + 主 sprite + 进程级三控制器。

    依赖最小化：QApplication + 主 PetInstance（config 与 per-pet MovieLibrary
    身份源）。屏事件/会话事件/退出收口全部在本类内自接，AppShell 只在
    拓扑分支处构造并 start()。

    4.3 后半：本类同时是进程级共享子系统（agent_link / proactive）的呈现
    扇出目标——``agent_link_manager`` / ``proactive_watcher`` 由 AppShell 在
    拓扑分支处注入（等价 PetWindow 的构造参数）。注入失败=None 时全部呈现面
    静默空转，绝不阻断启动。
    """

    def __init__(self, app, instance, *, screen=None, sprite_factory=None,
                 agent_link_manager=None, proactive_watcher=None) -> None:
        super().__init__()
        self.app = app
        self._instance = instance
        self._config = instance.config
        self._screen = screen if screen is not None else app.primaryScreen()
        self._sprite_factory = sprite_factory or (
            lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
        self._started = False
        self._session_end_done = False
        self.behavior: BehaviorController | None = None
        self.collision: SpriteCollisionWorld | None = None
        self.physics: ThrowPhysicsController | None = None
        self.driver: TickDriver | None = None
        self.overlay: ShellOverlayWindow | None = None
        self.sprite = None
        self.lib = None
        self.tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._tray_actions: list = []
        self._session_watcher = None
        # D12 指令通道（overlay 拓扑：独立设置进程 → 主进程）；未装时为惰性空转
        self._command_watcher = None
        self._command_timer = QTimer(self)
        self._bounds = QRect()
        # 4.2b 子肥鱼登记：sprite 顺序 = spawn 顺序 = 活跃宠清单顺序
        self._spawned: list = []
        self._spawned_libs: dict = {}
        self._spawned_slots: dict = {}
        # 4.3 后半：共享子系统注入（等价 PetWindow 的构造参数；None = 惰性空转）
        self.agent_link_manager = agent_link_manager
        self.proactive_watcher = proactive_watcher
        # 呈现/提醒状态（PetWindow 同名私有面的 sprite 等价物；window_alerts 的
        # host 形函数与 agent_link/proactive 的聚合读取都直接读这些属性）
        self._alert_queue: deque = deque()
        self._alert_current: dict | None = None
        self._sticky_bubble_active = False
        self._sticky_text = ""
        self._sticky_subtitle = ""
        self._sticky_buttons: list | None = None
        self._bubble_busy_until = 0.0
        self._bubble_suppressed = False
        self._last_sticky_restore = 0.0
        # 联动动作链（agent_link 的 request_link_anim/idle 落点）
        self._pending_link_anim: str | None = None
        self._link_anim_current: str | None = None
        self._link_next_provider = None
        self._link_chain: _LinkAnimChain | None = None
        # 快速对话气泡（懒建；无聊天变体 = None 静默降级）
        self._quick_chat = None
        self._quick_chat_resolved = False
        self._quick_chat_cls = None
        self._build()
        self._wire_screen_signals()
        self._install_session_watcher()
        self.app.aboutToQuit.connect(self._on_about_to_quit)

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        self._bounds = self._local_bounds(self._screen)
        self.behavior = BehaviorController(self._bounds)
        self.collision = SpriteCollisionWorld()
        self.physics = ThrowPhysicsController(self._bounds)
        # 4.1c 音效：点击 + 碰撞（click_sound 默认包，静默降级）
        self._sound = SpriteSoundPlayer(self._config, self.collision)
        self.collision.add_collision_listener(self._sound.on_collision)
        # 4.1c 碰撞 Q 弹：真撞击量级 → 双方 sprite 挤压
        self.collision.add_collision_listener(self._on_collision_squash)
        # M-2 统一 tick 驱动器（T2）：进程级一组控制器挂在驱动器上，overlay
        # 只做 advance+paint；屏迁移重建 overlay 时复用同一驱动器（成员替换）。
        self.driver = TickDriver(self)
        self.driver.set_controllers(self.behavior, self.collision, self.physics)
        # 边缘探头（edge_probe 移植）：第四控制器挂 tick 尾段（姿态最终写
        # 入口）；config 键 edge_probe_enabled（默认 False）世界内每次进入
        # 判定前热读，设置页开关即切即生效
        from .sprite_edge_probe import create_edge_probe_world
        self._probe = create_edge_probe_world(self._config, QRect(self._bounds))
        self.driver.add_extra_controller(self._probe)
        self.collision.add_collision_listener(self._on_collision_probe)
        # throw_egg 彩蛋：探头被撞飞头部跟随速度（extras 尾段，读物理结算后
        # 的速度——落地兜底依赖 physics 已切回 normal，顺序天然满足）
        from .sprite_throw_egg import create_throw_egg_world
        self._throw_egg = create_throw_egg_world(
            QRect(self._bounds), probe=self._probe)
        self.driver.add_extra_controller(self._throw_egg)
        self.overlay = ShellOverlayWindow(self._screen, driver=self.driver)
        self.overlay.behavior = self.behavior
        self.overlay.edge_probe = self._probe
        self.overlay.click_feedback = self._sound.on_click
        # 4.1c 弹弓：controller 由 OverlayWindow 自持，这里只接 config
        # （slingshot_enabled 热读，设置页即改即生效）
        self.overlay.slingshot.config = self._config
        # 4.1c 全量右键菜单（facade 适配旧 context_menus 建造器）
        from .sprite_menu_facade import build_sprite_full_menu
        self.overlay._full_menu_builder = (
            lambda target: build_sprite_full_menu(self, target))
        self.lib = self._create_main_library()
        # 首跑帧序列自动供给（B 档）：口径同 app._create_library，库内幂等
        getattr(self.lib, 'maybe_provision_frameseq', lambda: None)()
        scale = float(self._config.get("scale") or catalog.DEFAULT_SCALE)
        self.sprite = self._sprite_factory(self.lib, QPointF(0, 0), scale)
        self.sprite.home_screen = self._screen
        # DPR 由 overlay.add_sprite 按所在屏统一喂（V-11 收口，不再双喂）
        self.sprite.set_bounds(QRect(self._bounds))
        self.overlay.add_sprite(self.sprite)
        # 4.2b：sprite 移除时注销行为状态（spawn/退出子肥鱼的清理链）
        self.overlay.add_sprite_removed_listener(self.behavior.forget)
        self.overlay.add_sprite_removed_listener(self._probe.forget)
        self.overlay.add_sprite_removed_listener(self._throw_egg.forget)
        # 4.2a：按 rx/ry 比例恢复上次位置（无记录 → 默认右下角）
        self._restore_position()
        # 4.2b：按活跃宠清单复活子肥鱼（D5：依据是运行时清单，不是 slot 配置存在）
        self._restore_spawned_pets()
        # 4.1c 投喂（拖文件喂 sprite，命中判定与穿透同口径）
        self._bind_feeding()
        # 4.1b 窗口能力：on_top/穿透复合/全屏与光标监视/runtime 避让标记
        self._auto_hidden = False
        self._user_mouse_through = bool(self._config.get("mouse_through", False))
        self._auto_cursor_hidden = False
        self._cursor_restore_pending = False
        self._watcher = FullscreenCursorWatcher(self)
        self._watcher.fullscreen_changed.connect(self._on_fullscreen_changed)
        self._watcher.cursor_visibility_changed.connect(
            self._on_cursor_visibility_changed)
        self.overlay._through_changed = self._on_user_through_changed
        self.overlay._grab_finished_cb = self._on_grab_finished
        self.overlay.add_position_listener(self.sprite, self._on_main_sprite_moved)
        self._last_marker_write = 0.0
        self._apply_window_capabilities()
        # 4.1c 气泡跟随（真实 PetSpeechBubble；静默降级）
        self._bind_bubble()
        # 4.3 后半：联动动作链接续控制器（extras 尾段观测一次性动作结束边沿）
        self._link_chain = _LinkAnimChain(self)
        self.driver.add_extra_controller(self._link_chain)
        self._install_shared_link()
        self._build_tray()
        self._install_settings_command_watch()

    def _create_main_library(self):
        """per-pet MovieLibrary（T3）；角色素材缺失回退默认角色（口径同
        AppShell._create_ui_with_character_fallback）。"""
        character_id = str(self._config.get('character', catalog.DEFAULT_CHARACTER))
        try:
            return self._instance._create_library(character_id)
        except FileNotFoundError:
            if character_id == catalog.DEFAULT_CHARACTER:
                raise
            logging.warning('角色 %s 素材缺失，回退默认角色 %s',
                            character_id, catalog.DEFAULT_CHARACTER)
            self._config.set('character', catalog.DEFAULT_CHARACTER)
            return self._instance._create_library(catalog.DEFAULT_CHARACTER)

    @staticmethod
    def _local_bounds(screen) -> QRect:
        """屏幕可用工作区换算成 overlay 局部坐标（overlay 铺满屏幕几何）。"""
        geo = screen.geometry()
        avail = screen.availableGeometry()
        return QRect(avail.x() - geo.x(), avail.y() - geo.y(),
                     avail.width(), avail.height())

    @staticmethod
    def _default_corner_pos(bounds: QRect, rect: QRect) -> QPointF:
        """默认右下角（对齐 window_placement._default_corner_pos 的旧算式：
        右缘留 CORNER_MARGIN、底贴可用区底）。"""
        return QPointF(bounds.x() + bounds.width() - 1 - rect.width() - catalog.CORNER_MARGIN,
                       bounds.y() + bounds.height() - 1 - rect.height())

    # ---------------------------------------------------------------- 主 sprite 接线
    def _bind_feeding(self) -> None:
        """投喂控制器只认主 sprite；overlay/主 sprite 更换后必须重挂，
        否则投喂命中判定仍打在旧 sprite 的几何上（4.2b 主实例提升同理）。"""
        from .sprite_feeding import SpriteFeedingController
        self._feeding = SpriteFeedingController(
            self.overlay, self.sprite, self._config, self.behavior)
        self._feeding._bubble_cb = self._say_feeding_bubble
        self.overlay._feeding = self._feeding

    def _bind_bubble(self) -> None:
        """气泡跟随器只认主 sprite；重建前先关旧跟随器（防旧顶层气泡残留）。

        4.3 后半：跟随器接上点击回调（快速对话入口）+ ``hidden_signal``
        （提醒队列推进/粘滞气泡恢复，``window_alerts.on_speech_bubble_hidden``），
        并按聊天可用性切换气泡的可点状态。
        """
        follower = getattr(self, "_bubble_follower", None)
        if follower is not None:
            follower.close()
        self._bubble_follower = SpriteBubbleFollower(
            self.overlay, self.sprite, on_clicked=self._on_bubble_clicked)
        bubble = self._speech_bubble
        if bubble is not None:
            try:
                bubble.hidden_signal.connect(self._on_speech_bubble_hidden)
            except (AttributeError, RuntimeError, TypeError):
                logging.debug("overlay: 气泡 hidden 信号接线失败", exc_info=True)
        if self._quick_chat_resolved:
            # 快速对话可用性已解析过（不重付 import 成本）：新气泡直接对齐
            # 可点状态；否则等首次冒泡时由 show_bubble 的
            # _apply_bubble_interactive 惰性解析（与 legacy 触点一致）。
            self._apply_bubble_interactive()

    def _install_shared_link(self) -> None:
        """把本壳接进共享联动链（``AppShell._wire_shared_subsystems`` 的等价物）。

        共享 manager 的 ``win`` 是 ``MultiWindowProxy``，其 ``__init__`` 期
        注入 provider 时 overlay 壳还不存在（AppShell.start() 才构造），
        扇出集合为空 → provider 永远送不到。这里由壳自接一次；legacy 路径
        仍由 app.py 在每窗创建后调用，两边不重叠。
        """
        shared = getattr(getattr(self._instance, "shell", None), "_shared", None)
        if shared is None:
            return
        try:
            shared.proxy.set_link_next_provider(shared.agent_link._next_busy_anim)
        except Exception:
            logging.debug("overlay: 接入共享联动链失败", exc_info=True)

    # ---------------------------------------------------------------- 4.3 后半：共享子系统呈现面
    #
    # 本段是 PetWindow 呈现面的 sprite 等价物，消费方是进程级共享子系统：
    #   - ``MultiWindowProxy``（agent_link / proactive 的 win）：读聚合状态属性
    #     （isVisible/_dragging/_physics_mode/_click_effect_phase/mouse_through/
    #     _bubble_busy_until/_bubble_suppressed/_sticky_bubble_active/
    #     _alert_current/_alert_queue/agent_link_manager）并调用呈现方法；
    #   - ``window_alerts`` 的 host 形函数：show_alert/resolve_alert/
    #     pump_alerts/clear_alerts/hide_bubble/on_speech_bubble_hidden/
    #     set_bubble_suppressed——直接复用同一份提醒队列实现（不重复造）。
    # 缺失任一方法只会让对应功能静默降级，绝不抛到调用方。
    @property
    def _speech_bubble(self):
        """主 sprite 的气泡控件（PetWindow._speech_bubble 的等价物；无气泡=None）。"""
        return getattr(getattr(self, "_bubble_follower", None), "bubble", None)

    @property
    def scale(self) -> float:
        """主 sprite 缩放（气泡字号/锚点的 pet_scale 来源）。"""
        return float(getattr(self.sprite, "scale", 1.0) or 1.0)

    def isVisible(self) -> bool:  # noqa: N802 (Qt/PetWindow 命名)
        """聚合可见性 = overlay 是否可见（隐藏/全屏自动隐藏都算不可见）。"""
        overlay = getattr(self, "overlay", None)
        return bool(overlay is not None and overlay.isVisible())

    def visible_content_rect(self) -> QRect:
        """主 sprite 身体框的全局矩形（气泡锚点；window_placement 口径的 sprite 版）。"""
        if self.sprite is None:
            return QRect()
        return sprite_anchor_rect_global(self.sprite, self.overlay.geometry().topLeft())

    @property
    def _dragging(self) -> bool:
        """拖拽中（proactive G1 守卫的 interacting 判定之一）。"""
        return getattr(self.overlay, "_mouse_grab", None) is not None

    @property
    def _physics_mode(self):
        """'drag'/'throw'/None——**必须保持哨兵语义**（None = 不在物理模式）。

        消费方读 ``getattr(win, "_physics_mode", None) is not None``：返回
        ``False`` 会让 G1 恒真拦截（multi_window_shared 的同款注释记录了
        legacy 侧踩过的坑）。
        """
        state = getattr(self.sprite, "interaction_state", None)
        if state == INTERACTION_DRAG:
            return "drag"
        if state == INTERACTION_THROWN:
            return "throw"
        return None

    @property
    def _click_effect_phase(self) -> int:
        """sprite 世界没有点击效果相位（旧路径的 squash 相位计数）→ 恒 0。"""
        return 0

    @property
    def mouse_through(self) -> bool:
        return bool(getattr(self.overlay, "mouse_through", False))

    @property
    def cats(self) -> dict:
        """素材分类（联动动作链按 acts 名筛选用）。失败回空字典，不抛。"""
        try:
            return dict(self.behavior._categories(self.sprite.library))
        except Exception:
            logging.debug("overlay: 取素材分类失败", exc_info=True)
            return {}

    @property
    def idles(self) -> list:
        return list(self.cats.get("idles", []) or [])

    @property
    def on_look_synced(self):
        """主动识屏答复同步进 AI 会话（``PetInstance.sync_look_to_chat`` 注入面）。

        无聊天变体 / 未启用聊天 → None（proactive 侧 callable 判定后静默跳过）；
        这正是 app.py ``_wire_window`` 里 ``win.on_look_synced`` 的等价注入。
        """
        if not self._chat_enabled():
            return None
        return getattr(self._instance, "sync_look_to_chat", None)

    @property
    def hidden_bubble_redirect(self):
        """桌宠隐藏时的气泡改道面（灵动岛反馈气泡；``window_alerts`` 读它）。

        可见时返回 None：改道只在隐藏期有意义，且 AppShell 的
        ``_island_feedback_bubble`` 用 ``_aggregate_pet_visible()``（读
        ``instances[].win``）判定——overlay 拓扑下恒为 False，故这里先按
        本壳自己的可见性过滤，语义与 legacy 逐条对齐。

        agent_link 侧经共享 ``MultiWindowProxy`` 读到本属性（proxy 仅在
        overlay 拓扑转发它，见 multi_window_shared）。
        """
        if self.isVisible():
            return None
        shell = getattr(self._instance, "shell", None)
        redirect = getattr(shell, "_island_feedback_bubble", None)
        return redirect if callable(redirect) else None

    # ---- 气泡呈现（PetWindow.show_bubble / hold_bubble 等价）----
    def show_bubble(self, text: str, duration_ms: int = 3200,
                    subtitle: str | None = None, *, sticky: bool = False,
                    buttons: list | None = None, title_first: bool = False,
                    width_locked: bool = False, **_ignored) -> None:
        """向主 sprite 头顶冒泡（window.py:3850-3886 的 sprite 等价语义）。"""
        if not self.isVisible() or self._bubble_suppressed:
            return
        if self._speech_bubble is None:
            return
        if not sticky and not buttons and self._alert_current is not None:
            return  # 有提醒在展示：普通气泡让路，绝不覆盖审批弹窗
        if sticky or buttons:
            self._sticky_bubble_active = True
            self._sticky_text = str(text)
            self._sticky_subtitle = str(subtitle or "")
            self._sticky_buttons = list(buttons) if buttons else None
            self._show_bubble_text(self._sticky_text, 0,
                                   subtitle=self._sticky_subtitle,
                                   sticky=True, buttons=self._sticky_buttons)
            return
        self._apply_bubble_interactive()
        self.hold_bubble(duration_ms / 1000.0 + 2.0)
        self._show_bubble_text(str(text), duration_ms,
                               subtitle=str(subtitle or ""),
                               title_first=title_first,
                               width_locked=width_locked)

    def _show_bubble_text(self, text: str, duration_ms: int, **kwargs) -> bool:
        follower = getattr(self, "_bubble_follower", None)
        if follower is None:
            return False
        return follower.show(text, duration_ms, **kwargs)

    def hold_bubble(self, seconds: float) -> None:
        """声明重要气泡占用时长（联动/识屏气泡在此期间让路）。"""
        self._bubble_busy_until = max(
            self._bubble_busy_until, time.monotonic() + max(0.0, float(seconds)))

    def hide_bubble(self) -> None:
        """主动关闭当前气泡并推进提醒队列（window_alerts 同源实现）。"""
        if self._speech_bubble is None:
            self._clear_sticky_state()
            return
        window_alerts.hide_bubble(self)

    def clear_alerts(self) -> None:
        """清空提醒队列并关闭当前提醒（DSH 离线/重启收口）。"""
        if self._speech_bubble is None:
            self._alert_queue.clear()
            self._clear_sticky_state()
            return
        window_alerts.clear_alerts(self)

    def show_alert(self, text: str, *, subtitle: str = "", duration_ms: int = 0,
                   buttons: list | None = None, sticky: bool = True,
                   alert_id: str = "", priority: int = 3,
                   alert_type: str = "watchdog", metadata: dict | None = None) -> None:
        """提醒入队（审批/问题/硬失败/卡住提醒；一次只展示一个）。"""
        if self._speech_bubble is None:
            return
        window_alerts.show_alert(
            self, text, subtitle=subtitle, duration_ms=duration_ms,
            buttons=buttons, sticky=sticky, alert_id=alert_id,
            priority=priority, alert_type=alert_type, metadata=metadata)

    def resolve_alert(self, alert_id: str) -> None:
        """按 alert_id 精确收起某条提醒（并发审批各自定位，不误关他人）。"""
        if self._speech_bubble is None:
            return
        window_alerts.resolve_alert(self, alert_id)

    def set_bubble_suppressed(self, suppressed: bool) -> None:
        """设置页打开期间暂停气泡（PetWindow.set_bubble_suppressed 等价）。"""
        if self._speech_bubble is None:
            self._bubble_suppressed = bool(suppressed)
            return
        window_alerts.set_bubble_suppressed(self, suppressed)

    def _pump_alerts(self) -> None:
        """弹出队首提醒（window_alerts 的 host 回调面）。"""
        if self._speech_bubble is None:
            return
        window_alerts.pump_alerts(self)

    def _on_speech_bubble_hidden(self, *_args, **_kwargs) -> None:
        """气泡隐藏后的恢复/队列推进（PetWindow 同款委托）。"""
        if self._speech_bubble is None:
            return
        window_alerts.on_speech_bubble_hidden(self)

    def _clear_sticky_state(self) -> None:
        self._sticky_bubble_active = False
        self._sticky_text = ""
        self._sticky_subtitle = ""
        self._sticky_buttons = None

    # ---- 联动动作（agent_link 的 request_link_* 落点）----
    def _link_anim_busy(self) -> bool:
        """一次性动作（动作池/点击/移动）是否在播——联动请求不打断它。"""
        behavior = getattr(self, "behavior", None)
        if behavior is None:
            return False
        try:
            return behavior.state_of(self.sprite) in _LINK_ONESHOT_STATES
        except Exception:
            logging.debug("overlay: 读行为状态失败", exc_info=True)
            return False

    def switch_clip(self, name: str, link_request: bool = False) -> bool:
        """播放指定动画（window.switch_clip 语义：一次性，播完回掷骰链）。"""
        if self.behavior is None or not str(name or ""):
            return False
        try:
            return bool(self.behavior.play_once(self.sprite, str(name)))
        except Exception:
            logging.debug("overlay: 切换动画失败 %s", name, exc_info=True)
            return False

    def request_link_anim(self, name: str) -> None:
        """Agent 联动动作请求：一次性动作播放中不打断，存为待播（最新覆盖旧的）。"""
        self.mark_activity()
        name = str(name or "")
        if not name:
            return
        self._pending_link_anim = name
        if not self._link_anim_busy():
            self._play_pending_link_anim()

    def _play_pending_link_anim(self) -> None:
        name, self._pending_link_anim = self._pending_link_anim, None
        if not name:
            return
        self._link_anim_current = name
        self.switch_clip(name)

    def request_link_idle(self) -> None:
        """Agent 回到空闲：取消待播联动；一次性动作让它播完自然回待机。"""
        self._pending_link_anim = None
        self._link_anim_current = None
        if self._link_anim_busy():
            return
        idles = self.idles
        if idles:
            self.switch_clip(self._pick_idle(idles))

    def _pick_idle(self, idles: list) -> str:
        rng = getattr(getattr(self, "behavior", None), "rng", None)
        chooser = getattr(rng, "choice", None)
        if callable(chooser):
            try:
                return str(chooser(list(idles)))
            except Exception:
                logging.debug("overlay: 随机待机取素材失败", exc_info=True)
        return str(idles[0])

    def set_link_next_provider(self, provider) -> None:
        """注入联动动作链「下一个动作」提供者（``_LinkAnimChain`` 消费）。"""
        self._link_next_provider = provider

    def clear_pending_link_anim(self) -> None:
        self._pending_link_anim = None

    def mark_activity(self) -> None:
        """用户/联动活跃锚点。

        overlay 路径尚无闲置降帧门（D9 未落地项），本方法保留调用面以承接
        agent_link 的事件语义，等降帧刀落地后在此接真。
        """

    # ---- 快速对话（气泡可点 → 锚定 sprite 的输入气泡）----
    def _chat_enabled(self) -> bool:
        """进程级聊天开关（E2 单源 = AppShell.enable_chat，经 PetInstance 转发）。"""
        return bool(getattr(self._instance, "enable_chat", True))

    def quick_chat_available(self) -> bool:
        """快速对话是否可用（未启用聊天 / 无聊天打包变体 → False）。"""
        return self._quick_chat_class() is not None

    def _quick_chat_class(self):
        """解析 ``QuickChatBubble``（结果缓存；不可用时 None，**绝不抛**）。

        呈现面纪律：入口探测失败一律降级——气泡/联动链路不能因为一个可选
        外围模块而中断；真故障仍由 ``_import_quick_chat`` 的 log 暴露。
        """
        if not self._chat_enabled():
            return None
        if not self._quick_chat_resolved:
            self._quick_chat_resolved = True
            try:
                self._quick_chat_cls = _import_quick_chat()
            except Exception:
                logging.exception("overlay: 快速对话模块导入失败，入口静默降级")
                self._quick_chat_cls = None
            else:
                if self._quick_chat_cls is None:
                    logging.info("overlay: 无聊天变体，快速对话入口静默降级")
        return self._quick_chat_cls

    def _apply_bubble_interactive(self) -> None:
        """按快速对话可用性切换气泡可点（旧路径 ``_set_speech_bubble_interactive``）。"""
        follower = getattr(self, "_bubble_follower", None)
        if follower is not None:
            follower.set_interactive(self.quick_chat_available())

    def _on_bubble_clicked(self, sprite) -> None:
        """气泡主体点击 → 快速对话（``window.py:3419`` 语义：提醒/交互气泡 no-op）。"""
        if self._sticky_bubble_active or self._alert_current is not None:
            return
        self.open_quick_chat(sprite)

    def open_quick_chat(self, sprite=None) -> bool:
        """打开快速对话输入气泡，锚定被点 sprite；回车发送走既有 chat 链路。

        返回是否真的打开（无聊天变体/构造失败 → False，静默降级）。
        """
        cls = self._quick_chat_class()
        if cls is None:
            return False
        target = self.sprite if sprite is None else sprite
        anchor = _SpriteChatAnchor(self, target)
        try:
            bubble = self._quick_chat
            if bubble is None:
                bubble = cls(self._config, pet_window=anchor)
                bubble.open_chat_callback = self.open_full_chat
                self._quick_chat = bubble
            else:
                bubble.settings = self._config.chat_settings()
                bubble.refresh_session()
            bubble.show_for_pet(anchor)
            return True
        except Exception:
            logging.exception("overlay: 打开快速对话失败")
            return False

    def open_full_chat(self) -> None:
        """打开完整聊天窗（快速对话气泡的「全文见聊天窗」入口）。"""
        opener = getattr(self._instance, "open_chat", None)
        if not callable(opener):
            return
        try:
            opener()
        except Exception:
            logging.exception("overlay: 打开完整聊天窗失败")

    def _close_quick_chat(self) -> None:
        """收起并释放快速对话气泡（stop/aboutToQuit 收口）。"""
        bubble, self._quick_chat = self._quick_chat, None
        if bubble is None:
            return
        try:
            bubble.close()
        except Exception:
            logging.debug("overlay: 收起快速对话失败", exc_info=True)

    # ---- 显隐：共享子系统 pause/resume（window.py:1214-1250 语义）----
    def _pause_shared_subsystems(self) -> None:
        """隐藏 → proactive.pause + agent_link.pause（岛反馈面可用时不停联动）。

        共享实例（overlay 拓扑恒为 SharedSubsystems）的 ``pause`` 是刻意的
        no-op——单窗显隐不该停进程级监视器，G1 守卫逐 tick 读 ``isVisible()``
        拦下截图（限流器状态因此不丢）。本方法保留调用面与 legacy 逐条对齐。
        """
        proactive = getattr(self, "proactive_watcher", None)
        if proactive is not None:
            try:
                proactive.pause()
            except Exception:
                logging.debug("overlay: 暂停主动识屏失败", exc_info=True)
        manager = getattr(self, "agent_link_manager", None)
        if manager is None:
            return
        if self._island_feedback_available():
            return  # 隐藏期间岛是交互面，联动事件仍需驱动岛反馈气泡
        try:
            manager.pause()
        except Exception:
            logging.debug("overlay: 暂停联动监视器失败", exc_info=True)

    def _resume_shared_subsystems(self) -> None:
        """恢复显示 → proactive.resume + agent_link.resume（按最新配置重评估）。"""
        proactive = getattr(self, "proactive_watcher", None)
        if proactive is not None:
            try:
                proactive.resume()
            except Exception:
                logging.debug("overlay: 恢复主动识屏失败", exc_info=True)
        manager = getattr(self, "agent_link_manager", None)
        if manager is not None:
            try:
                manager.resume()
            except Exception:
                logging.debug("overlay: 恢复联动监视器失败", exc_info=True)

    def _island_feedback_available(self) -> bool:
        """岛反馈面是否可用（隐藏期气泡改道 + 联动是否暂停的判定探针）。"""
        shell = getattr(self._instance, "shell", None)
        probe = getattr(shell, "_island_feedback_available", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            logging.debug("overlay: 岛反馈面探测失败", exc_info=True)
            return False

    # ---------------------------------------------------------------- 灵动岛碰撞桥（4.3）
    def attach_island(self, island) -> None:
        """岛创建/重建/启停后接线（AppShell._sync_dynamic_island 调用）。

        幂等可重复调；island=None 即摘桥。world 必须是进程级 self.collision、
        overlay 必须是当前 overlay（原点来源）——否则墙挂到另一个世界/错位。
        """
        bridge = getattr(self, "island_bridge", None)
        if bridge is not None:
            bridge.close()
            self.island_bridge = None
        if island is None:
            return
        from .island_bridge import IslandWindowBridge
        self.island_bridge = IslandWindowBridge(
            island, self.collision, self.overlay, sound=self._sound)

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.overlay.show()
        self.overlay.start()
        self._sync_runtime_marker()  # D7：设置进程避让
        self.app.installEventFilter(self)  # Esc 全局兜底（弹弓取消）
        if self.tray is not None:
            self.tray.show()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt 命名)
        """应用级 Esc 兜底：overlay 是防抢焦点窗（WS_EX_NOACTIVATE），
        keyPressEvent 只在确有焦点时收到 Esc；弹弓瞄准中按 Esc 全局取消。"""
        from PySide6.QtCore import QEvent
        if (event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            overlay = getattr(self, "overlay", None)
            if overlay is not None and overlay.slingshot.aiming:
                overlay._cancel_slingshot(resume_drag=False)
                return True
        return super().eventFilter(watched, event)

    def stop(self) -> None:
        """幂等：重复 stop 是 no-op。"""
        if not self._started:
            return
        self._started = False
        self.app.removeEventFilter(self)
        self._teardown_settings_command_watch()
        bridge = getattr(self, "island_bridge", None)
        if bridge is not None:
            bridge.close()
            self.island_bridge = None
        self._watcher.stop()
        self._delete_runtime_marker()
        self._close_quick_chat()
        if getattr(self, "_bubble_follower", None) is not None:
            self._bubble_follower.close()
        self.overlay.stop()
        self.overlay.close()
        if self.tray is not None:
            self.tray.hide()

    def _on_about_to_quit(self) -> None:
        """退出收口：停 tick + 暂停预热 + 监视器与避让标记清理（4.1b）。"""
        self._watcher.stop()
        self._teardown_settings_command_watch()
        self._delete_runtime_marker()
        self._close_quick_chat()
        bridge = getattr(self, "island_bridge", None)
        if bridge is not None:
            bridge.close()
            self.island_bridge = None
        self.save_position()  # 4.2a：退出持久化（rx/ry/facing/scale）
        self.save_spawned_positions()  # 4.2b：子肥鱼逐只按 slot 身份持久化
        if self.overlay is not None:
            self.overlay.stop()
        pause = getattr(self.lib, "pause_warm", None)
        if callable(pause):
            try:
                pause()
            except Exception:
                logging.exception("overlay: 退出时暂停预热失败")

    # ---------------------------------------------------------------- 托盘（多宠聚合）
    def _build_tray(self) -> None:
        """建单托盘 + 双击显隐；菜单本体交给 ``_refresh_tray_menu`` 聚合重建。

        对齐 legacy（app.py:3171-3309）：进程级**单**托盘，多宠时逐只子菜单；
        图标/双击接线只做一次，菜单随生灭刷新（托盘菜单是快照，不重建会把已
        退出的身份留在菜单里）。
        """
        try:
            tray = QSystemTrayIcon(self._tray_icon(), self)
            tray.activated.connect(
                lambda reason: self._toggle_pet_visible()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None)
            tray.setToolTip("dsh-pet (overlay)")
            # 菜单本体强引用保活（app.py F5 教训：PySide6 wrapper 回收后
            # contextMenu 会命中失效 wrapper）
            self.tray = tray
            self._refresh_tray_menu()
        except Exception:
            logging.exception("overlay: 创建托盘失败")
            self.tray = None

    def _refresh_tray_menu(self) -> None:
        """重建聚合托盘菜单：进程级动作 + 逐只子菜单（legacy 单托盘多窗语义）。

        逐条对照 legacy（app.py:3203-3301，见 PR 报告）：
        - 顶层「显示 / 隐藏」「回到右下角」= legacy 主窗两项（overlay 单窗，
          显隐天然覆盖全部 sprite）；
        - 「生小肥鱼 / 退出子肥鱼」= overlay 多宠生命周期入口（legacy 在右键
          菜单/设置页，不重复造第二个实现，直接复用壳的两个公开方法）；
        - 多宠时逐只子菜单「主肥鱼 / 小肥鱼 [slot-N]」= legacy 每窗子菜单，
          含「回到右下角」「退出这只」；逐 sprite 显隐需要 per-sprite visible
          标志（4.1b 未落地项，涉及 overlay_window/pet_sprite），本刀不做，
          顶层显隐即全显全隐；
        - 「桌宠设置」按主身份路由（D13）、「退出」= app.quit。
        """
        if self.tray is None:
            return
        menu = QMenu()
        # 气泡是置顶 Tool 窗口（层级高于菜单）：弹出前先隐藏（legacy 同款）
        menu.aboutToShow.connect(self._hide_bubble_for_menu)
        menu.addAction("显示 / 隐藏", self._toggle_pet_visible)
        menu.addAction("回到右下角", lambda: self._go_default_corner(self.sprite))
        menu.addSeparator()
        menu.addAction("生小肥鱼", self.spawn_pet)
        menu.addAction("退出子肥鱼", self.clear_spawned_pets)
        if self._spawned:
            menu.addSeparator()
            for sprite, label in self._pet_entries():
                sub = menu.addMenu(label)
                sub.addAction("回到右下角",
                              lambda s=sprite: self._go_default_corner(s))
                sub.addAction("退出这只",
                              lambda s=sprite: self.exit_pet(s))
        menu.addSeparator()
        menu.addAction("桌宠设置", lambda: self.open_settings_for(self.sprite))
        menu.addAction("退出", self.app.quit)
        old = self._tray_menu
        self.tray.setContextMenu(menu)  # 新菜单先接管，旧菜单才允许释放（F5）
        self._tray_menu = menu
        # QAction wrapper 一并保活：子菜单的 menuAction 挂在父菜单 actions 里，
        # wrapper 被回收会让整棵菜单被 PySide6 判为已删除（app.py:3319 同因）
        snapshot = list(menu.actions())
        for act in list(snapshot):
            sub = act.menu()
            if sub is not None:
                snapshot.extend(sub.actions())
        self._tray_actions = snapshot
        if old is not None and old is not menu:
            old.deleteLater()

    def _pet_entries(self) -> list:
        """托盘逐只条目：(sprite, 标签)。主宠在前 = 活跃清单列表头口径。"""
        entries = [(self.sprite, "主肥鱼")]
        for sprite in self._spawned:
            slot = self._spawned_slots.get(sprite)
            label = f"小肥鱼 [slot-{slot}]" if slot is not None else "小肥鱼"
            entries.append((sprite, label))
        return entries

    def _go_default_corner(self, sprite) -> None:
        """把指定 sprite 送回右下角（window.go_default_corner 等价）。"""
        sprite.set_pos(self._default_corner_pos(self._bounds, sprite.rect()))

    def _hide_bubble_for_menu(self) -> None:
        """托盘菜单弹出前隐藏气泡（legacy menu.aboutToShow → hide_speech_bubble）。"""
        follower = getattr(self, "_bubble_follower", None)
        if follower is not None:
            follower.hide()

    def _tray_icon(self):
        """托盘图标尽力取鱼本体 idle 首帧（裁剪/精修是 4.1b 的事）；
        取不到回退系统标准图标。"""
        from PySide6.QtGui import QIcon

        try:
            cats = catalog.build_categories(
                self.lib.names(), self.lib.manifest,
                self.lib.folder_map, self.lib.folder_files)
            idle = cats["idles"][0] if cats["idles"] else None
            pm = self.lib.movie(idle).currentPixmap() if idle else None
            if pm is not None and not pm.isNull():
                return QIcon(pm)
        except Exception:
            pass
        return self.app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

    # ---------------------------------------------------------------- 4.1b 窗口能力
    def refresh_settings(self) -> None:
        """外部配置变更应用点（AppShell._apply_external_config_change 扇出）。"""
        self._apply_window_capabilities()

    def _apply_window_capabilities(self) -> None:
        """按当前配置应用窗口能力（4.1b parity）：on_top / 穿透复合 /
        全屏与光标监视器门。"""
        self.set_on_top(bool(self._config.get("on_top", True)), persist=False)
        self._user_mouse_through = bool(self._config.get("mouse_through", False))
        self._apply_effective_mouse_through()
        self._watcher.set_fullscreen_enabled(
            bool(self._config.get("auto_hide_fullscreen", True)))
        self._watcher.set_cursor_enabled(
            bool(self._config.get("cursor_hidden_passthrough", True)))
        # 预测预热提前量（config predict_prewarm_lead_ms，范围 200-600，0=关）
        try:
            lead_ms = int(self._config.get("predict_prewarm_lead_ms", 350) or 350)
        except (TypeError, ValueError):
            lead_ms = 350
        lead_ms = max(0, min(600, lead_ms))
        self.behavior._predict_lead_s = lead_ms / 1000.0
        self.behavior.predict_enabled = lead_ms > 0

    def set_on_top(self, on: bool, *, persist: bool = True) -> None:
        """窗口置顶（window.py set_on_top 等价）。"""
        on = bool(on)
        was_visible = self.overlay.isVisible()
        self.overlay.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if was_visible:
            self.overlay.show()  # setWindowFlag 重建原生窗口会先隐藏
        if persist:
            self._config.set("on_top", on)
            save = getattr(self._config, "save", None)
            if callable(save):
                save()

    def _apply_effective_mouse_through(self) -> None:
        """有效穿透 = 用户手动穿透 OR 光标自动穿透（window.py:4204 同式）。"""
        self.overlay.mouse_through = bool(
            self._user_mouse_through or self._auto_cursor_hidden)

    def _on_user_through_changed(self, on: bool) -> None:
        """菜单「鼠标穿透」直写收编：记用户意愿 + 持久化 + 复合重算。"""
        self._user_mouse_through = bool(on)
        self._config.set("mouse_through", self._user_mouse_through)
        save = getattr(self._config, "save", None)
        if callable(save):
            save()
        self._apply_effective_mouse_through()

    def _on_fullscreen_changed(self, hit: bool) -> None:
        """全屏出现 → 隐藏；退出 → 恢复（window_screen.on_fullscreen_changed 等价）。

        自动隐藏与手动隐藏同语义（4.3 后半）：隐藏期间 proactive/agent_link
        一并 pause，恢复时 resume——legacy 的 ``host.hide()`` 走自定义 hide →
        ``_pause_activity`` 正是这条链。
        """
        # 诊断留痕：探测翻转时记录判定原因（2026-09-23 实机抓到 1Hz 翻转，
        # 无原因无法归因——probe 的 why 串本来就有，只差落日志）
        why = ""
        try:
            from . import platform_win
            why = platform_win._fg_fullscreen_probe()[1]
        except Exception:
            pass
        logging.info("overlay: 全屏状态变化 hit=%s auto_hidden=%s why=%s",
                     hit, self._auto_hidden, why)
        if hit:
            if not self._auto_hidden and self.overlay.isVisible():
                self._auto_hidden = True
                self._hide_bubble_for_visibility()
                self.overlay.hide()
                self._pause_shared_subsystems()
        elif self._auto_hidden:
            self._auto_hidden = False
            self.overlay.show()
            self._restore_sticky_bubble()
            self._resume_shared_subsystems()

    def _cursor_transition_blocked(self) -> bool:
        """拖拽进行中（window._cursor_transition_blocked 等价）。"""
        return self.overlay._mouse_grab is not None

    def _on_cursor_visibility_changed(self, visibility: str) -> None:
        """光标隐藏（watcher 已做 0.2s 去抖）→ 自动穿透；恢复 → 解除。"""
        if visibility == "HIDDEN":
            if not self._cursor_transition_blocked():
                self._auto_cursor_hidden = True
                self._apply_effective_mouse_through()
        elif visibility == "SHOWING":
            if self._cursor_transition_blocked():
                self._cursor_restore_pending = True
            else:
                self._cursor_restore_pending = False
                self._auto_cursor_hidden = False
                self._apply_effective_mouse_through()

    def _on_grab_finished(self) -> None:
        """拖拽收尾（overlay._finish_grab 钩子）：冲刷光标恢复滞留。"""
        if self._cursor_restore_pending:
            self._cursor_restore_pending = False
            self._auto_cursor_hidden = False
            self._apply_effective_mouse_through()

    # ---------------------------------------------------------------- 4.2a 位置持久化
    def _pos_from_ratios(self, rx: float, ry: float, sprite=None) -> QPointF:
        """rx/ry（身体中心相对可用区比例）反解 sprite 左上角坐标。

        口径同 window_placement.restore_position：以**身体框**中心为锚，扣除
        身体框在 sprite 矩形内的偏移（body_box 未声明的角色退化为全画布）。
        """
        sprite = self.sprite if sprite is None else sprite
        body = (sprite.body_rect() if hasattr(sprite, "body_rect")
                else sprite.rect())
        bw, bh = body.width(), body.height()
        cx = self._bounds.left() + rx * self._bounds.width()
        cy = self._bounds.top() + ry * self._bounds.height()
        off_x = body.x() - sprite.rect().x()
        off_y = body.y() - sprite.rect().y()
        return QPointF(cx - bw / 2 - off_x, cy - bh / 2 - off_y)

    def _sprite_geometry(self, sprite, bounds: QRect) -> dict | None:
        """sprite 当前几何 → rx/ry/facing/scale（save_position 的共用算式）。

        bounds 非法（未初始化/退化为 0 宽高）返回 None：宁可不落盘，也不写
        NaN/Inf 比例污染下一次恢复。
        """
        if bounds.width() <= 0 or bounds.height() <= 0:
            return None
        body = (sprite.body_rect() if hasattr(sprite, "body_rect")
                else sprite.rect())
        cx = body.x() + body.width() / 2.0
        cy = body.y() + body.height() / 2.0
        geometry = {
            "rx": (cx - bounds.left()) / bounds.width(),
            "ry": (cy - bounds.top()) / bounds.height(),
            "scale": float(getattr(sprite, "scale", 1.0) or 1.0),
        }
        facing = getattr(sprite, "facing", None)
        if facing in ("left", "right"):
            geometry["facing"] = facing
        return geometry

    def _restore_position(self) -> None:
        """按"身体中心相对可用区比例"恢复位置（window_placement.restore_position
        语义；贴边钳制由 set_pos 的 body_box 钳制收口）。"""
        rx, ry = self._config.get("rx"), self._config.get("ry")
        if rx is None or ry is None:
            self.sprite.set_pos(self._default_corner_pos(
                self._bounds, self.sprite.rect()))
        else:
            self.sprite.set_pos(self._pos_from_ratios(float(rx), float(ry)))
        facing = str(self._config.get("facing", "") or "")
        if facing in ("left", "right"):
            self.sprite.facing = facing

    def save_position(self) -> None:
        """身体中心相对可用区比例持久化（window_placement.save_position 语义）。"""
        geometry = self._sprite_geometry(self.sprite, self._bounds)
        if not geometry:
            return
        for key, value in geometry.items():
            self._config.set(key, value)
        save = getattr(self._config, "save", None)
        if callable(save):
            save()

    # ---------------------------------------------------------------- 4.2b 多 sprite 生灭
    def _spawn_config_dir(self):
        """子肥鱼身份/几何的落盘目录（= 主配置目录）；假配置对象无 dir → None。"""
        return getattr(self._config, "dir", None)

    def _active_slots(self) -> list[int]:
        """进程内活跃身份（spawn 顺序）= 当前活跃宠清单的内容。"""
        return [self._spawned_slots[sprite] for sprite in self._spawned
                if sprite in self._spawned_slots]

    def _persist_active_slots(self) -> None:
        """活跃宠清单落盘（spawn/退出/复活各调一次，原子替换）。"""
        config_dir = self._spawn_config_dir()
        if not config_dir:
            return
        overlay_spawn_state.save_active_slots(config_dir, self._active_slots())

    def spawn_pet(self) -> None:
        """生小肥鱼（app.py spawn_pet 进程内路径语义 + D6 无锁身份分配）。

        分配 slot 身份 → 建 per-pet 库 + 新 sprite（自主宠向右逐级错开，重叠
        规避由 body 钳制兜底）→ 进活跃清单并落盘。分配/建库失败只记录，
        不留半只 sprite 在清单里。
        """
        config_dir = self._spawn_config_dir()
        try:
            slot = overlay_spawn_state.allocate_slot(
                config_dir, active_slots=self._active_slots())
        except Exception:
            logging.exception("overlay: 生小肥鱼失败（无可用 slot 身份）")
            return
        try:
            self._spawn_slot(slot)
        except Exception:
            logging.exception("overlay: 生小肥鱼失败 (slot=%s)", slot)
            return
        self._persist_active_slots()
        self._refresh_tray_menu()  # 托盘逐只条目随生灭重建（4.2c）
        logging.info("overlay: 已生成子肥鱼 (slot=%s)", slot)

    def _spawn_slot(self, slot: int) -> None:
        """按 slot 身份建一只子肥鱼（spawn 与重启复活共用路径，不落清单）。

        身份几何（rx/ry/facing/scale）从 ``config-slot-N.json`` 读；有记录 =
        重启复活，按比例恢复上次位置；无记录 = 首次生成，自主宠向右错开。
        角色跟随主配置（``_create_main_library``）——每宠独立角色的设置面属
        后续刀，本刀只冻结位置/朝向/尺寸的身份记忆。
        """
        config_dir = self._spawn_config_dir()
        if config_dir and not overlay_spawn_state.slot_config_exists(config_dir, slot):
            # 只在全新身份上落种（命名/剔除位置键复用 slot_manager 既有惯例，
            # 对齐 Config.__init__ 的"已有存档的 slot 一律不动"）；已有存档的
            # 身份（重启复活）一个键都不碰——否则每次重启都会用主设置刷新掉
            # 该子肥鱼自存的尺寸/位置。
            slot_manager.seed_slot_config_from_main(config_dir, slot)
        state = (overlay_spawn_state.read_slot_geometry(config_dir, slot)
                 if config_dir else {})
        lib = self._create_main_library()
        scale = float(state.get("scale") or self._config.get("scale")
                      or catalog.DEFAULT_SCALE)
        sprite = self._sprite_factory(lib, QPointF(0, 0), scale)
        sprite.home_screen = self._screen
        sprite.set_bounds(QRect(self._bounds))
        sprite.set_pos(self._spawn_sprite_pos(sprite, state))
        facing = state.get("facing")
        if facing in ("left", "right"):
            sprite.facing = facing
        self.overlay.add_sprite(sprite)
        self._spawned.append(sprite)
        self._spawned_libs[sprite] = lib
        self._spawned_slots[sprite] = slot
        if config_dir:
            # 生成/复活即落一次几何：未及优雅退出（崩溃/断电）也能按清单复活
            geometry = self._sprite_geometry(sprite, self._bounds)
            if geometry:
                overlay_spawn_state.write_slot_geometry(config_dir, slot, geometry)

    def _spawn_sprite_pos(self, sprite, state: dict) -> QPointF:
        """子肥鱼落位：有持久化比例按 rx/ry 恢复，否则自主宠向右逐级错开。"""
        rx, ry = state.get("rx"), state.get("ry")
        if rx is not None and ry is not None:
            return self._pos_from_ratios(float(rx), float(ry), sprite=sprite)
        index = len(self._spawned) + 1
        main_body = self.sprite.body_rect()
        return QPointF(main_body.x() + main_body.width() + 24 * index,
                       main_body.y())

    def _restore_spawned_pets(self) -> None:
        """D5：启动恢复依据 = 活跃宠清单（**不是** slot 配置存在）。

        清单缺失 = 首次运行语义；损坏/缺首字段由 overlay_spawn_state 防御性
        回退成空清单（只有主宠）。单条复活失败（素材缺失/身份配置被删）只
        记录并摘除该条，不拖垮启动，也不让坏条目永久留在清单里。
        """
        config_dir = self._spawn_config_dir()
        if not config_dir:
            return
        slots = overlay_spawn_state.load_active_slots(config_dir)
        if not slots:
            return
        restored: list[int] = []
        for slot in slots:
            if not overlay_spawn_state.slot_config_exists(config_dir, slot):
                logging.warning("overlay: 活跃宠 slot-%s 身份配置缺失，跳过复活", slot)
                continue
            try:
                self._spawn_slot(slot)
            except Exception:
                logging.exception("overlay: 复活子肥鱼失败 (slot=%s)", slot)
                continue
            restored.append(slot)
        if restored != slots:
            self._persist_active_slots()  # 摘掉复活失败的条目
        logging.info("overlay: 按活跃清单复活 %d 只子肥鱼", len(restored))

    def _remove_spawned_sprite(self, sprite) -> None:
        """退出一只子肥鱼：几何先落盘（配置保留）→ remove_sprite（clip 释放
        V-8 + 行为注销 V-9 挂点）→ 库 shutdown 收尾。"""
        config_dir = self._spawn_config_dir()
        slot = self._spawned_slots.pop(sprite, None)
        geometry = self._sprite_geometry(sprite, self._bounds)
        if config_dir and slot and geometry:
            overlay_spawn_state.write_slot_geometry(config_dir, slot, geometry)
        lib = self._spawned_libs.pop(sprite, None)
        if sprite in self._spawned:
            self._spawned.remove(sprite)
        self.overlay.remove_sprite(sprite)
        shutdown = getattr(lib, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                logging.exception("overlay: 子肥鱼素材库收尾失败")

    def clear_spawned_pets(self) -> None:
        """退出全部子肥鱼（app.py clear_spawned_pets 语义）：逐只出活跃清单
        （slot 配置保留），最后清单落盘为空——退出即不再复活（D5）。"""
        for sprite in list(self._spawned):
            self._remove_spawned_sprite(sprite)
        self._spawned = []
        self._persist_active_slots()
        self._refresh_tray_menu()

    def save_spawned_positions(self) -> None:
        """退出收口：逐只按各自 slot 身份持久化几何（重启复活的位置来源）。"""
        config_dir = self._spawn_config_dir()
        if not config_dir:
            return
        for sprite in list(self._spawned):
            slot = self._spawned_slots.get(sprite)
            geometry = self._sprite_geometry(sprite, self._bounds)
            if slot and geometry:
                overlay_spawn_state.write_slot_geometry(config_dir, slot, geometry)

    # ---------------------------------------------------------------- 4.2b 主实例提升
    def exit_pet(self, sprite=None) -> bool:
        """退出单只（菜单「退出这只」的 overlay 落点）。

        主宠退出 → 提升一只子宠为主（app.py P1-3 等价语义）；子宠退出 →
        仅出活跃清单（配置保留）。返回是否真的退掉了目标。
        """
        target = self.sprite if sprite is None else sprite
        if target is self.sprite:
            return self._exit_main_pet()
        if target in self._spawned:
            self._remove_spawned_sprite(target)
            self._persist_active_slots()
            self._refresh_tray_menu()
            return True
        return False

    def _exit_main_pet(self) -> bool:
        """主宠退出（app.py:2944-2956 等价）：无子宠 → 全部退出语义；
        有子宠 → 活跃清单列表头提升为主（接管主身份/持久化身份）。"""
        self.save_position()
        if not self._spawned:
            self._persist_active_slots()
            self._refresh_tray_menu()
            quit_fn = getattr(self.app, "quit", None)
            if callable(quit_fn):
                quit_fn()  # 最后一窗关闭 → 走全部退出语义
            return True
        promoted = self._spawned[0]
        promoted_lib = self._spawned_libs.pop(promoted, None)
        promoted_slot = self._spawned_slots.pop(promoted, None)
        old_main, old_lib = self.sprite, self.lib
        config_dir = self._spawn_config_dir()
        geometry = self._sprite_geometry(promoted, self._bounds)
        if config_dir and promoted_slot and geometry:
            overlay_spawn_state.write_slot_geometry(
                config_dir, promoted_slot, geometry)
        # 接管主身份：提升者的几何写进主配置（主配置 = 重启蒙主宠的持久身份）
        if geometry:
            for key, value in geometry.items():
                self._config.set(key, value)
            save = getattr(self._config, "save", None)
            if callable(save):
                save()
        self._spawned.remove(promoted)
        self.sprite = promoted
        self.lib = promoted_lib
        self.overlay.remove_sprite(old_main)
        self._persist_active_slots()
        self._refresh_tray_menu()
        self._rebind_main_sprite()
        shutdown = getattr(old_lib, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                logging.exception("overlay: 旧主肥鱼素材库收尾失败")
        logging.info("overlay: 主肥鱼退出，已提升子肥鱼 (slot=%s) 为主",
                     promoted_slot)
        return True

    def _rebind_main_sprite(self) -> None:
        """主 sprite 更换后重挂"只认主 sprite"的接线（位置监听/投喂/气泡/标记）。"""
        self.overlay.add_position_listener(self.sprite, self._on_main_sprite_moved)
        self._bind_feeding()
        self._bind_bubble()
        self._sync_runtime_marker()

    # ---------------------------------------------------------------- D7 设置进程避让标记
    def _sync_runtime_marker(self) -> None:
        """写 runtime 标记（设置进程避让读它）：主 sprite 身体框的全局几何。"""
        try:
            body = self.sprite.body_rect()
            origin = self.overlay.geometry().topLeft()
            slot_manager.write_runtime_marker(
                self._config.dir, self._config.instance_id,
                origin.x() + body.x(), origin.y() + body.y(),
                body.width(), body.height(), versioned=True)
        except Exception:
            logging.debug("overlay: 写 runtime 标记失败", exc_info=True)

    def _on_main_sprite_moved(self, _sprite) -> None:
        now = time.monotonic()
        if now - self._last_marker_write < 1.0:  # 1Hz 节流
            return
        self._last_marker_write = now
        self._sync_runtime_marker()

    def _delete_runtime_marker(self) -> None:
        try:
            slot_manager.delete_runtime_marker(
                self._config.dir, self._config.instance_id)
        except Exception:
            logging.debug("overlay: 删 runtime 标记失败", exc_info=True)

    # ---------------------------------------------------------------- D12 指令通道
    def _install_settings_command_watch(self) -> None:
        """装 D12 指令消费：config 目录 watcher + 3s 轮询兜底（不新起线程）。

        「独立设置进程写指令文件 → 主进程消费」的消费侧，机制与 app.py
        ``_install_config_watcher`` 同款：QFileSystemWatcher 盯目录（原子
        ``os.replace`` 会在目录里产生事件）+ QTimer 轮询兜底（网络盘/换 inode
        等场景可能漏事件，轮询是保底；与 ``_settings_watch_timer`` 同 3s 节奏）。
        无 config 目录（假配置对象 / 目录不可建）时整条链不装——与
        ``_spawn_config_dir()`` 的既有防御一致，绝不在测试替身上抛。
        """
        config_dir = self._spawn_config_dir()
        if not config_dir:
            return
        from PySide6.QtCore import QFileSystemWatcher

        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logging.warning("overlay: 配置目录不可创建，跳过指令通道: %s", config_dir)
            return
        watcher = QFileSystemWatcher(self)
        watcher.directoryChanged.connect(self._on_settings_command_dir_changed)
        if not watcher.addPath(str(config_dir)):
            logging.warning("overlay: 无法监视配置目录，指令通道退化为轮询: %s",
                            config_dir)
        self._command_watcher = watcher
        self._command_timer.setInterval(3000)
        self._command_timer.timeout.connect(self._consume_settings_command)
        self._command_timer.start()
        # 启动即消费：设置进程可能在主进程起来之前就写下了指令（保鲜窗口内有效）
        self._consume_settings_command()

    def _on_settings_command_dir_changed(self, _path: str) -> None:
        self._consume_settings_command()

    def _consume_settings_command(self) -> None:
        """消费一条设置进程指令（幂等；无指令 = 空转）。

        指令语义：``target`` 为空 = 退出全部子肥鱼（设置页「一键退出子肥鱼」）；
        否则只退那个 slot 身份（身份已不在活跃清单 = 空操作，只留日志）。
        """
        config_dir = self._spawn_config_dir()
        if not config_dir:
            return
        command = overlay_settings_command.consume_command(config_dir)
        if command is None:
            return
        if command.get("command") != overlay_settings_command.CMD_EXIT_SPAWNED_PETS:
            return  # consume 已按白名单过滤，这里只是防御
        target = command.get("target")
        if target is None:
            logging.info("overlay: 收到设置进程指令 → 退出全部子肥鱼")
            self.clear_spawned_pets()
            return
        sprite = next((item for item in self._spawned
                       if self._spawned_slots.get(item) == target), None)
        if sprite is None:
            logging.info("overlay: 指令目标 slot-%s 已不在活跃清单，跳过", target)
            return
        logging.info("overlay: 收到设置进程指令 → 退出子肥鱼 slot-%s", target)
        self.exit_pet(sprite)

    def _teardown_settings_command_watch(self) -> None:
        """释放指令 watcher 与轮询定时器（stop / aboutToQuit / 测试收口共用）。"""
        watcher = getattr(self, "_command_watcher", None)
        if watcher is not None:
            try:
                watcher.directoryChanged.disconnect(
                    self._on_settings_command_dir_changed)
            except (RuntimeError, TypeError):
                pass
            watcher.setParent(None)
            watcher.deleteLater()
            self._command_watcher = None
        timer = getattr(self, "_command_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass

    # ---------------------------------------------------------------- D13 逐 sprite 设置路由
    def sprite_instance_id(self, sprite) -> str:
        """sprite 的 config 身份：主 sprite = 主身份，子 sprite = 各自 slot 身份。

        D13：独立设置进程按 ``--instance`` 打开对应 ``config-slot-N.json``
        （app.py:1518-1523 的透传模式）；主身份与进程级 DSH_PET_INSTANCE 同值
        → 命令逐字不变。陌生 sprite（不在登记表）回退主身份，绝不瞎猜 slot。
        """
        main_id = str(getattr(self._config, "instance_id", "") or "")
        if sprite is None or sprite is self.sprite:
            return main_id
        slot = self._spawned_slots.get(sprite)
        if slot is None:
            return main_id
        return slot_manager.slot_to_instance_id(int(slot))

    def open_settings_for(self, sprite=None) -> bool:
        """按被点 sprite 的 config 身份打开设置页（D13 落点）。

        返回 True = 已交给独立设置进程（与 ``AppShell.open_settings_process``
        同义）。只认 AppShell 的 opener：拿不到（测试替身）或开关关闭 → False；
        overlay 拓扑没有 PetWindow 可挂 parent，故不做进程内回退。
        """
        opener = getattr(getattr(self._instance, "shell", None),
                         "open_settings_process", None)
        if not callable(opener):
            return False
        return bool(opener(_SettingsIdentity(self.sprite_instance_id(sprite))))

    def set_pet_visible(self, visible: bool) -> None:
        """显隐切换（app.py toggle_visible 等价）+ 岛状态同步 + 共享子系统 pause/resume。

        4.3 后半：显隐钩子上接主动识屏/联动监视器的 pause/resume
        （``window.py:1214-1250`` 语义）。隐藏时也收起气泡；恢复时把仍挂着的
        粘滞提醒重新挂上（legacy ``_resume_activity`` 同款）。
        """
        if visible:
            self.overlay.show()
            probe = getattr(self, "_probe", None)
            if probe is not None:
                probe.resume()
            self._restore_sticky_bubble()
            self._resume_shared_subsystems()
        else:
            self._hide_bubble_for_visibility()
            self.overlay.hide()
            probe = getattr(self, "_probe", None)
            if probe is not None:
                probe.pause()
            self._pause_shared_subsystems()
        island = getattr(getattr(self._instance, "shell", None), "island", None)
        if island is not None:
            try:
                island.set_pet_visible(bool(visible))
            except Exception:
                pass

    def _hide_bubble_for_visibility(self) -> None:
        """隐藏期收起气泡（气泡是置顶 Tool 窗，不随 overlay 隐藏）。"""
        follower = getattr(self, "_bubble_follower", None)
        if follower is not None:
            follower.hide()

    def _restore_sticky_bubble(self) -> None:
        """恢复显示：仍挂着的粘滞提醒重新挂上（legacy ``_resume_activity`` 同款）。"""
        if not self._sticky_bubble_active or not self._sticky_text:
            return
        if self._speech_bubble is None:
            return
        self._show_bubble_text(self._sticky_text, 0,
                              subtitle=self._sticky_subtitle, sticky=True,
                              buttons=self._sticky_buttons)

    def _toggle_pet_visible(self) -> None:
        self.set_pet_visible(not self.overlay.isVisible())

    def _on_collision_squash(self, event) -> None:
        """碰撞 Q 弹（旧权威冲量路径的 squash 语义）：真撞击量级时
        双方 sprite 各压一次。runtime_id 反查 sprite（成员少，线性即可）。"""
        if getattr(event, "j", 0.0) < float(self.collision.hit_min_dv):
            return
        from .sprite_collision import SpriteCollisionWorld
        for sprite in self.overlay.sprites:
            if SpriteCollisionWorld._member_id(sprite) in (event.a, event.b):
                squash = getattr(sprite, "squash", None)
                if callable(squash):
                    squash()

    def _on_collision_probe(self, event) -> None:
        """碰撞真撞击 → 边缘探头取消会话（旧机 collision_client 取消链语义）
        + throw_egg arm（探头被撞飞头部跟随速度）。"""
        if getattr(event, "j", 0.0) < float(self.collision.hit_min_dv):
            return
        probe = getattr(self, "_probe", None)
        egg = getattr(self, "_throw_egg", None)
        if probe is None and egg is None:
            return
        from .sprite_collision import SpriteCollisionWorld
        for sprite in self.overlay.sprites:
            if SpriteCollisionWorld._member_id(sprite) in (event.a, event.b):
                if probe is not None:
                    probe.on_sprite_collision_hit(sprite)
                if egg is not None:
                    # 必须在探头 cancel 之后：靠重进倒计时识别探头会话
                    egg.on_probe_collision_throw(sprite)

    def _say_feeding_bubble(self, files: int, folders: int,
                            total_bytes: int, stats: dict) -> None:
        """投喂气泡（file_eater._show_feedback 同文案格式）。"""
        from .file_eater import format_bytes
        if files and folders:
            batch = f"{files} 个文件、{folders} 个文件夹"
        elif files:
            batch = f"{files} 个文件"
        elif folders:
            batch = f"{folders} 个文件夹"
        else:
            batch = "空气"
        text = (
            f"啊呜～吃掉 {batch}（{format_bytes(total_bytes)}），"
            f"累计吃掉 {stats.get('file_count', 0)} 个文件、"
            f"{stats.get('folder_count', 0)} 个文件夹，"
            f"共 {format_bytes(stats.get('total_bytes', 0))}！"
        )
        self._bubble_follower.say(text, subtitle="放心，只是做个样子，文件没有删除或移动哦")

    def switch_character(self, character_id: str) -> None:
        """切换角色（4.1c）：换 per-pet 库 + 行为状态重置 + 配置持久化。

        per-pet 库是 T3 定论：新建库给主 sprite，旧库 shutdown 收尾 clip；
        行为状态机 forget 后下个 tick 自动接管 bind（防旧 clip 跨库残留）。
        """
        current = str(self._config.get("character", catalog.DEFAULT_CHARACTER))
        if not character_id or character_id == current:
            return
        egg = getattr(self, "_throw_egg", None)
        if egg is not None:
            egg.cancel_all("character_switch")  # 换角色即换素材，飞行会话兜底回正
        try:
            new_lib = self._instance._create_library(character_id)
        except Exception:
            logging.exception("overlay: 切换角色建库失败 %s", character_id)
            return
        self._config.set("character", character_id)
        save = getattr(self._config, "save", None)
        if callable(save):
            save()
        old_lib = self.lib
        self.lib = new_lib
        self.sprite.library = new_lib
        self.behavior.forget(self.sprite)
        shutdown = getattr(old_lib, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                logging.exception("overlay: 旧素材库收尾失败")

    # ---------------------------------------------------------------- 屏事件
    def _wire_screen_signals(self) -> None:
        self.app.screenAdded.connect(self.handle_screen_added)
        self.app.screenRemoved.connect(self.handle_screen_removed)
        self.app.primaryScreenChanged.connect(self.handle_primary_screen_changed)
        self._connect_screen(self._screen)

    def _connect_screen(self, screen) -> None:
        try:
            screen.geometryChanged.connect(self.handle_geometry_changed)
            screen.availableGeometryChanged.connect(self.handle_geometry_changed)
        except (AttributeError, TypeError):
            pass  # 测试假屏无 Qt 信号：几何同步由测试直调 handler

    def _disconnect_screen(self, screen) -> None:
        try:
            screen.geometryChanged.disconnect(self.handle_geometry_changed)
            screen.availableGeometryChanged.disconnect(self.handle_geometry_changed)
        except (AttributeError, TypeError, RuntimeError):
            pass

    def handle_geometry_changed(self, *args) -> None:
        """geometryChanged/availableGeometryChanged → 几何同步 + DPR 重喂。"""
        self._sync_geometry()
        self.sprite.set_dpr(float(self._screen.devicePixelRatio()))

    def handle_screen_added(self, screen) -> None:
        """屏插入：主屏切换由 primaryScreenChanged 负责；4.2a 位置持久化后
        才有"回保存屏"语义，本刀只观测记录。"""
        logging.info("overlay: screen added (%s)", _screen_name(screen))

    def handle_screen_removed(self, screen) -> None:
        """屏拔出：被拔的是当前屏 → 先收尾拖拽再按比例迁移到新主屏重建；
        非当前屏不动作（主屏切换由 primaryScreenChanged 负责）。"""
        logging.info("overlay: screen removed (%s)", _screen_name(screen))
        if screen is self._screen:
            self._end_drag()  # 拖拽中拔屏处置（v1.1 §10）
            self._migrate_to_screen(self.app.primaryScreen())

    def handle_primary_screen_changed(self, screen) -> None:
        logging.info("overlay: primary screen changed -> %s", _screen_name(screen))
        self._migrate_to_screen(screen)

    def _sync_geometry(self) -> None:
        """当前屏几何/可用区变化：overlay 几何 + 控制器边界 + sprite 按比例迁移。"""
        old_bounds = QRect(self._bounds)
        self.overlay.setGeometry(self._screen.geometry())
        new_bounds = self._local_bounds(self._screen)
        self._migrate_sprite_position(old_bounds, new_bounds)
        self._apply_bounds(new_bounds)

    def _apply_bounds(self, new_bounds: QRect) -> None:
        self._bounds = QRect(new_bounds)
        self.behavior.bounds = QRect(new_bounds)
        self.physics.set_bounds(new_bounds)
        # 主 sprite 与全部子肥鱼共用同一钳制域（4.2b：漏掉子肥鱼会让它们
        # 在几何变化后仍被旧屏边界钳制）
        for sprite in [self.sprite, *self._spawned]:
            sprite.set_bounds(QRect(new_bounds))
        probe = getattr(self, "_probe", None)
        if probe is not None:
            probe.set_bounds(QRect(new_bounds))
        egg = getattr(self, "_throw_egg", None)
        if egg is not None:
            egg.set_bounds(QRect(new_bounds))

    def _migrate_sprite_position(self, old_bounds: QRect, new_bounds: QRect,
                                 sprite=None) -> None:
        """rx/ry 语义迁移：sprite 中心相对可用区的比例在几何变化前后不变
        （口径同 window_placement.save_position 的持久化比例）。"""
        sprite = self.sprite if sprite is None else sprite
        if sprite is None:
            return
        if old_bounds.width() <= 0 or old_bounds.height() <= 0:
            return
        rect = sprite.rect()
        rx = (rect.x() + rect.width() / 2.0 - old_bounds.x()) / old_bounds.width()
        ry = (rect.y() + rect.height() / 2.0 - old_bounds.y()) / old_bounds.height()
        ncx = new_bounds.x() + rx * new_bounds.width()
        ncy = new_bounds.y() + ry * new_bounds.height()
        sprite.set_pos(QPointF(ncx - rect.width() / 2.0, ncy - rect.height() / 2.0))

    def _migrate_to_screen(self, new_screen) -> None:
        """overlay 重建到新屏（屏热插拔/主屏切换）：sprite 按比例迁移坐标。"""
        if new_screen is None or new_screen is self._screen:
            return
        old_bounds = QRect(self._bounds)
        old_overlay = self.overlay
        self._end_drag()
        self._disconnect_screen(self._screen)
        self._screen = new_screen
        new_bounds = self._local_bounds(new_screen)
        self.overlay = ShellOverlayWindow(new_screen, driver=self.driver)
        self.overlay.behavior = self.behavior
        self.overlay.edge_probe = getattr(self, "_probe", None)
        # 迁移后接线恢复（原 _build 挂在 overlay 上的能力一并重挂，否则
        # 屏迁移后点击音效/全量菜单/投喂/穿透回调/拖拽收尾全部静默丢失）
        self.overlay.click_feedback = self._sound.on_click
        # 4.1c 弹弓：controller 由 OverlayWindow 自持，这里只接 config
        # （slingshot_enabled 热读，设置页即改即生效）
        self.overlay.slingshot.config = self._config
        from .sprite_menu_facade import build_sprite_full_menu
        self.overlay._full_menu_builder = (
            lambda target: build_sprite_full_menu(self, target))
        self.overlay._through_changed = self._on_user_through_changed
        self.overlay._grab_finished_cb = self._on_grab_finished
        self.overlay.add_position_listener(self.sprite, self._on_main_sprite_moved)
        # 4.2b：子肥鱼随主 sprite 一起迁到新 overlay（否则拔屏/主屏切换后
        # 它们留在已关闭的旧 overlay 上，等于静默消失）
        for sprite in [self.sprite, *self._spawned]:
            old_overlay.remove_sprite(sprite, release_clip=False)  # 迁移保留 clip
            self.overlay.add_sprite(sprite)
            sprite.home_screen = new_screen
            sprite.set_dpr(float(new_screen.devicePixelRatio()))
            self._migrate_sprite_position(old_bounds, new_bounds, sprite)
        self._apply_bounds(new_bounds)
        self._apply_window_capabilities()  # on_top/穿透复合/监视器门重挂
        self._connect_screen(new_screen)
        was_started = self._started
        # M-2：驱动器为进程级共享（新 overlay 已在构造时挂上）——不再停旧表
        # 再起新表（那会在多 overlay 下停掉整组 tick）；旧 overlay 关闭即摘除。
        if getattr(self, "_feeding", None) is not None:
            self._bind_feeding()
        if getattr(self, "_bubble_follower", None) is not None:
            self._bind_bubble()
        bridge = getattr(self, "island_bridge", None)
        if bridge is not None:
            # 屏迁移：岛墙局部坐标按新 overlay 原点重算（气泡跟随器同款）
            bridge.set_origin(self.overlay.geometry().topLeft())
        if was_started:
            self.overlay.show()
            self.overlay.start()
        old_overlay.close()

    def _end_drag(self) -> None:
        """拖拽中拔屏/迁移：按当前位置松手收尾，清掉 overlay 的 grab 状态。"""
        grab = getattr(self.overlay, "_mouse_grab", None)
        if grab is None:
            return
        try:
            grab.on_release(QPointF(grab.pos))
        except Exception:
            logging.exception("overlay: 屏事件收尾拖拽失败")
        self.overlay._mouse_grab = None
        self.overlay._press_global = None

    # ---------------------------------------------------------------- 会话结束（issue #111 等价链）
    def _install_session_watcher(self) -> None:
        try:
            self._session_watcher = install_session_watcher(
                app=self.app, on_session_end=self._on_session_end)
        except Exception:
            logging.exception("overlay: 安装会话结束探测器失败")
            self._session_watcher = None

    def _on_session_end(self) -> None:
        """会话结束（关机/注销）：停止本路径全部 ffmpeg reader。

        spawn 闸门由 SessionWatcher 自身置位（先闸门后回调，顺序不可颠倒）；
        这里只做 watcher 不管的部分：停全部 clip + 停 tick。幂等。"""
        if self._session_end_done:
            return
        self._session_end_done = True
        stop_all = getattr(self.lib, "stop_all_clips", None)
        if callable(stop_all):
            try:
                stop_all()
            except Exception:
                logging.exception("overlay: 会话结束停止素材库 clip 失败")
        if self.overlay is not None:
            self.overlay.stop()
