# -*- coding: utf-8 -*-
"""SpriteBubbleFollower offscreen 单测（4.1c 气泡）。

覆盖：锚点 = 身体框全局换算（非整画布）、30Hz 节流 + 尾部补发、
say 走真实 PetSpeechBubble.show_text 形参、show_image 配图通道、close 静默。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRect
from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from pet.sprite_bubble import SpriteBubbleFollower, sprite_anchor_rect_global

app = QApplication.instance() or QApplication([])


class FakeBubble:
    def __init__(self):
        self.moves: list[QRect] = []
        self.texts: list = []
        self.closed = 0

    def reposition(self, anchor):
        self.moves.append(QRect(anchor))

    def show_text(self, text, anchor, duration_ms, **kw):
        self.texts.append((text, QRect(anchor), duration_ms, kw))

    def hide(self):
        pass

    def close(self):
        self.closed += 1


def _make(tmp_path):
    shell, lib = fac._make_shell(tmp_path)
    shell.sprite.bind_clip("idle1")
    shell.sprite._rebuild_pixmap()
    follower = SpriteBubbleFollower(shell.overlay, shell.sprite)
    follower.bubble = FakeBubble()
    return shell, follower


def test_anchor_uses_body_rect_global(tmp_path):
    shell, follower = _make(tmp_path)
    try:
        anchor = follower.anchor()
        origin = shell.overlay.geometry().topLeft()
        # CapSprite 无 body_rect → 回退整 rect；这里直接用真 PetSprite（fac 壳）
        body = shell.sprite.body_rect() if hasattr(shell.sprite, "body_rect") else shell.sprite.rect()
        assert anchor == QRect(origin + body.topLeft(), body.size())
    finally:
        shell._delete_runtime_marker()


def test_follow_throttle_and_flush(tmp_path):
    shell, follower = _make(tmp_path)
    try:
        follower._on_sprite_moved(shell.sprite)   # 第一发立即
        assert len(follower.bubble.moves) == 1
        follower._on_sprite_moved(shell.sprite)   # 30Hz 窗口内 → 节流滞留
        assert len(follower.bubble.moves) == 1
        assert follower._follow_pending is True
        follower._flush_follow()                  # 尾部补发
        assert len(follower.bubble.moves) == 2
        assert follower._follow_pending is False
    finally:
        shell._delete_runtime_marker()


def test_say_passes_anchor_and_kwargs(tmp_path):
    shell, follower = _make(tmp_path)
    try:
        assert follower.say("你好", 2500, subtitle="sub") is True
        text, anchor, dur, kw = follower.bubble.texts[0]
        assert text == "你好" and dur == 2500 and kw["subtitle"] == "sub"
        assert anchor.width() > 0
        assert follower.say("") is False
        follower.close()
        assert follower.bubble is None
    finally:
        shell._delete_runtime_marker()


def test_show_image_delegates_anchor_and_scale(tmp_path):
    """配图自言自语：形参面对齐 PetSpeechBubble.show_image，锚点/缩放由本层补齐。"""
    shell, follower = _make(tmp_path)
    try:
        calls = []

        def _show_image(path, anchor, duration_ms, **kw):
            calls.append((path, QRect(anchor), duration_ms, kw))
            return True

        follower.bubble.show_image = _show_image
        assert follower.show_image("cat.png", 1500, image_scale=1.6) is True
        path, anchor, duration_ms, kw = calls[0]
        assert path == "cat.png" and duration_ms == 1500
        assert kw["image_scale"] == 1.6
        assert kw["pet_scale"] == shell.sprite.scale
        body = (shell.sprite.body_rect() if hasattr(shell.sprite, "body_rect")
                else shell.sprite.rect())
        assert anchor == QRect(shell.overlay.geometry().topLeft() + body.topLeft(),
                               body.size())
    finally:
        shell._delete_runtime_marker()


def test_show_image_degrades_quietly(tmp_path):
    """气泡不可用 / 底层不支持配图 / 底层抛异常 → False，绝不抛到调用方。"""
    shell, follower = _make(tmp_path)
    try:
        follower.bubble = None
        assert follower.show_image("cat.png", 1000) is False

        follower.bubble = FakeBubble()  # 无 show_image：按失败降级
        assert follower.show_image("cat.png", 1000) is False

        def _boom(*_args, **_kwargs):
            raise RuntimeError("pixmap decode failed")

        follower.bubble.show_image = _boom
        assert follower.show_image("cat.png", 1000) is False
    finally:
        shell._delete_runtime_marker()
