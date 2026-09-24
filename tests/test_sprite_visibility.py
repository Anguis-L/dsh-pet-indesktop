# -*- coding: utf-8 -*-
"""per-sprite visible 回归（DS 审查 M14 / PHASE4_DESIGN.md:248-250 4.1b 验收项）。

设计：PetSprite 加 ``visible`` 字段（默认 True）；隐藏的 sprite 必须从**三处**
排除——逐像素命中（``sprite_at``，穿透判据同源）、位置监听 fanout、绘制
（paintEvent）；壳层 ``set_pet_visible`` 的整窗语义不变，托盘逐只子菜单接上
逐只显隐。

纪律：offscreen；同步直调事件处理器/handler，不起真实 QTimer、不 sleep。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF
from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from tests.test_overlay_dead_switches import _make_shell
from tests.test_overlay_window import FakeSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- PetSprite
def _make_pet_sprite():
    from pet.pet_sprite import PetSprite

    lib = fac.RichLibrary()
    sprite = PetSprite(lib, pos=QPointF(50, 50), scale=0.5)
    sprite.bind_clip("idle1")
    sprite._rebuild_pixmap()
    return sprite


def test_pet_sprite_visible_defaults_true_and_toggle_reports_dirty():
    """默认可见；切隐藏走既有脏矩形通道（sprite 不是窗口，得自己擦残留）。"""
    sprite = _make_pet_sprite()
    assert sprite.visible is True

    reports: list = []
    sprite._dirty_cb = lambda old, new: reports.append((old, new))
    sprite.set_visible(False)

    assert sprite.visible is False
    assert len(reports) == 1
    old, new = reports[0]
    assert old == new == sprite.paint_bounds()   # 原地区域上报 = 擦除

    sprite.set_visible(False)                    # 幂等：不再上报
    assert len(reports) == 1
    sprite.set_visible(True)
    assert sprite.visible is True
    assert len(reports) == 2


# ---------------------------------------------------------------- OverlayWindow 三处排除
def test_hidden_sprite_excluded_from_hit_and_click_through():
    """命中与穿透判据：隐藏 sprite 不参与逐像素联合命中。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (64, 64))
    overlay.add_sprite(sprite)

    assert overlay.sprite_at(QPoint(4, 4)) is sprite
    assert overlay._is_transparent_at(QPoint(4, 4)) is False

    sprite.visible = False
    assert overlay.sprite_at(QPoint(4, 4)) is None
    assert overlay._is_transparent_at(QPoint(4, 4)) is True

    sprite.visible = True
    assert overlay.sprite_at(QPoint(4, 4)) is sprite


def test_hidden_sprite_excluded_from_position_fanout():
    """位置 fanout：隐藏 sprite 的 rect 变化不再触发跟随回调。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((10, 10), (40, 40), movable=True)
    sprite.velocity = QPointF(100, 0)
    overlay.add_sprite(sprite)
    seen: list = []
    overlay.add_position_listener(sprite, seen.append)

    overlay._on_tick(dt=0.1)
    assert seen == [sprite]

    sprite.visible = False
    seen.clear()
    sprite.frame_dirty = True
    overlay._on_tick(dt=0.1)
    assert seen == [], "隐藏 sprite 不得进位置 fanout"


def test_hidden_sprite_excluded_from_paint():
    """绘制：隐藏 sprite 不画（脏区域照旧被擦除）。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (64, 64))
    sprite.visible = False
    overlay.add_sprite(sprite)

    overlay.grab()
    assert sprite.paint_calls == 0

    sprite.visible = True
    overlay.grab()
    assert sprite.paint_calls == 1


# ---------------------------------------------------------------- 壳：整窗语义与逐只
def test_shell_set_sprite_visible_keeps_window_semantics(tmp_path):
    """逐只显隐只改 sprite 标志；整窗显隐（set_pet_visible）语义不变。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = shell.sprite
        assert shell._sprite_visible(sprite) is True

        shell.set_sprite_visible(sprite, False)
        assert sprite.visible is False
        assert shell._sprite_visible(sprite) is False

        shell.toggle_sprite_visible(sprite)
        assert sprite.visible is True

        # 整窗显隐：不动 per-sprite 标志
        shell.overlay.show()
        shell.set_pet_visible(False)
        assert shell.overlay.isVisible() is False
        assert sprite.visible is True
        shell.set_pet_visible(True)
        assert shell.overlay.isVisible() is True
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_tray_menu_has_per_sprite_visibility_toggle(tmp_path):
    """托盘逐只子菜单：勾选态 = 该 sprite 当前可见性；重开菜单同步刷新。"""
    shell, lib = _make_shell(tmp_path)
    try:
        second = shell._sprite_factory(lib, QPointF(0, 0), 1.0)
        shell.overlay.add_sprite(second)
        shell._spawned.append(second)
        shell._spawned_slots[second] = 3
        shell._refresh_tray_menu()

        menu = shell._tray_menu
        assert menu is not None
        sub = next(a.menu() for a in menu.actions()
                   if a.text().startswith("小肥鱼"))
        action = next(a for a in sub.actions() if a.text() == "显示这只")
        assert action.isCheckable() is True
        assert action.isChecked() is True
        assert shell._sprite_visible(second) is True

        action.trigger()                       # 取消勾选 = 隐藏这只
        assert second.visible is False
        assert shell.sprite.visible is True    # 只影响被点的那只

        # 弹出前同步：外部改了标志也能反映到勾选态
        second.visible = True
        shell._sync_tray_pet_visibility()
        assert action.isChecked() is True
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_click_route_ignores_hidden_sprite(tmp_path):
    """隐藏 sprite 不参与鼠标路由（按下不建立 grab）。"""
    from PySide6.QtCore import QEvent

    from tests.test_overlay_dead_switches import _hit_pos, _mouse_event

    shell, _lib = _make_shell(tmp_path)
    try:
        sprite = shell.sprite
        pos = _hit_pos(sprite)
        shell.set_sprite_visible(sprite, False)
        press = _mouse_event(QEvent.Type.MouseButtonPress, pos)
        shell.overlay.mousePressEvent(press)
        assert shell.overlay._mouse_grab is None
        assert not press.isAccepted()
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_sprite_without_visible_field_treated_as_visible():
    """鸭式 sprite（无 visible 字段）沿用既有语义：恒可见（灰假件不被打红）。"""
    from pet.overlay_window import OverlayWindow

    overlay = OverlayWindow()
    sprite = FakeSprite((0, 0), (64, 64))     # test_overlay_window.FakeSprite 无 visible
    assert not hasattr(sprite, "visible")
    overlay.add_sprite(sprite)
    assert overlay.sprite_at(QPoint(4, 4)) is sprite
    overlay.grab()
    assert sprite.paint_calls == 1
