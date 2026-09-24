# -*- coding: utf-8 -*-
"""Windows 逐像素穿透轮询的「包围盒两段式」回归（DS 审查 M12b）。

背景：``WindowsPerPixelInputController`` 原为恒定 10ms 轮询（拖拽 100ms）。
光标在窗口包围盒外时逐像素判定必然「无命中 → 穿透」，10ms 一次的
QCursor.pos()/mapFromGlobal/GetWindowLong 调用纯属空转（overlay 铺满整屏时
命中判定也要每 10ms 走一遍全部 sprite 的 alpha 查询）。

要求：光标在包围盒内 = 10ms（逐像素跟手），盒外 = 50ms（低频），拖拽仍
100ms 且优先级最高；松手恢复时按当前位置重新定档。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication

from pet import platform_win
from pet.platform_win import WindowsPerPixelInputController as Ctl

app = QApplication.instance() or QApplication([])

WIN_LEFT, WIN_TOP, WIN_W, WIN_H = 100, 200, 400, 300


class _FakeCursor:
    """可设定的假光标（QCursor.pos 静态方法替身）。"""

    position = QPoint(1000, 1000)

    @classmethod
    def pos(cls):
        return QPoint(cls.position)


class _FakeWindow:
    def __init__(self):
        self.mouse_through = False
        self._press_global = None
        self.visible = True

    def winId(self):
        return 4242

    def mapFromGlobal(self, point):
        return QPoint(point.x() - WIN_LEFT, point.y() - WIN_TOP)

    def _is_transparent_at(self, local):
        return True

    def isVisible(self):
        return self.visible

    def width(self):
        return WIN_W

    def height(self):
        return WIN_H


def _make_controller(monkeypatch):
    """绕过 __init__（不碰真实 Win32 样式），拼出与生产同形的控制器。"""
    monkeypatch.setattr(platform_win, "QCursor", _FakeCursor)
    clicks: list[bool] = []
    monkeypatch.setattr(platform_win, "_set_windows_click_through",
                        lambda hwnd, enabled: clicks.append(bool(enabled)))
    controller = object.__new__(Ctl)
    controller._window = _FakeWindow()
    controller._timer = QTimer()
    controller._timer.setInterval(Ctl.NORMAL_POLL_INTERVAL_MS)
    return controller, clicks


def test_poll_tiers_constants():
    assert Ctl.NORMAL_POLL_INTERVAL_MS == 10
    assert Ctl.IDLE_POLL_INTERVAL_MS == 50
    assert Ctl.DRAG_POLL_INTERVAL_MS == 100
    assert Ctl.IDLE_POLL_INTERVAL_MS > Ctl.NORMAL_POLL_INTERVAL_MS


def test_refresh_switches_tier_by_bounding_box(monkeypatch):
    """盒内 10ms / 盒外 50ms：同一 refresh 入口按光标位置定档。"""
    controller, clicks = _make_controller(monkeypatch)

    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)   # 盒内
    controller.refresh()
    assert controller._timer.interval() == Ctl.NORMAL_POLL_INTERVAL_MS
    assert clicks == [True]                                      # 盒内空处：穿透

    _FakeCursor.position = QPoint(WIN_LEFT - 50, WIN_TOP - 50)   # 盒外
    controller.refresh()
    assert controller._timer.interval() == Ctl.IDLE_POLL_INTERVAL_MS

    _FakeCursor.position = QPoint(WIN_LEFT + WIN_W + 5, WIN_TOP + 5)  # 右缘外
    controller.refresh()
    assert controller._timer.interval() == Ctl.IDLE_POLL_INTERVAL_MS

    _FakeCursor.position = QPoint(WIN_LEFT + WIN_W - 1, WIN_TOP + WIN_H - 1)
    controller.refresh()
    assert controller._timer.interval() == Ctl.NORMAL_POLL_INTERVAL_MS


def test_drag_tier_wins_over_cursor_position(monkeypatch):
    """拖拽中 refresh 不得把 100ms 打回按位置定的档（事件必须持续送达）。"""
    controller, _clicks = _make_controller(monkeypatch)
    controller._window._press_global = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller._timer.setInterval(Ctl.DRAG_POLL_INTERVAL_MS)

    _FakeCursor.position = QPoint(WIN_LEFT + 10, WIN_TOP + 10)   # 盒内
    controller.refresh()
    assert controller._timer.interval() == Ctl.DRAG_POLL_INTERVAL_MS

    _FakeCursor.position = QPoint(0, 0)                          # 盒外
    controller.refresh()
    assert controller._timer.interval() == Ctl.DRAG_POLL_INTERVAL_MS


def test_release_resumes_position_tier(monkeypatch):
    """松手：先按当前位置重新定档并强制刷新一次穿透状态。"""
    controller, _clicks = _make_controller(monkeypatch)
    controller._window._press_global = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    controller._timer.setInterval(Ctl.DRAG_POLL_INTERVAL_MS)

    _FakeCursor.position = QPoint(0, 0)                          # 盒外
    controller._window._press_global = None
    controller.set_drag_active(False)
    assert controller._timer.interval() == Ctl.IDLE_POLL_INTERVAL_MS

    controller._timer.setInterval(Ctl.DRAG_POLL_INTERVAL_MS)
    controller._window._press_global = QPoint(WIN_LEFT + 10, WIN_TOP + 10)
    _FakeCursor.position = QPoint(WIN_LEFT + 20, WIN_TOP + 20)   # 盒内
    controller._window._press_global = None
    controller.set_drag_active(False)
    assert controller._timer.interval() == Ctl.NORMAL_POLL_INTERVAL_MS
