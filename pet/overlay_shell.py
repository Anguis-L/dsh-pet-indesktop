# -*- coding: utf-8 -*-
"""Phase 4.1a 产品壳：overlay 拓扑（单合成窗）的 AppShell 挂载层。

设计稿：.scratch/single-overlay-window/PHASE4_DESIGN.md（T1/T4/T5 + §5 4.1a 行
+ v1.1 §10 屏热插拔/会话事件升级项）。本模块只做骨架与生命周期：

- 主屏 OverlayWindow + 主 sprite（MovieLibrary 经主 PetInstance._create_library
  创建——per-pet 库是 T3 定论，禁止跨 sprite 共享 clip 对象）；
- 进程级一组三控制器（BehaviorController/SpriteCollisionWorld/
  ThrowPhysicsController），tick 顺序协议：行为 → 碰撞 → 抛掷物理
  （挂 OverlayWindow.before_sprites_advance，接线口径同
  .scratch/single-overlay-window/run_overlay_demo.py）；
- 屏事件：screenAdded/screenRemoved/primaryScreenChanged/geometryChanged →
  overlay 重建或几何同步，sprite 位置按 rx/ry（中心相对可用区比例）语义
  迁移；主屏 DPR 变化重喂 sprite.set_dpr；拖拽中拔屏先收尾拖拽再迁移；
- 会话结束（Windows 关机/注销）：复用 session_watcher 的闸门与信号链，
  收口本路径 MovieLibrary 的全部 clip（issue #111 等价链，D10 落点）。

T5 切换策略：PET_RENDER_TOPOLOGY=overlay 环境变量是开发期一次性分流 flag，
读取收口在本模块 is_overlay_topology()，不进 Config/设置页/schema。
4.1a 明确不做：多 sprite（4.2）、位置持久化（4.2a，本刀恒走默认右下角）、
岛/气泡/聊天/菜单全量 parity（4.1b/4.1c）、捕获模式切换（T4 后续）。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, QPointF, QRect
from PySide6.QtWidgets import QMenu, QStyle, QSystemTrayIcon

from . import catalog
from .overlay_window import OverlayWindow
from .pet_sprite import PetSprite
from .session_watcher import install_session_watcher
from .sprite_behavior import BehaviorController
from .sprite_collision import SpriteCollisionWorld
from .sprite_physics import ThrowPhysicsController

ENV_TOPOLOGY = "PET_RENDER_TOPOLOGY"
TOPOLOGY_OVERLAY = "overlay"


def is_overlay_topology() -> bool:
    """唯一 env 读取点（T5：dev flag，不进 Config/设置页/schema）。"""
    return os.environ.get(ENV_TOPOLOGY, "").strip().lower() == TOPOLOGY_OVERLAY


def _screen_name(screen) -> str:
    try:
        return str(screen.name())
    except Exception:
        return "?"


class ShellOverlayWindow(OverlayWindow):
    """4.1a 产品 overlay：三控制器 tick 钩子 + 单击/拖拽区分。

    before_sprites_advance 转发给 OverlayShell 注入的回调（tick 顺序协议
    在壳层固化，子类不持有控制器）；behavior 属性由壳层挂载，
    contextMenuEvent（基类）查表读它。点击 vs 拖拽的阈值判定口径同
    run_overlay_demo.py：按下位移小于 DRAG_THRESHOLD*scale 视为单击。
    """

    def __init__(self, screen, on_advance) -> None:
        super().__init__(screen=screen)
        self._on_advance = on_advance
        self._press_pos = None
        self.behavior = None  # OverlayShell 挂载；contextMenuEvent 查表读它

    def before_sprites_advance(self, dt: float) -> None:
        self._on_advance(dt)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._press_pos = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        grab = self._mouse_grab
        press, self._press_pos = self._press_pos, None
        super().mouseReleaseEvent(event)
        if grab is None or press is None or self.behavior is None:
            return
        threshold = catalog.DRAG_THRESHOLD * getattr(grab, "scale", 1.0)
        if (event.position() - press).manhattanLength() < threshold:
            self.behavior.on_sprite_clicked(grab)


class OverlayShell(QObject):
    """overlay 拓扑产品壳：主屏 overlay + 主 sprite + 进程级三控制器。

    依赖最小化：QApplication + 主 PetInstance（config 与 per-pet MovieLibrary
    身份源）。屏事件/会话事件/退出收口全部在本类内自接，AppShell 只在
    拓扑分支处构造并 start()。
    """

    def __init__(self, app, instance, *, screen=None, sprite_factory=None) -> None:
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
        self.overlay: ShellOverlayWindow | None = None
        self.sprite = None
        self.lib = None
        self.tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._session_watcher = None
        self._bounds = QRect()
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
        self.overlay = ShellOverlayWindow(self._screen, self._advance_controllers)
        self.overlay.behavior = self.behavior
        self.lib = self._create_main_library()
        scale = float(self._config.get("scale") or catalog.DEFAULT_SCALE)
        self.sprite = self._sprite_factory(self.lib, QPointF(0, 0), scale)
        self.sprite.home_screen = self._screen
        self.sprite.set_dpr(float(self._screen.devicePixelRatio()))
        self.sprite.set_bounds(QRect(self._bounds))
        self.overlay.add_sprite(self.sprite)
        # 位置持久化是 4.2a 的事：本刀恒按 go_default_corner 语义落右下角
        self.sprite.set_pos(self._default_corner_pos(self._bounds, self.sprite.rect()))
        self._build_tray()

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

    # ---------------------------------------------------------------- tick 顺序协议
    def _advance_controllers(self, dt: float) -> None:
        """tick 顺序协议：行为 → 碰撞 → 抛掷物理（集成约定，固化成测试）。"""
        sprites = self.overlay.sprites
        self.behavior.tick(sprites, dt)
        self.collision.tick(sprites, dt)
        self.physics.tick(sprites, dt)

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.overlay.show()
        self.overlay.start()
        if self.tray is not None:
            self.tray.show()

    def stop(self) -> None:
        """幂等：重复 stop 是 no-op。"""
        if not self._started:
            return
        self._started = False
        self.overlay.stop()
        self.overlay.close()
        if self.tray is not None:
            self.tray.hide()

    def _on_about_to_quit(self) -> None:
        """退出收口：停 tick + 暂停预热（口径同旧路径 _on_about_to_quit 的
        窗级项；位置持久化 4.2a 才接入）。"""
        if self.overlay is not None:
            self.overlay.stop()
        pause = getattr(self.lib, "pause_warm", None)
        if callable(pause):
            try:
                pause()
            except Exception:
                logging.exception("overlay: 退出时暂停预热失败")

    # ---------------------------------------------------------------- 托盘（最小集）
    def _build_tray(self) -> None:
        """最小托盘：退出入口（右键 sprite 菜单的 退出 之外的保底路径）。
        全量托盘菜单 parity 属 4.1b/4.1c。"""
        try:
            tray = QSystemTrayIcon(self._tray_icon(), self)
            menu = QMenu()
            menu.addAction("退出", self.app.quit)
            tray.setContextMenu(menu)
            tray.setToolTip("dsh-pet (overlay)")
            # 菜单本体强引用保活（app.py F5 教训：PySide6 wrapper 回收后
            # contextMenu 会命中失效 wrapper）
            self._tray_menu = menu
            self.tray = tray
        except Exception:
            logging.exception("overlay: 创建托盘失败")
            self.tray = None

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
        self.sprite.set_bounds(QRect(new_bounds))

    def _migrate_sprite_position(self, old_bounds: QRect, new_bounds: QRect) -> None:
        """rx/ry 语义迁移：sprite 中心相对可用区的比例在几何变化前后不变
        （口径同 window_placement.save_position 的持久化比例）。"""
        if self.sprite is None:
            return
        if old_bounds.width() <= 0 or old_bounds.height() <= 0:
            return
        rect = self.sprite.rect()
        rx = (rect.x() + rect.width() / 2.0 - old_bounds.x()) / old_bounds.width()
        ry = (rect.y() + rect.height() / 2.0 - old_bounds.y()) / old_bounds.height()
        ncx = new_bounds.x() + rx * new_bounds.width()
        ncy = new_bounds.y() + ry * new_bounds.height()
        self.sprite.set_pos(QPointF(ncx - rect.width() / 2.0, ncy - rect.height() / 2.0))

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
        self.overlay = ShellOverlayWindow(new_screen, self._advance_controllers)
        self.overlay.behavior = self.behavior
        old_overlay.remove_sprite(self.sprite)
        self.overlay.add_sprite(self.sprite)
        self.sprite.home_screen = new_screen
        self.sprite.set_dpr(float(new_screen.devicePixelRatio()))
        self._migrate_sprite_position(old_bounds, new_bounds)
        self._apply_bounds(new_bounds)
        self._connect_screen(new_screen)
        was_started = self._started
        old_overlay.stop()
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
