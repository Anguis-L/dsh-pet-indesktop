# -*- coding: utf-8 -*-
"""B7a 生命周期收口：屏迁移后的监听重挂/气泡不丢 + ``stop()`` 释放子宠库与在飞供给。

- **F2（确定缺口）**：三条 ``sprite-removed`` 监听（``behavior.forget`` /
  ``probe.forget`` / ``throw_egg.forget``）只在 ``_build`` 注册，``_migrate_to_screen``
  重建 ``ShellOverlayWindow`` 后不重挂——屏热插拔/主屏切换之后再退出任意一只宠，
  ``remove_sprite`` 不再注销行为/探头/彩蛋状态（强引用 + 探头 armed 状态残留）；
- **屏迁移丢气泡（已登记 §2.3.1）**：``_bind_bubble`` 重建跟随器会 close 旧气泡窗，
  新窗口不 show、``_sticky_*`` 也不重挂——旧版气泡是独立 Tool 窗，跨屏只跟随不销毁；
- **F3（确定缺口）**：``stop()`` 不遍历子宠库 ``shutdown()``、也不
  ``_cancel_frameseq_provisions()``（口径见 ``_on_about_to_quit`` / ``_on_session_end``）。

纪律：offscreen；真壳 + 真 PetSprite；同步直调 handler，不 sleep。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import tests.test_sprite_menu_facade as fac
from tests.test_overlay_spawn import _make_persistent_shell

app = QApplication.instance() or QApplication([])


def _make_shell(tmp_path, values=None):
    shell, lib = fac._make_shell(tmp_path, values)
    shell.sprite.bind_clip("idle1")
    shell.sprite._rebuild_pixmap()
    return shell, lib


def _migrate(shell):
    """按真实入口迁移到主屏（fake screen → primaryScreen）。"""
    primary = app.primaryScreen()
    old = shell.overlay
    shell._migrate_to_screen(primary)
    assert shell.overlay is not old, "前提：迁移确实重建了 overlay"
    return old


# ---------------------------------------------------------------- F2：监听重挂
def test_migrate_to_screen_rewires_sprite_removed_listeners(tmp_path):
    """迁移重建 overlay 后三条 sprite-removed 监听必须仍在（否则退出子宠残留）。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.spawn_pet()
        child = shell._spawned[0]
        _migrate(shell)
        listeners = shell.overlay._sprite_removed_listeners
        assert shell.behavior.forget in listeners
        assert shell._probe.forget in listeners
        assert shell._throw_egg.forget in listeners
        # 行为状态表是可观测的那一条：真 tick 建态 → 真移除 → 必须注销
        shell.driver.tick_sim(1 / 60.0)
        assert child in shell.behavior._states, "前提：行为表已为该 sprite 建态"
        shell.overlay.remove_sprite(child)
        assert child not in shell.behavior._states, "迁移后退出子宠必须仍注销行为状态"
    finally:
        shell.clear_spawned_pets()
        shell._delete_runtime_marker()


def test_migrate_to_screen_keeps_exit_pet_cleanup_path(tmp_path):
    """走真实退出入口（``exit_pet``）时迁移后的清理链完整。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.spawn_pet()
        child = shell._spawned[0]
        _migrate(shell)
        shell.driver.tick_sim(1 / 60.0)
        assert child in shell.behavior._states
        assert shell.exit_pet(child) is True
        assert child not in shell.behavior._states
        assert child not in shell.overlay.sprites
    finally:
        shell.clear_spawned_pets()
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- 屏迁移：气泡不丢
def test_migrate_to_screen_restores_sticky_bubble(tmp_path):
    """粘滞气泡跨屏迁移不丢：重建跟随器后按原内容重新挂上。

    旧版气泡是不随窗重建的独立 Tool 窗，跨屏只跟随不销毁；新版重建跟随器会
    ``follower.close()`` 关掉真气泡窗，若不重挂，用户正在看的审批/提醒气泡
    在拔屏/主屏切换瞬间消失（且 ``_sticky_*`` 仍在 = 状态与画面不一致）。
    """
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.start()
        shell._sticky_bubble_active = True
        shell._sticky_text = "审批：请确认"
        shell._show_bubble_text("审批：请确认", 0, sticky=True)
        assert shell._speech_bubble.isVisible() is True

        _migrate(shell)

        assert shell._sticky_bubble_active is True, "粘滞态跨迁移保留"
        assert shell._speech_bubble.isVisible() is True, "迁移后粘滞气泡必须重新挂上"
        assert shell._speech_bubble._raw_text == "审批：请确认"
    finally:
        shell.stop()
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_migrate_to_screen_restores_transient_bubble(tmp_path):
    """限时（非粘滞）气泡跨屏迁移也不丢：按迁移前抓下的原文案补一次。"""
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.start()
        shell._show_bubble_text("在吗", 3200)
        assert shell._speech_bubble.isVisible() is True

        _migrate(shell)

        assert shell._speech_bubble.isVisible() is True, "迁移后限时气泡必须补回"
        assert shell._speech_bubble._raw_text == "在吗"
    finally:
        shell.stop()
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_migrate_to_screen_does_not_orphan_old_bubble(tmp_path):
    """重建跟随器时旧气泡不得被"隐藏回调"重新挂回（孤儿顶层气泡）。

    新气泡已在迁移中被替换：旧窗必须真的收掉，否则桌面上会留下一个不再跟随
    sprite、也不受壳管理的孤儿顶层气泡（迁移期 overlay 尚未 show，隐藏回调
    因此早退——这条用例锁住该顺序不被改坏）。
    """
    shell, _lib = _make_shell(tmp_path)
    try:
        shell.start()
        shell._sticky_bubble_active = True
        shell._sticky_text = "审批：请确认"
        shell._show_bubble_text("审批：请确认", 0, sticky=True)
        old_bubble = shell._speech_bubble

        _migrate(shell)

        assert shell._speech_bubble is not old_bubble
        assert old_bubble.isVisible() is False, "被替换掉的旧气泡窗必须收掉"
    finally:
        shell.stop()
        shell.overlay.close()
        shell._delete_runtime_marker()


# ---------------------------------------------------------------- F3：stop() 释放
def test_stop_releases_spawned_libraries_and_inflight_provisions(tmp_path):
    """``stop()`` 必须收掉子宠库与在飞帧序列供给线程（复用既有收口路径）。"""
    shell, made = _make_persistent_shell(tmp_path)
    shell.start()
    shell.spawn_pet()
    child = shell._spawned[0]
    child_lib = shell._spawned_libs[child]
    main_lib = shell.lib
    cancels: list = []
    main_cancels: list = []
    child_lib.cancel_frameseq_provision = lambda: cancels.append(1)
    main_lib.cancel_frameseq_provision = lambda: main_cancels.append(1)

    shell.stop()

    assert child_lib.shutdown_calls == 1, "子宠库必须 shutdown（活线程是库的子对象）"
    assert cancels, "子宠库的在飞供给必须取消"
    assert main_cancels, "主库的在飞供给同样取消"
