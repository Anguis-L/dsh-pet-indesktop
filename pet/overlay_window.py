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
Phase 4 D2/HiDPI：DPR 由 overlay 统一按所在屏喂给**全部** sprite
（add_sprite/showEvent/屏事件），屏幕 DPR 变化（显示缩放、跨屏、几何变化）
即重喂并按新 DPR 重建——对齐旧路径 window.py:2129-2218 的信号驱动语义。
Phase 4 M-2：仿真推进（行为/碰撞/抛掷物理）与 tick 时钟、M-1 档位状态机整体
移交给统一驱动器 ``tick_driver.TickDriver``（T2：驱动器独立持有控制器，调
``world.tick(全部 overlays 的 sprites, dt)``）；overlay 只保留职责范围内的
「advance → 脏矩形 → 位置监听 fanout → update」段（``tick_advance``）与
绘制/命中/鼠标路由。``start``/``stop``/``_on_tick``/``_note_kinetic`` 等旧属性面
保留为转发（deprecation shim，见各自 docstring）。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QCursor, QPainter, QRegion, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from .sprite_menu import build_sprite_menu
from .tick_driver import TickDriver

if sys.platform == "win32":
    from .platform_win import WindowsPerPixelInputController, _set_windows_no_activate

# 命中阈值：alpha >= 16 视为不透明（与现架构 _is_transparent_at 同口径）
ALPHA_HIT_THRESHOLD = 16


class OverlayWindow(QWidget):
    """全屏透明合成窗：sprites 有序列表即 z-order（尾部最上）。"""

    def __init__(self, screen: QScreen | None = None, parent: QWidget | None = None,
                 *, driver: TickDriver | None = None) -> None:
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
        # sprite 移除通知（V-9）：行为控制器状态表等外部簿记的注销挂点
        self._sprite_removed_listeners: list = []
        # D2/P1：Qt 信号驱动 DPR 变化（QWindow.screenChanged + 所在屏
        # logical/physicalDotsPerInchChanged；Qt 6.11 无 devicePixelRatioChanged），
        # 与 geometryChanged 一起重喂 sprite。showEvent 接线，closeEvent 摘线。
        self._dpr_watch_window = None
        self._dpr_watch_screen = None

        # M-2 统一 tick 驱动器（T2）：仿真段 + tick 时钟 + M-1 档位全归驱动器
        # （tick_driver.TickDriver），overlay 只提供推进+重绘段（tick_advance）。
        # 未注入 driver 时自建私有驱动器——裸 overlay / demo 装配照旧可用；
        # 产品壳（overlay_shell）建一个进程级驱动器，挂全部屏的 overlay。
        self._driver = driver if driver is not None else TickDriver(self)
        self._driver.attach(self)
        # 旧属性面（deprecation）：overlay._timer 即驱动器时钟，供旧调用点
        # （测试同步驱动/间隔断言）沿用；新代码请走 tick_driver。
        self._timer = self._driver.timer

    @staticmethod
    def _tick_interval_ms(refresh_rate: float) -> int:
        """tick 间隔公式（V-6）：实现已随 M-2 迁到 TickDriver，本处保留旧入口
        （旧调用点/测试的静态引用面），语义逐位相同。"""
        return TickDriver.tick_interval_ms(refresh_rate)

    @property
    def screen(self) -> QScreen | None:
        """所在屏（只读）：M-2 后驱动器按它取刷新率（多 overlay 取最高者）。"""
        return self._screen

    @property
    def tick_driver(self) -> TickDriver:
        """统一 tick 驱动器（M-2）：持有控制器 / tick 时钟 / M-1 档位。"""
        return self._driver

    def _refresh_tick_interval(self) -> None:
        """重读屏幕刷新率并刷新 tick 间隔（V-6：电池 DRR 会在 170/60Hz
        间动态切换，构造时的一次性读数会变陈旧）；M-2 后委托驱动器。"""
        self._driver.refresh_tick_interval()

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        # attach 幂等：stop()（会 close/detach）后重启仍被驱动器驱动
        self._driver.attach(self)
        self._driver.start()

    def stop(self) -> None:
        self._driver.stop()

    # ---------------------------------------------------------------- M-1 闲置降档（挂点）
    def _note_kinetic(self) -> None:
        """运动/输入信号（M-1：升档同步立即，不等下一个 tick）。

        M-2 后档位状态机、四档降档与 QTimer 应用整体在 TickDriver；overlay
        只把事件侧信号转发进去——鼠标事件与 sprite 位移回调都走这里。
        """
        self._driver.note_kinetic()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_tick_interval()  # V-6：显示时重读刷新率（电池 DRR）
        self._feed_dpr()               # V-11：DPR 由 overlay 统一喂
        self._arm_dpr_change_watch()   # D2/P1：屏 DPR 变化 → 重喂 + 重建
        self._note_kinetic()           # M-1：可见即回 T0（从 T3 唤醒）
        if sys.platform == "win32" and self._input_controller is None:
            # 逐像素穿透：未命中任何 sprite 的屏幕区域点击直达下层应用——
            # 全屏 overlay 不抢占桌面交互（硬指标"体验不回退"的底线）。
            self._input_controller = WindowsPerPixelInputController(self)
            # 点击宠物不夺前台焦点（issue #98 语义沿用）。
            _set_windows_no_activate(int(self.winId()))

    def closeEvent(self, event) -> None:
        # V-12：关窗即停 tick（M-2：从驱动器摘除本 overlay；最后一个摘除时
        # 驱动器停表——多 overlay 场景其余成员继续被驱动）
        self._driver.detach(self)
        self._disarm_dpr_change_watch()  # D2/P1：摘线，与 showEvent arm 对称
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

        def _on_sprite_dirty(old, new):
            if old != new:
                # 位移 = 运动信号（M-1）：物理/拖拽/行为位移同步升档
                self._note_kinetic()
            else:
                # 纯帧通知：记录动画活性（"动画在播"判定的输入）
                self._driver.note_frame()
            self.update(QRegion(old) | QRegion(new))

        sprite._dirty_cb = _on_sprite_dirty
        sprite._kinetic_cb = self._note_kinetic  # set_velocity 直报（M-1）
        # V-11：DPR 由 overlay 统一按所在屏喂（此前靠调用方记得，demo 从没
        # 喂过）；取 QScreen 而非 widget 的 devicePixelRatioF——窗口未
        # realize 前后者不可信（offscreen/壳层构建期恒 1.0）
        set_dpr = getattr(sprite, "set_dpr", None)
        if callable(set_dpr):
            set_dpr(float(self._screen.devicePixelRatio()))
        if self.isVisible():
            self.update(QRegion(sprite.rect()))

    # ---------------------------------------------------------------- DPR 归一化（D2）
    def _feed_dpr(self) -> None:
        """把所在屏 DPR 喂给全部 sprite（add_sprite/showEvent/屏事件）。

        覆盖所有 sprite（不只主宠）：4.2b 生成的子 sprite 同样必须按屏 DPR
        渲染，否则 125%/150% 下只有主宠清晰。只在 DPR 真正变化时补一次
        全窗重绘（同值零开销——屏幕信号会重复上报）；变化时 sprite 内部已
        按新 DPR 重建 pixmap 与命中图（PetSprite.set_dpr → _invalidate_frames）。
        """
        dpr = float(self._screen.devicePixelRatio())
        changed = False
        for sprite in self.sprites:
            set_dpr = getattr(sprite, "set_dpr", None)
            if not callable(set_dpr):
                continue
            before = getattr(sprite, "dpr", None)
            set_dpr(dpr)
            if getattr(sprite, "dpr", None) != before:
                changed = True
        if changed:
            self.update()

    @staticmethod
    def _connect_screen_signal(screen, name: str, slot) -> None:
        """按名连接屏信号；假屏缺该信号/已销毁时静默跳过（测试假屏无 DPI 信号）。"""
        sig = getattr(screen, name, None)
        if sig is None:
            return
        try:
            sig.connect(slot)
        except (TypeError, RuntimeError):
            pass

    @staticmethod
    def _disconnect_screen_signal(screen, name: str, slot) -> None:
        """按名断开屏信号；未连接过/屏已销毁时静默跳过。"""
        sig = getattr(screen, name, None)
        if sig is None:
            return
        try:
            sig.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    def _wire_screen_dpi_signals(self, screen) -> None:
        """把所在屏的 DPR/几何变化信号挂到重喂；跨屏时换挂新屏。

        Qt 6.11 的 QScreen 没有 devicePixelRatioChanged：显示缩放变化由
        logical/physicalDotsPerInchChanged 上报；geometryChanged 覆盖分辨率/
        模式切换（可能连带 DPR 变化）。名字与 PetWindow 同族方法一致，
        能力断言按 sprite 侧等价物对照（tests/test_sprite_dpr.py）。
        """
        if screen is None:
            return
        old = self._dpr_watch_screen
        if old is screen:
            return
        if old is not None:
            self._disconnect_screen_signals(old)
        self._dpr_watch_screen = screen
        for name in ("logicalDotsPerInchChanged", "physicalDotsPerInchChanged"):
            self._connect_screen_signal(screen, name, self._on_screen_dpi_changed)
        for name in ("geometryChanged", "availableGeometryChanged"):
            self._connect_screen_signal(screen, name, self._on_screen_geometry_changed)

    def _disconnect_screen_signals(self, screen) -> None:
        for name in ("logicalDotsPerInchChanged", "physicalDotsPerInchChanged"):
            self._disconnect_screen_signal(screen, name, self._on_screen_dpi_changed)
        for name in ("geometryChanged", "availableGeometryChanged"):
            self._disconnect_screen_signal(screen, name, self._on_screen_geometry_changed)

    def _arm_dpr_change_watch(self) -> None:
        """接线屏 DPR 变化信号（showEvent 调用；幂等，QWindow 重建时重挂）。

        QWindow 不存在（壳层构建期/未 realize 的测试直调 showEvent）时只挂
        所在屏的 DPI/几何信号——窗口句柄拿到后再补挂 screenChanged。
        """
        win = self.windowHandle()
        old = self._dpr_watch_window
        if win is not None and win is not old:
            if old is not None:
                self._disconnect_screen_signal(old, "screenChanged",
                                               self._on_window_screen_changed)
            self._connect_screen_signal(win, "screenChanged",
                                        self._on_window_screen_changed)
            self._dpr_watch_window = win
        self._wire_screen_dpi_signals(self._screen)

    def _disarm_dpr_change_watch(self) -> None:
        """关闭窗口时摘除信号接线（与 showEvent 的 arm 对称）。"""
        old = self._dpr_watch_window
        if old is not None:
            self._disconnect_screen_signal(old, "screenChanged",
                                           self._on_window_screen_changed)
            self._dpr_watch_window = None
        old = self._dpr_watch_screen
        if old is not None:
            self._disconnect_screen_signals(old)
            self._dpr_watch_screen = None

    def _on_window_screen_changed(self, screen) -> None:
        """QWindow.screenChanged：跨屏 → 换挂新屏信号并按新 DPR 重喂。

        窗口不动时跨屏（副屏 DPI 配置不同）也走这条：新屏从此成为 DPR 与
        几何的取数源，sprite 立即按新 DPR 重建，不等 tick/重绘。
        """
        if screen is not None:
            self._screen = screen
            self._wire_screen_dpi_signals(screen)
            self._refresh_tick_interval()
        self._feed_dpr()
        self.update()

    def _on_screen_dpi_changed(self, *_args) -> None:
        """系统显示缩放变化（窗口未移动）→ 按新 DPR 重喂 + 重绘。"""
        self._feed_dpr()
        self.update()

    def _on_screen_geometry_changed(self, *_args) -> None:
        """所在屏几何/可用区变化：overlay 跟随屏几何 + 重读刷新率 + 重喂 DPR。

        分辨率/模式切换常连带 DPR 与刷新率变化；几何同步对未接壳层的裸
        overlay 必需（壳层路径另有 _sync_geometry，重复设置同值是 no-op）。
        """
        screen = self._screen
        if screen is not None:
            geo = screen.geometry()
            if self.geometry() != geo:
                self.setGeometry(geo)
        self._refresh_tick_interval()
        self._feed_dpr()
        self.update()

    def remove_sprite(self, sprite, *, release_clip: bool = True) -> None:
        """移除并按其矩形局部刷新（露出下层内容）。

        release_clip=True（默认，V-8）：同时释放 sprite 的 clip 所有权
        （disconnect+stop+清缓存），移除即停解码；屏迁移等保留 clip 的
        场景传 False。移除后通知 _sprite_removed_listeners（V-9：行为
        控制器状态表等外部簿记据此注销）。
        """
        if sprite not in self.sprites:
            return
        self.sprites.remove(sprite)
        self._position_listeners.pop(sprite, None)
        sprite._dirty_cb = None
        sprite._kinetic_cb = None
        if release_clip:
            close = getattr(sprite, "close", None)
            if callable(close):
                close()
        for cb in list(self._sprite_removed_listeners):
            cb(sprite)
        self.update(QRegion(sprite.rect()))

    def add_sprite_removed_listener(self, cb) -> None:
        """注册 sprite 移除通知（V-9 外部簿记注销挂点）；重复注册 no-op。"""
        if cb not in self._sprite_removed_listeners:
            self._sprite_removed_listeners.append(cb)

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
        """行为层钩子（**deprecation**）：仅由"未挂控制器"的驱动器调用。

        M-2 之前这是唯一的仿真段挂点（demo / Phase 2 装配把碰撞世界与物理
        挂在这里）；M-2 之后产品路径的仿真段由 ``TickDriver.tick_sim`` 持有
        三控制器完成（T2：驱动器独立，不挂在主 overlay 上），本钩子只在
        "驱动器未挂控制器"的兼容分支被调用——避免"驱动器 + 钩子"双份仿真。
        新装配请用 ``TickDriver.set_controllers(...)``；本方法保留一个发布
        周期供旧装配迁移，随 4.4 退役刀删除。
        """

    def tick_advance(self, dt: float) -> None:
        """推进+重绘段（M-2 拆分的下半段，驱动器每 tick 调用一次）。

        advance → 脏矩形 → 位置监听 fanout → update。V-3/V-4 语义不变：脏区
        取 advance 上报的旧|新 rect；帧到达直驱重绘另行由 sprite 的
        ``_dirty_cb``（见 add_sprite）保证，不依赖本段。
        """
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

    def _on_tick(self, dt: float | None = None) -> None:
        """兼容 shim（**deprecation**）：转发给驱动器的一次 tick。

        M-2 之前 tick 循环（dt 钳制 / M-1 档位 / 仿真段 / advance）在本类；
        拆分后循环归 ``TickDriver.on_tick``，本方法只转发，供旧调用点（demo、
        既有测试）同步驱动。新代码请用 ``overlay.tick_driver``（或直接
        ``driver.on_tick``）。
        """
        self._driver.on_tick(dt)

    # ---------------------------------------------------------------- 合成
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        region = event.region()
        for sprite in self.sprites:  # 列表序 = z-order：先画底层，尾部最上
            if region.intersects(sprite.rect()):
                sprite.paint(painter)
        painter.end()

    # ---------------------------------------------------------------- 命中与鼠标路由
    def set_mouse_through(self, on: bool) -> None:
        """用户手动穿透开关的统一直写点（菜单/集成层）；shell 可挂
        _through_changed 回调收编为"用户穿透 + 自动穿透"的复合语义。"""
        self.mouse_through = bool(on)
        cb = getattr(self, "_through_changed", None)
        if callable(cb):
            cb(self.mouse_through)

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
        self._check_stale_press()  # V-7：穿透轮询是恒开的，顺带兜底卡死的拖拽
        return self.sprite_at(local) is None

    def contextMenuEvent(self, event) -> None:
        """右键菜单（Phase 3a）：命中 sprite 弹最小集菜单；未命中忽略。"""
        self._note_kinetic()  # M-1：输入事件同步升档
        target = self.sprite_at(event.pos())
        if target is None:
            event.ignore()  # 未命中：不弹菜单（Windows 穿透轮询会把右键让给下层）
            return
        # behavior 由集成层持有（demo/未来 AppShell 挂在 overlay 上），基类不拥有
        menu = build_sprite_menu(self, target, behavior=getattr(self, "behavior", None))
        menu.exec(event.globalPos())
        event.accept()

    def mousePressEvent(self, event) -> None:
        self._note_kinetic()  # M-1：输入事件同步升档
        if event.button() != Qt.MouseButton.LeftButton:
            # V-13：左键拖拽中被其它键打断——先合成 release 收尾，否则
            # sprite 卡 drag 态（behavior 跳过、velocity 已清零）直到下次按下
            if self._mouse_grab is not None:
                self._finish_grab(event.position())
            # 只有左键进拖拽 grab——右键语义是弹菜单（contextMenuEvent），
            # 若进 grab，松手会被当成一次"原地放下"的拖拽（PetWindow 旧语义）
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
        self._note_kinetic()  # M-1：拖拽移动保持 T0
        # grab 期间事件直达被按住的 sprite（光标移出/落到别的 sprite 上不换手）
        self._mouse_grab.on_move(event.position())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._note_kinetic()  # M-1：松手（甩出判定）同步升档
        grab = self._mouse_grab
        self._finish_grab(event.position())
        if grab is None:
            event.ignore()
            return
        event.accept()

    def _finish_grab(self, position) -> None:
        """收尾当前拖拽 grab（release/打断/看门狗共用）。"""
        grab, self._mouse_grab = self._mouse_grab, None
        self._press_global = None
        if self._input_controller is not None:
            self._input_controller.set_drag_active(False)
        if grab is not None:
            grab.on_release(position)
        cb = getattr(self, "_grab_finished_cb", None)
        if callable(cb):
            cb()  # 4.1b：shell 冲刷光标恢复滞留等拖拽后状态

    def _check_stale_press(self) -> None:
        """V-7 拖拽看门狗：release 事件丢失（alt-tab/弹窗抢 grab/屏拔除）
        时 _press_global 卡死 → should_click_through 永假 → 全屏吞点击。
        穿透轮询与 tick 路径兜底：左键已不在按下态则强制收尾。"""
        if self._press_global is None:
            return
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            return
        self._finish_grab(QPointF(self.mapFromGlobal(QCursor.pos())))
