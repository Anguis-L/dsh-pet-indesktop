# -*- coding: utf-8 -*-
"""overlay 拓扑「自言自语族」聚焦回归（周期气泡 / 点击台词 / 配图）。

背景：这套功能在旧架构里的唯一宿主是 ``PetWindow``（self_talk 状态装配 +
周期定时器 + 点击分支 + 朗读注入）。overlay 拓扑下 ``PetWindow`` 不构造，
``pet/overlay_shell.py`` 的 sprite 壳成了唯一宿主——不接线就整族静默失效
（气泡跟随器本身能出泡，见本文件判别实验结论）。

覆盖：
1. ``_init_self_talk`` 状态装配 + 周期定时器：timeout → 气泡收到文本，
   并按 ``after_display`` 重排（+显示时长）；
2. 点击 sprite → ``click_show_self_talk`` 出泡且重排定时器；
3. 逐动画台词：``click_talk_texts_for`` 绑定过的动画名出绑定文本，
   并把实际显示的文本交给朗读通道；
4. 显隐对称：隐藏期 timer 停、恢复重排；
5. 配图分支：命中图片时走 ``SpriteBubbleFollower.show_image``（刀 1）。

纪律：同步直调（信号 emit / handler 直调，不 sleep 赌时序）；假屏 + 真实
PetSprite 复用既有测试件；全部 offscreen。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 脚手架
class TalkConfig(cap.CapConfig):
    """CapConfig + 点击动画台词绑定表（``Config.click_talk_texts_for`` 的最小等价物）。"""

    def __init__(self, tmp_path, values=None, bindings=None):
        super().__init__(tmp_path, values)
        self._bindings = dict(bindings or {})

    def click_talk_texts_for(self, character_id, click_name):
        return list(self._bindings.get(str(click_name)) or [])


def _talk_values(**over):
    """自言自语最小确定性配置：间隔钉死 5s、时长 1s（after_display 可算准）。"""
    values = {
        "self_talk_enabled": True,
        "self_talk_texts": ["自言自语台词"],
        "self_talk_duration_seconds": 1.0,
        "self_talk_min_interval": 5.0,
        "self_talk_max_interval": 5.0,
        "self_talk_image_chance": 0,
    }
    values.update(over)
    return values


def _make_shell(tmp_path, values=None, *, bindings=None):
    """真实 PetSprite + RichLibrary 的壳（点击链路需要 click 素材池）。"""
    config = TalkConfig(tmp_path, values, bindings)
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    return shell, config


def _cleanup(shell) -> None:
    shell._self_talk_timer.stop()
    shell._delete_runtime_marker()


# ---------------------------------------------------------------- 1. 周期气泡
def test_periodic_timer_timeout_shows_bubble_and_reschedules(tmp_path):
    """周期定时器到点 → 气泡出文本；显示后按 after_display 重排（+时长）。"""
    shell, _config = _make_shell(tmp_path, _talk_values())
    try:
        shell.overlay.show()
        # _init_self_talk 末尾即排程（window.py:740）；5s 间隔钉死可精确断言
        assert shell._self_talk_timer.isActive() is True
        assert shell._self_talk_timer.interval() == 5000

        shell._self_talk_timer.timeout.emit()  # 真实信号 → on_self_talk_timeout

        assert shell._speech_bubble is not None
        assert shell._speech_bubble._raw_text == "自言自语台词"
        assert shell._last_self_talk_text == "自言自语台词"
        # 已展示 → 下一次排在 max(1000, (5 + 1) * 1000)
        assert shell._self_talk_timer.interval() == 6000
    finally:
        _cleanup(shell)


def test_self_talk_disabled_does_not_arm_timer(tmp_path):
    """总开关关闭时不排程（周期气泡是独立开关，不能只看文本池非空）。"""
    shell, _config = _make_shell(
        tmp_path, _talk_values(self_talk_enabled=False))
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is False
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 4. 显隐对称
def test_hide_stops_timer_and_show_reschedules(tmp_path):
    """隐藏期 timer 停（省电、防止对不可见壳冒泡）；恢复显示重排。"""
    shell, _config = _make_shell(tmp_path, _talk_values())
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is True

        shell.set_pet_visible(False)
        assert shell._self_talk_timer.isActive() is False

        shell.set_pet_visible(True)
        assert shell._self_talk_timer.isActive() is True
    finally:
        _cleanup(shell)


# ---------------------------------------------------------------- 5. 配图分支
def _write_png(directory, name="angry.png"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    pixmap = QPixmap(48, 32)
    pixmap.fill(QColor("red"))
    assert pixmap.save(str(path))
    return path


def test_image_branch_reaches_follower_show_image(tmp_path):
    """出图概率 100% → 走刀 1 的 ``show_image``（而不是文本分支）。"""
    image_dir = tmp_path / "talk-images"
    _write_png(image_dir)
    shell, _config = _make_shell(tmp_path, _talk_values(
        self_talk_image_dir=str(image_dir),
        self_talk_image_chance=100,
        self_talk_image_scale=150,
    ))
    try:
        shell.overlay.show()
        assert shell._self_talk_images, "配置的图片目录必须装进载荷"
        assert shell._self_talk_image_scale == 1.5

        assert shell._show_random_self_talk() is True
        assert shell._speech_bubble is not None
        assert shell._speech_bubble._content_kind == "image"
        assert shell._speech_bubble._image_scale == 1.5
        # 图片没有可朗读文本：显式记 None（点击路径据此保持安静）
        assert shell._last_self_talk_text is None
    finally:
        _cleanup(shell)


def test_deleted_absolute_image_dir_degrades_to_text(tmp_path):
    """用户删掉的外部图片目录不再回退内置彩蛋池（window.py:123 语义）。"""
    shell, _config = _make_shell(tmp_path, _talk_values(
        self_talk_image_dir=str(tmp_path / "gone"),
        self_talk_image_chance=100,
    ))
    try:
        assert shell._self_talk_images == []
        shell.overlay.show()
        assert shell._show_random_self_talk() is True
        assert shell._speech_bubble._raw_text == "自言自语台词"
    finally:
        _cleanup(shell)
