# -*- coding: utf-8 -*-
"""V-5~V-15 overlay/sprite 硬化回归（QT_QPA_PLATFORM=offscreen 可跑）。

覆盖（REVIEW_VERDICT.md 小修批）：
- V-6  tick 间隔公式（90Hz 边界）与刷新率重读入口；
- V-7  _press_global 丢失 release 的看门狗（全屏吞点击兜底）；
- V-8  remove_sprite 释放 clip（release_clip=False 保留）+ 移除通知；
- V-9  _cats_cache 弱键（库销毁自动回收）；
- V-10 scale property（置脏/重钳/上报）；
- V-12 closeEvent 停 tick timer；
- V-13 拖拽中被非左键打断 → 合成 release 收尾。

纪律（AGENTS.md 时序测试）：同步直调 handler，不 sleep 赌时序；
素材用纯 QImage 假 clip。
"""
from __future__ import annotations

import gc
import os
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QCloseEvent, QImage, QMouseEvent, QRegion
from PySide6.QtWidgets import QApplication

from pet.overlay_window import OverlayWindow
from pet.pet_sprite import PetSprite
from pet.sprite_behavior import BehaviorController

app = QApplication.instance() or QApplication([])


class FakeClip(QObject):
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
    def __init__(self, clip=None):
        self._clip = clip or FakeClip()
        self.no_mirror: set[str] = set()
        self.idles = ["idle"]
        self.turns: list = []
        self.moves: list = []
        self.clicks: list = []

    def movie(self, name):
        return self._clip


def _make_sprite(pos=QPointF(100, 100), scale=0.5):
    clip = FakeClip()
    sprite = PetSprite(FakeLibrary(clip), pos=pos, scale=scale)
    sprite.bind_clip("idle")
    return sprite, clip


# ---------------------------------------------------------------- V-6
def test_tick_interval_boundaries():
    assert OverlayWindow._tick_interval_ms(170.0) == 6
    assert OverlayWindow._tick_interval_ms(144.0) == 7
    assert OverlayWindow._tick_interval_ms(90.0) == 11   # 旧边界错打成 16
    assert OverlayWindow._tick_interval_ms(75.0) == 13
    assert OverlayWindow._tick_interval_ms(60.0) == 16
    assert OverlayWindow._tick_interval_ms(0.0) == 16


def test_show_event_rereads_refresh_rate():
    from PySide6.QtGui import QShowEvent

    overlay = OverlayWindow()
    overlay._timer.setInterval(999)
    overlay.showEvent(QShowEvent())
    assert overlay._timer.interval() != 999


# ---------------------------------------------------------------- V-7
class FakeGrab:
    def __init__(self):
        self.released = False

    def on_release(self, _pos):
        self.released = True


def test_stale_press_watchdog_clears_dead_grab():
    overlay = OverlayWindow()
    grab = FakeGrab()
    overlay._mouse_grab = grab
    overlay._press_global = QPoint(5, 5)
    # 测试环境无真实按键：mouseButtons() 必为 NoButton → 看门狗应收尾
    assert not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton)
    overlay._check_stale_press()
    assert overlay._press_global is None
    assert overlay._mouse_grab is None
    assert grab.released is True


def test_stale_press_watchdog_noop_when_idle():
    overlay = OverlayWindow()
    overlay._check_stale_press()  # 无 press：不得抛异常
    assert overlay._press_global is None


# ---------------------------------------------------------------- V-8
def test_remove_sprite_releases_clip_and_notifies():
    overlay = OverlayWindow()
    sprite, clip = _make_sprite()
    overlay.add_sprite(sprite)
    assert clip.started is True
    removed = []
    overlay.add_sprite_removed_listener(removed.append)

    overlay.remove_sprite(sprite)

    assert clip.started is False      # 移除即停解码
    assert removed == [sprite]        # V-9 移除通知
    assert sprite._dirty_cb is None


def test_remove_sprite_keep_clip_for_migration():
    overlay = OverlayWindow()
    sprite, clip = _make_sprite()
    overlay.add_sprite(sprite)

    overlay.remove_sprite(sprite, release_clip=False)

    assert clip.started is True       # 屏迁移：clip 保留
    assert sprite._clip is not None


# ---------------------------------------------------------------- V-9
def test_cats_cache_weak_key_evicts_on_lib_gc():
    c = BehaviorController(QRect(0, 0, 800, 600))
    lib = FakeLibrary()
    cats = c._categories(lib)
    assert cats["idles"] == ["idle"]
    assert len(c._cats_cache) == 1

    ref = weakref.ref(lib)
    del lib
    gc.collect()

    assert ref() is None
    assert len(c._cats_cache) == 0    # 库销毁 → 缓存自动回收（无地址复用误判）


# ---------------------------------------------------------------- V-10
def test_scale_setter_marks_dirty_and_reclamps():
    overlay = OverlayWindow()
    sprite, _clip = _make_sprite()
    sprite.set_bounds(QRect(0, 0, 500, 400))
    overlay.add_sprite(sprite)
    overlay._on_tick(dt=1 / 60)       # 消费首帧脏
    sprite._frame_dirty = False

    old_rect = sprite.rect()
    sprite.scale = 1.0                # 画布 320x180 → 640x360

    assert sprite._frame_dirty is True
    assert sprite.rect().width() == 640
    assert sprite.rect() != old_rect
    # 补钳：身体框（无 body_box → 全画布）不得出界
    assert sprite.pos.x() <= 500 - 640 or sprite.rect().right() <= 500 or sprite.pos.x() == 0


def test_scale_setter_rejects_non_positive():
    sprite, _clip = _make_sprite()
    try:
        sprite.scale = 0
    except ValueError:
        pass
    else:
        raise AssertionError("scale=0 必须抛 ValueError")
    assert sprite.scale == 0.5


# ---------------------------------------------------------------- V-12
def test_close_event_stops_tick_timer():
    overlay = OverlayWindow()
    overlay.start()
    assert overlay._timer.isActive()
    overlay.closeEvent(QCloseEvent())
    assert not overlay._timer.isActive()


# ---------------------------------------------------------------- V-13
def test_non_left_press_during_grab_synthesizes_release():
    overlay = OverlayWindow()
    grab = FakeGrab()
    overlay._mouse_grab = grab
    overlay._press_global = QPoint(5, 5)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(10, 10), QPointF(10, 10),
        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier)

    overlay.mousePressEvent(event)

    assert overlay._mouse_grab is None
    assert overlay._press_global is None
    assert grab.released is True
