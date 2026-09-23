# -*- coding: utf-8 -*-
"""单合成窗 overlay（Phase 1a 骨架）：一个全屏透明窗口承载全部宠物 sprite。

架构背景见 .scratch/single-overlay-window/spec.md：「每宠一个顶层窗口」在
Windows 合成器上是结构性税负（move() 1.5-4.3ms/次 + DWM 多窗竞争）；单
overlay 内合成后，宠物移动 = 改 sprite 坐标 + update(脏矩形)，完全不经过
WM/DWM 的窗口移动路径。Phase 0 实测（同目录 probe_report）该路线 GO。

本阶段（1b/2）：Windows 逐像素穿透已接通（复用 platform_win 的
WS_EX_TRANSPARENT 轮询 + 联合命中，见 showEvent）；非 Windows 联合
QRegion setMask 与多屏几何留待后续（spec 开放问题 Q1）。
Phase 3a：右键菜单最小集（contextMenuEvent → sprite_menu.build_sprite_menu）；
鼠标路由只让左键进拖拽 grab，右键只弹菜单（PetWindow 旧语义）。
Phase 3b：sprite 位置监听（add_position_listener）——sprite 不产生 moveEvent，
气泡/聊天窗等外围小窗的跟随源平移为 tick 后 rect 变化 fanout（spec「Phase 3
设计」3b 节）。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QElapsedTimer, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QPainter, QRegion, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from .sprite_menu import build_sprite_menu

if sys.platform == "win32":
    from .platform_win import WindowsPerPixelInputController, _set_windows_no_activate

# 命中阈值：alpha >= 16 视为不透明（与现架构 _is_transparent_at 同口径）
ALPHA_HIT_THRESHOLD = 16


class OverlayWindow(QWidget):
    """全屏透明合成窗：sprites 有序列表即 z-order（尾部最上）。"""

    def __init__(self, screen: QScreen | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        # Phase 1a 暂不做多屏：默认主屏，调用方可注入别的 QScreen。
        self._screen = screen or QApplication.primaryScreen()
        self.setGeometry(self._screen.geometry())

        self.sprites: list = []
        self._mouse_grab = None
        # sprite 位置监听（Phase 3b）：sprite -> [cb]，tick 推进后 rect 发生
        # 变化的 sprite 触发其 listeners（cb(sprite)）；外围小窗（气泡等）的
        # 跟随源——sprite 不是窗口，没有 moveEvent 可挂。
        self._position_listeners: dict = {}
        # Windows 逐像素穿透协议字段（WindowsPerPixelInputController 直接复用，
        # 见 showEvent）：mouse_through = 用户手动"鼠标穿透"开关（恒穿透）；
        # _press_global 非 None = 拖拽中（穿透轮询据此降频并强制不穿透）。
        # 非 Windows 平台的联合 QRegion setMask 穿透为后续阶段。
        self.mouse_through = False
        self._press_global: QPoint | None = None
        self._input_controller = None

        self._elapsed = QElapsedTimer()
        self._tick_count = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self._tick_interval_ms(self._screen.refreshRate()))
        self._timer.timeout.connect(self._on_tick)

    @staticmethod
    def _tick_interval_ms(refresh_rate: float) -> int:
        """tick 间隔：高刷屏（>90Hz）按刷新率取整（170Hz→6ms），普通屏 16ms。"""
        if refresh_rate > 90.0:
            return max(1, round(1000.0 / refresh_rate))
        return 16

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self._elapsed.start()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32" and self._input_controller is None:
            # 逐像素穿透：未命中任何 sprite 的屏幕区域点击直达下层应用——
            # 全屏 overlay 不抢占桌面交互（硬指标"体验不回退"的底线）。
            self._input_controller = WindowsPerPixelInputController(self)
            # 点击宠物不夺前台焦点（issue #98 语义沿用）。
            _set_windows_no_activate(int(self.winId()))

    def closeEvent(self, event) -> None:
        if self._input_controller is not None:
            self._input_controller.stop()
            self._input_controller = None
        super().closeEvent(event)

    # ---------------------------------------------------------------- sprite 管理
    def add_sprite(self, sprite) -> None:
        """追加到 z-order 尾部（最上）；重复添加是 no-op。

        同时挂接 sprite 的脏上报回调（V-3/V-4）：set_pos 位移与帧到达
        直驱 update，不经 tick——物理/拖拽移动的旧位置即时清除，tick
        降档后动画帧率不随之掉。
        """
        if sprite in self.sprites:
            return
        self.sprites.append(sprite)
        sprite._dirty_cb = lambda old, new: self.update(QRegion(old) | QRegion(new))
        if self.isVisible():
            self.update(QRegion(sprite.rect()))

    def remove_sprite(self, sprite) -> None:
        """移除并按其矩形局部刷新（露出下层内容）。"""
        if sprite not in self.sprites:
            return
        self.sprites.remove(sprite)
        self._position_listeners.pop(sprite, None)
        sprite._dirty_cb = None
        self.update(QRegion(sprite.rect()))

    # ---------------------------------------------------------------- 位置监听（Phase 3b）
    def add_position_listener(self, sprite, cb) -> None:
        """注册 sprite 位置监听：tick 推进后 rect 变化时回调 cb(sprite)。

        只在 rect 真正变化时触发——advance 返回 None（视觉无变化）或仅帧
        内容变化（rect 未动）都不触发，跟随方不会被无位移的动画帧白唤。
        重复注册同一 cb 是 no-op。
        """
        listeners = self._position_listeners.setdefault(sprite, [])
        if cb not in listeners:
            listeners.append(cb)

    def remove_position_listener(self, sprite, cb) -> None:
        """注销位置监听；未注册过是 no-op。"""
        listeners = self._position_listeners.get(sprite)
        if not listeners:
            return
        if cb in listeners:
            listeners.remove(cb)
        if not listeners:
            self._position_listeners.pop(sprite, None)

    # ---------------------------------------------------------------- 统一 tick
    def before_sprites_advance(self, dt: float) -> None:
        """行为层钩子（默认空实现）：每个 tick 在 sprite.advance 之前调用。

        Phase 2 的进程内碰撞世界、demo 的边界反弹/碰撞物理挂在这里——
        钩子只改各 sprite 的 velocity/pos，位置积分仍由 advance 统一完成。
        """

    def _on_tick(self, dt: float | None = None) -> None:
        if dt is None:
            # 首 tick 没有已流逝时间，按定时器间隔计；后续按实测流逝，封顶
            # 50ms 防卡顿后位置跳变（语义同 Phase 0 探针）。
            if self._tick_count:
                dt = min(0.05, self._elapsed.nsecsElapsed() / 1e9)
            else:
                dt = self._timer.interval() / 1000.0
            self._elapsed.restart()
        self._tick_count += 1
        self.before_sprites_advance(dt)
        dirty = QRegion()
        moved = []
        for sprite in list(self.sprites):
            changed = sprite.advance(dt)
            if changed is not None:
                old, new = changed
                dirty |= QRegion(old) | QRegion(new)
                if old != new and sprite in self._position_listeners:
                    moved.append(sprite)
        # 位置监听 fanout 放在整轮 advance 之后：tick 内所有控制器已写完
        # 位置，跟随方读到的是本 tick 的最终 rect
        for sprite in moved:
            for cb in list(self._position_listeners.get(sprite, [])):
                cb(sprite)
        if not dirty.isEmpty():
            self.update(dirty)

    # ---------------------------------------------------------------- 合成
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        region = event.region()
        for sprite in self.sprites:  # 列表序 = z-order：先画底层，尾部最上
            if region.intersects(sprite.rect()):
                sprite.paint(painter)
        painter.end()

    # ---------------------------------------------------------------- 命中与鼠标路由
    def sprite_at(self, local_pos: QPoint | QPointF):
        """逐像素联合命中：z-order 顶层往下，矩形粗筛 + alpha 细判。

        参数为 overlay 局部坐标（Phase 1a 单屏：= 屏幕物理坐标 - overlay
        原点）。Windows 穿透轮询与鼠标路由共用此判据。
        """
        x, y = int(local_pos.x()), int(local_pos.y())
        for sprite in reversed(self.sprites):
            rect = sprite.rect()
            if not rect.contains(x, y):
                continue
            if sprite.alpha_at(QPoint(x - rect.x(), y - rect.y())) >= ALPHA_HIT_THRESHOLD:
                return sprite
        return None

    def _is_transparent_at(self, local: QPoint | QPointF) -> bool:
        """逐像素联合穿透判据（WindowsPerPixelInputController 协议方法）：
        光标处没有任何 sprite 的不透明像素 = 该点穿透到下层应用。"""
        return self.sprite_at(local) is None

    def contextMenuEvent(self, event) -> None:
        """右键菜单（Phase 3a）：命中 sprite 弹最小集菜单；未命中忽略。"""
        target = self.sprite_at(event.pos())
        if target is None:
            event.ignore()  # 未命中：不弹菜单（Windows 穿透轮询会把右键让给下层）
            return
        # behavior 由集成层持有（demo/未来 AppShell 挂在 overlay 上），基类不拥有
        menu = build_sprite_menu(self, target, behavior=getattr(self, "behavior", None))
        menu.exec(event.globalPos())
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            # 只有左键进拖拽 grab——右键语义是弹菜单（contextMenuEvent），
            # 若进 grab，松手会被当成一次"原地放下"的拖拽（PetWindow 旧语义）
            self._mouse_grab = None
            event.ignore()
            return
        target = self.sprite_at(event.position())
        if target is None:
            self._mouse_grab = None
            event.ignore()  # 未命中：忽略（Windows 穿透轮询会把点击让给下层）
            return
        self._mouse_grab = target
        self._press_global = event.globalPosition().toPoint()
        if self._input_controller is not None:
            self._input_controller.set_drag_active(True)
        target.on_press(event.position())
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._mouse_grab is None:
            event.ignore()
            return
        # grab 期间事件直达被按住的 sprite（光标移出/落到别的 sprite 上不换手）
        self._mouse_grab.on_move(event.position())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        grab, self._mouse_grab = self._mouse_grab, None
        self._press_global = None
        if self._input_controller is not None:
            self._input_controller.set_drag_active(False)
        if grab is None:
            event.ignore()
            return
        grab.on_release(event.position())
        event.accept()
