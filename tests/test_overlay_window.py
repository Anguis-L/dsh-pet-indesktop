# -*- coding: utf-8 -*-
"""Phase 1a 骨架 offscreen 单测（QT_QPA_PLATFORM=offscreen 可跑）。

覆盖：OverlayWindow 的 z-order/alpha 联合命中、脏矩形局部刷新（视觉无变化
不 update）、鼠标路由与 grab 转发；PetSprite 的帧签名缓存、镜像命中与
拖拽协议。全部用纯 QImage 假 sprite/假 clip，不依赖 webm 素材与 ffmpeg。
纪律：直接同步调用 _on_tick(dt=...) / 事件处理器，不启动真实 QTimer、
不固定 sleep 赌时序（AGENTS.md 时序测试纪律）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QRegion
from PySide6.QtWidgets import QApplication

from pet.overlay_window import ALPHA_HIT_THRESHOLD, OverlayWindow
from pet.pet_sprite import INTERACTION_DRAG, INTERACTION_NORMAL, PetSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 假 sprite / 假 clip
class FakeSprite:
    """纯 QImage 假 sprite：与 PetSprite 同一组协议方法，喂固定 alpha 图案。"""

    def __init__(self, pos, size, *, opaque=True, movable=False):
        self.pos = QPointF(*pos)
        self._size = size
        self.image = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)
        if opaque:
            self.image.fill(0xFF336699)
        self.velocity = QPointF(0, 0)
        self.movable = movable
        self.frame_dirty = False
        self.paint_calls = 0
        self.press_events: list[QPointF] = []
        self.move_events: list[QPointF] = []
        self.release_events: list[QPointF] = []

    def rect(self):
        return QRect(int(self.pos.x()), int(self.pos.y()), self._size[0], self._size[1])

    def alpha_at(self, local):
        x, y = int(local.x()), int(local.y())
        if 0 <= x < self._size[0] and 0 <= y < self._size[1]:
            return (self.image.pixel(x, y) >> 24) & 0xFF
        return 0

    def advance(self, dt):
        old = self.rect()
        if self.movable:
            self.pos += self.velocity * dt
        new = self.rect()
        if new != old or self.frame_dirty:
            self.frame_dirty = False
            return (old, new)
        return None

    def paint(self, painter: QPainter):
        self.paint_calls += 1

    def on_press(self, pos):
        self.press_events.append(QPointF(pos))

    def on_move(self, pos):
        self.move_events.append(QPointF(pos))

    def on_release(self, pos):
        self.release_events.append(QPointF(pos))


class FakeClip(QObject):
    """接口对齐 WebMClip 的假 clip：frameChanged 信号 + 固定 QImage 帧。"""

    frameChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.frame = 0
        self.image = QImage(640, 360, QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)
        self.started = False

    def currentFrameNumber(self):
        return self.frame

    def currentImage(self):
        return self.image

    def frameCount(self):
        return 1

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False


class FakeLibrary:
    def __init__(self, clip):
        self._clip = clip
        self.no_mirror: set[str] = set()

    def movie(self, name):
        return self._clip


class CountingOverlay(OverlayWindow):
    """记录 update 调用与脏矩形的测试 overlay。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.update_calls = 0
        self.update_regions: list[QRegion] = []

    def update(self, *args):
        self.update_calls += 1
        for a in args:
            if isinstance(a, QRegion):
                self.update_regions.append(QRegion(a))
        super().update(*args)


class CountingPetSprite(PetSprite):
    """统计帧重建次数的 PetSprite（验证签名缓存快路径）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.build_count = 0

    def _rebuild_pixmap(self):
        rebuilt = super()._rebuild_pixmap()
        if rebuilt:
            self.build_count += 1
        return rebuilt


def _mouse_event(etype, pos):
    return QMouseEvent(etype, QPointF(*pos), QPointF(*pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)


def _make_overlay():
    overlay = CountingOverlay()
    overlay.update_calls = 0
    overlay.update_regions.clear()
    return overlay


# ---------------------------------------------------------------- OverlayWindow
def test_tick_interval_from_refresh_rate():
    # >=75Hz 按刷新率取整（封顶 16ms）；<75Hz 或无读数固定 16ms（V-6：
    # 旧边界把 90Hz 错打成 16ms，90Hz 屏动画每 3 帧才交付一次）
    assert OverlayWindow._tick_interval_ms(170.0) == 6
    assert OverlayWindow._tick_interval_ms(144.0) == 7
    assert OverlayWindow._tick_interval_ms(90.0) == 11
    assert OverlayWindow._tick_interval_ms(60.0) == 16


def test_sprite_at_z_order_and_alpha_threshold():
    overlay = OverlayWindow()
    bottom = FakeSprite((0, 0), (64, 64))                    # 全不透明，垫底
    top = FakeSprite((0, 0), (64, 64), opaque=False)         # 顶层，默认全透明
    for x in range(8):
        for y in range(8):
            top.image.setPixel(x, y, 0xFF000000)
    overlay.add_sprite(bottom)
    overlay.add_sprite(top)

    assert overlay.sprite_at(QPoint(4, 4)) is top        # 顶层不透明处命中顶层
    assert overlay.sprite_at(QPoint(32, 32)) is bottom   # 顶层透明 → 穿透到底层
    assert overlay.sprite_at(QPoint(200, 200)) is None
    # 阈值边界：alpha < ALPHA_HIT_THRESHOLD 视为透明，>= 命中
    top.image.setPixel(16, 16, (ALPHA_HIT_THRESHOLD - 1) << 24)
    assert overlay.sprite_at(QPoint(16, 16)) is bottom
    top.image.setPixel(17, 17, ALPHA_HIT_THRESHOLD << 24)
    assert overlay.sprite_at(QPoint(17, 17)) is top


def test_dirty_region_only_when_visual_changes():
    overlay = _make_overlay()
    sprite = FakeSprite((10, 10), (40, 40))
    overlay.add_sprite(sprite)

    overlay._on_tick(dt=0.016)
    assert overlay.update_calls == 0                       # 视觉无变化：不 update

    sprite.frame_dirty = True                              # 原地换帧
    overlay._on_tick(dt=0.016)
    assert overlay.update_calls == 1
    assert overlay.update_regions[-1] == QRegion(sprite.rect())  # 旧矩形 == 新矩形

    sprite.frame_dirty = True
    sprite.velocity = QPointF(600, 0)                      # 换帧 + 位移
    sprite.movable = True
    old = sprite.rect()
    overlay._on_tick(dt=0.016)
    assert sprite.rect() != old
    assert overlay.update_regions[-1] == (QRegion(old) | QRegion(sprite.rect()))


def test_mouse_routing_hit_grab_and_miss():
    overlay = OverlayWindow()
    bottom = FakeSprite((0, 0), (100, 100))
    top = FakeSprite((20, 20), (100, 100))
    overlay.add_sprite(bottom)
    overlay.add_sprite(top)

    press = _mouse_event(QEvent.Type.MouseButtonPress, (50, 50))
    overlay.mousePressEvent(press)
    assert press.isAccepted()
    assert len(top.press_events) == 1 and not bottom.press_events

    # grab 期间事件直达 grabbed sprite——即使光标已移出它、落到别的 sprite 上
    move = _mouse_event(QEvent.Type.MouseMove, (30, 30))
    overlay.mouseMoveEvent(move)
    assert len(top.move_events) == 1 and not bottom.move_events

    release = _mouse_event(QEvent.Type.MouseButtonRelease, (30, 30))
    overlay.mouseReleaseEvent(release)
    assert len(top.release_events) == 1

    # 未命中任何 sprite：事件忽略（后续接穿透），sprite 不再收到
    miss = _mouse_event(QEvent.Type.MouseButtonPress, (400, 400))
    overlay.mousePressEvent(miss)
    assert not miss.isAccepted()
    assert len(top.press_events) == 1 and not bottom.press_events


# ---------------------------------------------------------------- F1：拖拽悬空动画接线
class PoolLibrary:
    """行为控制器可用的池协议假库（全不透明帧，便于逐像素命中）。"""

    def __init__(self, *, idles, drag=None):
        self.idles = list(idles)
        self.turns: list = []
        self.moves: list = []
        self.clicks: list = []
        self.acts: list = []
        self.drag = drag
        self.no_mirror: set[str] = set()
        self._clips = {}
        for name in self.idles + ([drag] if drag else []):
            clip = FakeClip()
            clip.image.fill(0xFF336699)
            self._clips[name] = clip

    def movie(self, name):
        return self._clips[name]

    def duration(self, name):
        return 1.0

    def clip(self, name):
        return self._clips[name]


def test_press_is_candidate_and_threshold_drag_binds_drag_clip():
    """M3 语义：按下只是点击候选（不绑 drag）；mouseMove 过 DRAG_THRESHOLD
    才升级真拖拽并绑悬空动画（旧 window.py:3169-3171/3192-3193）。

    基类 release 只做 grab 收尾（点击/拖拽判别在壳层），松手后控制器靠
    接管态自愈回待机——这条路径覆盖「看门狗收尾 / 壳层分支没走到」。
    """
    from pet.sprite_behavior import STATE_DRAG, STATE_IDLE, BehaviorController

    overlay = OverlayWindow()
    lib = PoolLibrary(idles=["idle"], drag="hang")
    sprite = PetSprite(lib, pos=QPointF(100, 100), scale=0.5)
    sprite.bind_clip("idle")
    sprite._rebuild_pixmap()
    sprite._clock = lambda: 1000.0                     # 假钟：松手判静止放下
    overlay.add_sprite(sprite)
    behavior = BehaviorController(QRect(0, 0, 1000, 1000))
    overlay.behavior = behavior

    overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (150, 150)))

    assert sprite.interaction_state != INTERACTION_DRAG   # 点击候选：不置态
    assert sprite._clip_name == "idle"                    # 不切悬空动画（M3 闪姿回归）
    assert behavior.state_of(sprite) != STATE_DRAG

    # 位移 100px ≫ DRAG_THRESHOLD×scale：move 升级真拖拽
    overlay.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, (250, 150)))

    assert sprite.interaction_state == INTERACTION_DRAG
    assert behavior.state_of(sprite) == STATE_DRAG
    assert sprite._clip_name == "hang"                 # 过阈值才绑悬空动画

    overlay.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, (250, 150)))
    assert sprite.interaction_state == INTERACTION_NORMAL

    behavior.tick([sprite], 0.016)                     # 接管结束自愈
    assert behavior.state_of(sprite) == STATE_IDLE
    assert sprite._clip_name == "idle"


def test_click_below_threshold_never_binds_drag_clip():
    """全程不超阈值 = 单击：sprite 不挪窝、不置拖拽态、不绑 drag clip。"""
    from pet.sprite_behavior import STATE_DRAG, BehaviorController

    overlay = OverlayWindow()
    lib = PoolLibrary(idles=["idle"], drag="hang")
    sprite = PetSprite(lib, pos=QPointF(100, 100), scale=0.5)
    sprite.bind_clip("idle")
    sprite._rebuild_pixmap()
    sprite._clock = lambda: 1000.0
    overlay.add_sprite(sprite)
    behavior = BehaviorController(QRect(0, 0, 1000, 1000))
    overlay.behavior = behavior

    overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (150, 150)))
    overlay.mouseMoveEvent(_mouse_event(QEvent.Type.MouseMove, (151, 151)))  # 1px
    overlay.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, (151, 151)))

    assert sprite.pos == QPointF(100, 100)             # 未跟随光标（点击候选不挪窝）
    assert sprite.interaction_state != INTERACTION_DRAG
    assert behavior.state_of(sprite) != STATE_DRAG
    assert sprite._clip_name == "idle"


def test_press_without_behavior_is_noop():
    """未挂 behavior 的裸 overlay（demo/旧装配）：按下不得抛异常。"""
    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (100, 100))
    overlay.add_sprite(sprite)
    overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (30, 30)))
    assert overlay._mouse_grab is sprite
    assert len(sprite.press_events) == 1
    overlay.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, (30, 30)))


# ---------------------------------------------------------------- PetSprite
def test_pet_sprite_frame_signature_cache():
    clip = FakeClip()
    library = FakeLibrary(clip)
    sprite = CountingPetSprite(library, scale=0.5)
    sprite.bind_clip("fake")
    assert clip.started                                    # bind 即起播

    sprite._rebuild_pixmap()
    sprite._rebuild_pixmap()
    assert sprite.build_count == 1                         # 签名不变：不重建

    clip.frame = 1
    sprite._rebuild_pixmap()
    assert sprite.build_count == 2                         # 帧号变化：重建
    sprite._rebuild_pixmap()
    assert sprite.build_count == 2

    sprite.facing = "right"
    sprite._rebuild_pixmap()
    assert sprite.build_count == 3                         # 朝向（镜像）变化：重建
    sprite._rebuild_pixmap()
    assert sprite.build_count == 3

    library.no_mirror.add("fake")
    sprite._rebuild_pixmap()
    assert sprite.build_count == 4                         # no_mirror 登记改变签名


def test_pet_sprite_mirror_and_alpha_hit():
    clip = FakeClip()                                      # 左半不透明、右半全透明
    for x in range(320):
        for y in range(360):
            clip.image.setPixel(x, y, 0xFF112233)
    sprite = PetSprite(FakeLibrary(clip), pos=QPointF(100, 100), scale=0.5)
    sprite.bind_clip("fake")
    sprite._rebuild_pixmap()

    w = sprite.rect().width()
    mid_y = sprite.rect().height() // 2
    assert sprite.rect().topLeft() == QPoint(100, 100)
    assert sprite.alpha_at(QPoint(5, mid_y)) >= ALPHA_HIT_THRESHOLD
    assert sprite.alpha_at(QPoint(w - 5, mid_y)) == 0

    sprite.facing = "right"                                # 不在 no_mirror：镜像
    sprite._rebuild_pixmap()
    assert sprite.alpha_at(QPoint(5, mid_y)) == 0          # 左缘变成原图右半
    assert sprite.alpha_at(QPoint(w - 5, mid_y)) >= ALPHA_HIT_THRESHOLD


def test_pet_sprite_advance_dirty_reporting():
    clip = FakeClip()
    sprite = PetSprite(FakeLibrary(clip), pos=QPointF(0, 0), scale=0.5)
    sprite.bind_clip("fake")
    sprite.advance(0.016)                                  # 吞掉 bind 后的首帧脏标记

    assert sprite.advance(0.016) is None                   # 静止 + 无换帧：None

    clip.frameChanged.emit(0)                              # 帧信号 → 上报脏矩形
    changed = sprite.advance(0.016)
    assert changed is not None
    assert changed[0] == changed[1]                        # 原地换帧：旧=新

    sprite.set_velocity(QPointF(600, 0))                   # 位移 → 旧|新两矩形
    old = sprite.rect()
    changed = sprite.advance(0.016)
    assert sprite.rect() != old
    assert changed == (old, sprite.rect())


def test_pet_sprite_drag_protocol():
    clip = FakeClip()
    sprite = PetSprite(FakeLibrary(clip), pos=QPointF(50, 50), scale=0.5)
    sprite.bind_clip("fake")
    sprite.set_velocity(QPointF(120, 0))
    # 固定假钟：轨迹样本时间戳相同 → 初速不可估算 → 静止放下，
    # 不依赖真实时间流逝（时序测试纪律）
    sprite._clock = lambda: 1000.0

    sprite.on_press(QPointF(60, 60))                       # grab 偏移 (10, 10)
    assert not sprite.dragging                             # 点击候选期不置拖拽态（M3）
    sprite.begin_drag()                                    # 过阈值升级真拖拽
    assert sprite.dragging
    assert sprite.interaction_state == INTERACTION_DRAG
    assert sprite.velocity == QPointF(0, 0)                # 拖拽期间 velocity 停

    sprite.on_move(QPointF(200, 210))
    assert sprite.pos == QPointF(190, 200)                 # 跟随光标 - grab 偏移
    sprite.advance(0.016)                                  # 拖拽中 velocity 不积分
    assert sprite.pos == QPointF(190, 200)

    sprite.on_release(QPointF(200, 210))
    assert not sprite.dragging
    assert sprite.interaction_state == INTERACTION_NORMAL  # 低速松手：原地放下
    assert sprite.velocity == QPointF(0, 0)                # 不再恢复拖拽前速度


# ---------------------------------------------------------------- Windows 逐像素穿透接线
class FakeInputController:
    """记录 set_drag_active 调用的假穿透控制器。"""

    def __init__(self):
        self.drag_states: list[bool] = []

    def set_drag_active(self, active):
        self.drag_states.append(bool(active))


def test_is_transparent_at_delegates_union_hit():
    overlay = OverlayWindow()
    sprite = FakeSprite((50, 50), (100, 100))
    overlay.add_sprite(sprite)
    assert overlay._is_transparent_at(QPoint(10, 10)) is True      # 无 sprite：穿透
    assert overlay._is_transparent_at(QPoint(60, 60)) is False     # 命中 sprite：不穿透


def test_press_global_and_drag_polling_protocol():
    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (100, 100))
    overlay.add_sprite(sprite)
    fake_ctl = FakeInputController()
    overlay._input_controller = fake_ctl

    assert overlay.mouse_through is False
    assert overlay._press_global is None

    overlay.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, (30, 30)))
    assert overlay._press_global is not None          # 拖拽中：穿透轮询据此强制不穿透
    assert fake_ctl.drag_states == [True]             # 拖拽期轮询降频

    overlay.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, (30, 30)))
    assert overlay._press_global is None
    assert fake_ctl.drag_states == [True, False]      # 松手恢复 10ms 轮询


def test_windows_per_pixel_controller_integration():
    import sys

    if sys.platform != "win32":
        import pytest

        pytest.skip("WindowsPerPixelInputController 仅 Windows")
    from pet.platform_win import WindowsPerPixelInputController

    overlay = OverlayWindow()
    sprite = FakeSprite((100, 100), (100, 100))
    overlay.add_sprite(sprite)
    overlay.show()                                    # showEvent 创建真控制器
    assert isinstance(overlay._input_controller, WindowsPerPixelInputController)
    ctl = overlay._input_controller

    # 光标落在 sprite 外：穿透；落在 sprite 不透明像素上：恢复接收
    assert ctl.should_click_through(QPoint(10, 10)) is True
    assert ctl.should_click_through(QPoint(120, 120)) is False
    # 用户手动穿透开关：恒穿透
    overlay.mouse_through = True
    assert ctl.should_click_through(QPoint(120, 120)) is True
    overlay.mouse_through = False
    # 拖拽中：恒不穿透（事件必须持续送达）
    overlay._press_global = QPoint(120, 120)
    assert ctl.should_click_through(QPoint(10, 10)) is False
    overlay._press_global = None
    overlay.close()
    assert overlay._input_controller is None          # closeEvent 复位样式并停轮询
