# -*- coding: utf-8 -*-
"""音乐自动唱歌（``music_sing_enabled``）overlay 接线回归（DS 审查 M5f）。

旧架构落点：``window.py:485-489/745-746`` 建 1s 轮询 + 显隐对称启停；
``window_alerts.py:495-550`` 的 host 形实现（检测/唱歌态/静音宽限期）。
overlay 拓扑下 PetWindow 不构造，该键此前零消费（死开关）。

接法对齐 self_talk：壳提供 host 形转发（``_check_music_sing`` /
``_switch`` / ``_is_one_shot_playing``）+ 定时器 + 配置热改/显隐对称。

纪律：offscreen；monkeypatch 音乐检测函数，同步直调 handler，不 sleep。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from tests.test_overlay_dead_switches import _make_shell

app = QApplication.instance() or QApplication([])


class _MusicSingHarness:
    """把 shell 与假音乐检测拼起来（不依赖真实音频 COM/素材）。"""

    def __init__(self, shell, playing=True):
        self.shell = shell
        self.playing = playing

    def check(self):
        from pet import music_detect
        original = music_detect.is_music_playing
        try:
            music_detect.is_music_playing = lambda: self.playing
            self.shell._check_music_sing()
        finally:
            music_detect.is_music_playing = original


def test_music_sing_polls_when_enabled(tmp_path):
    """开关开：1s 轮询启动；隐藏停表、恢复显示按开关重启。"""
    shell, _lib = _make_shell(tmp_path, {"music_sing_enabled": True})
    try:
        shell.overlay.show()
        app.processEvents()
        assert shell._music_sing_timer.interval() == 1000
        assert shell._music_sing_timer.isActive()
        shell.set_pet_visible(False)
        assert not shell._music_sing_timer.isActive()
        shell.set_pet_visible(True)
        assert shell._music_sing_timer.isActive()
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_music_sing_disabled_no_timer(tmp_path):
    """开关默认关：定时器不跑（不可见零消耗纪律）。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        app.processEvents()
        assert not shell._music_sing_timer.isActive()
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_music_sing_switches_to_sing_anim_and_back(tmp_path):
    """检测到音乐 → 播唱歌动画；音乐停 + 宽限期过 → 退出唱歌态。"""
    shell, lib = _make_shell(tmp_path, {"music_sing_enabled": True})
    # 唱歌素材名与旧架构同源常量（window_alerts.check_music_sing 读它）
    from pet.window import SING_ANIM
    lib._clips[SING_ANIM] = fac.FakeClip(SING_ANIM, 24)
    try:
        shell.overlay.show()
        app.processEvents()
        harness = _MusicSingHarness(shell, playing=True)
        harness.check()
        assert shell._music_sing_active is True
        assert shell.behavior.anim_of(shell.sprite) == SING_ANIM

        # 音乐停：宽限期内仍保持唱歌（前奏/间奏不退出）
        harness.playing = False
        shell._music_sing_silent_since = time.monotonic()
        harness.check()
        assert shell._music_sing_active is True
        # 宽限期（>=1s）已过：退出唱歌态
        shell._music_sing_silent_since = time.monotonic() - 999.0
        harness.check()
        assert shell._music_sing_active is False
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_music_sing_config_hot_toggle(tmp_path):
    """设置页热改：开→启轮询，关→停表并退出唱歌态。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        app.processEvents()
        assert not shell._music_sing_timer.isActive()
        shell._config.set("music_sing_enabled", True)
        shell.refresh_settings()
        assert shell._music_sing_timer.isActive()
        shell._config.set("music_sing_enabled", False)
        shell.refresh_settings()
        assert not shell._music_sing_timer.isActive()
        assert shell._music_sing_active is False
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()
