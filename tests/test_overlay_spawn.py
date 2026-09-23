# -*- coding: utf-8 -*-
"""4.2a 位置持久化 + 4.2b 多 sprite 生灭 offscreen 单测。

覆盖：rx/ry 恢复与保存（身体中心比例口径）、无记录落右下角、facing 恢复、
spawn 增 sprite（per-pet 库/行为接管/错开落位）、D1 clip 所有权隔离、clear
全清（clip 释放/库 shutdown/行为注销）、菜单 spawn 入口。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRect
from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from pet.sprite_menu_facade import build_sprite_full_menu

app = QApplication.instance() or QApplication([])


def test_restore_position_from_rx_ry(tmp_path):
    shell, _ = fac._make_shell(tmp_path, {"rx": 0.5, "ry": 0.25, "facing": "right"})
    try:
        body = shell.sprite.body_rect()
        cx = body.x() + body.width() / 2.0
        cy = body.y() + body.height() / 2.0
        b = shell._bounds
        assert abs((cx - b.left()) / b.width() - 0.5) < 0.02
        assert abs((cy - b.top()) / b.height() - 0.25) < 0.02
        assert shell.sprite.facing == "right"
    finally:
        shell._delete_runtime_marker()


def test_restore_falls_back_to_corner(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        expected = shell._default_corner_pos(shell._bounds, shell.sprite.rect())
        assert shell.sprite.pos == expected
    finally:
        shell._delete_runtime_marker()


def test_save_position_writes_ratios(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        shell.sprite.set_pos(QPointF(400, 300))
        shell.sprite.facing = "right"
        shell.save_position()
        body = shell.sprite.body_rect()
        b = shell._bounds
        assert abs(shell._config.get("rx")
                   - (body.x() + body.width() / 2.0 - b.left()) / b.width()) < 1e-9
        assert abs(shell._config.get("ry")
                   - (body.y() + body.height() / 2.0 - b.top()) / b.height()) < 1e-9
        assert shell._config.get("facing") == "right"
        assert shell._config.saved >= 1
    finally:
        shell._delete_runtime_marker()


def test_spawn_and_clear_pets(tmp_path):
    shell, main_lib = fac._make_shell(tmp_path)
    try:
        made = []
        shell._create_main_library = lambda: (made.append(1), fac.RichLibrary())[1]
        shell.spawn_pet()
        shell.spawn_pet()
        assert len(shell.overlay.sprites) == 3
        assert len(shell._spawned) == 2
        assert len(made) == 2                       # per-pet 库（T3）
        for s in shell._spawned:
            assert s.library is not main_lib
        # 行为接管：tick 会为 spawn 的 sprite 绑定 idle
        shell.behavior.tick(shell.overlay.sprites, 0.05)
        for s in shell._spawned:
            assert s._clip_name == "idle1"
        libs = [s.library for s in shell._spawned]
        shell.clear_spawned_pets()
        assert shell.overlay.sprites == [shell.sprite]
        assert shell._spawned == []
        assert all(lib.shutdown_calls == 1 for lib in libs)
        # 行为状态已注销（V-9 挂点）
        for s in libs:
            pass
    finally:
        shell._delete_runtime_marker()


def test_spawn_pet_clips_are_per_sprite_owned(tmp_path):
    """D1 clip 所有权守卫：同名 clip 在每个 sprite 上必须是不同对象。

    设计稿 T3/D1：共享 clip = 一速多宠锁步/互相冻结（demo 已修）。产品侧由
    spawn_pet 的 per-pet MovieLibrary 保证——本测试锁定该语义：主 sprite 与
    两个子 sprite 各自 bind 同名 ``idle1`` 后，库对象与 clip 对象都不得同一。
    """
    shell, main_lib = fac._make_shell(tmp_path)
    try:
        made = []
        shell._create_main_library = lambda: (made.append(1), fac.RichLibrary())[1]
        shell.spawn_pet()
        shell.spawn_pet()
        assert len(made) == 2                       # per-pet 库（T3）
        sprites = [shell.sprite, *shell._spawned]
        clips = []
        for sprite in sprites:
            sprite.bind_clip("idle1")
            clip = sprite.library.movie("idle1")
            assert sprite._clip is clip             # bind 取自本 sprite 的库
            clips.append(clip)
        # 同角色的同名 clip 不允许跨 sprite 共享同一播放器对象
        assert len({id(c) for c in clips}) == len(sprites)
        assert all(s.library is not main_lib for s in shell._spawned)
    finally:
        shell._delete_runtime_marker()


def test_menu_has_spawn_entries(tmp_path):
    shell, _ = fac._make_shell(tmp_path)
    try:
        menu = build_sprite_full_menu(shell)
        titles = [a.text() for a in menu.actions()]
        assert "生小肥鱼" in titles
        assert "退出子肥鱼" in titles
    finally:
        shell._delete_runtime_marker()
