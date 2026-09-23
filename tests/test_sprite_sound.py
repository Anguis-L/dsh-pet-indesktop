# -*- coding: utf-8 -*-
"""SpriteSoundPlayer + shell 音效接线 offscreen 单测（4.1c）。

覆盖：点击音量走 click_sound_volume、碰撞 j 闸门与轻重分档、80ms 节流、
enabled 关静默、解析失败静默降级、shell 单击路由触发 click_feedback、
shell 碰撞监听器挂接。play_sound 全程打桩，不出声。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from pet import click_sound
from pet.sprite_sound import SpriteSoundPlayer

app = QApplication.instance() or QApplication([])


class _Cfg:
    def __init__(self, values=None):
        self._v = dict(values or {})
        self.dir = None

    def get(self, key, default=None):
        return self._v.get(key, default)


def _player(monkeypatch, values=None, hit_min_dv=100.0, clock=None):
    plays = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: plays.append(volume) or True)
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates",
                        lambda pack, data_dir=None: ["/tmp/click.wav"])
    world = SimpleNamespace(hit_min_dv=hit_min_dv)
    player = SpriteSoundPlayer(_Cfg(values), world,
                               clock=clock or (lambda: 1000.0))
    return player, plays


def test_click_volume_from_config(monkeypatch):
    player, plays = _player(monkeypatch, {"click_sound_volume": 0.3})
    player.on_click()
    assert plays == [0.3]


def test_collision_gate_and_tiers(monkeypatch):
    player, plays = _player(monkeypatch, hit_min_dv=100.0)
    player.on_collision(SimpleNamespace(j=50.0))    # 不到闸门不发声
    assert plays == []
    player.on_collision(SimpleNamespace(j=150.0))   # 过闸门轻档
    assert plays == [0.45]
    player._last_play = 0.0
    player.on_collision(SimpleNamespace(j=250.0))   # ≥2× 重档
    assert plays == [0.45, 0.9]


def test_throttle(monkeypatch):
    now = [1000.0]
    player, plays = _player(monkeypatch, clock=lambda: now[0])
    player.on_click()
    player.on_click()                               # 80ms 内第二次被节流
    assert len(plays) == 1
    now[0] += 0.1
    player.on_click()
    assert len(plays) == 2


def test_disabled_silences(monkeypatch):
    player, plays = _player(monkeypatch, {"click_sound_enabled": False})
    player.on_click()
    player.on_collision(SimpleNamespace(j=999.0))
    assert plays == []


def test_resolve_failure_degrades(monkeypatch):
    plays = []
    monkeypatch.setattr(click_sound, "play_sound",
                        lambda path, volume: plays.append(volume))
    def boom(pack, data_dir=None):
        raise RuntimeError("no pack")
    monkeypatch.setattr(click_sound, "resolve_click_sound_candidates", boom)
    player = SpriteSoundPlayer(_Cfg(), SimpleNamespace(hit_min_dv=0.0))
    player.on_click()                               # 不抛异常
    player.on_collision(SimpleNamespace(j=1.0))
    assert plays == []


# ---------------------------------------------------------------- shell 接线
def test_shell_click_feedback_and_collision_listener(tmp_path, monkeypatch):
    import tests.test_overlay_window_capabilities as cap

    shell = cap._make_shell(tmp_path)
    try:
        clicks = []
        shell.overlay.click_feedback = lambda: clicks.append(1)
        # 单击路由：threshold 内 press→release
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent

        sprite = shell.sprite
        center = sprite.rect().center()
        shell.overlay._mouse_grab = sprite
        shell.overlay._press_pos = QPointF(center)
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(center), QPointF(center),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier)
        shell.overlay.behavior = type("B", (), {
            "on_sprite_clicked": lambda self, s: True})()
        shell.overlay.mouseReleaseEvent(event)
        assert clicks == [1]
        # 碰撞监听器已挂接
        assert shell._sound.on_collision in shell.collision._listeners
    finally:
        shell._delete_runtime_marker()
