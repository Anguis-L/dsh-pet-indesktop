# -*- coding: utf-8 -*-
"""锁屏/挂起降档（O4）：WM_WTSSESSION_CHANGE + WM_POWERBROADCAST → driver.set_suspended。

缺口（审计原文）：会话探测器只认 WM_QUERYENDSESSION/WM_ENDSESSION，锁屏时
overlay 仍可见 → 档位停在 T1（AC 下满速）+ clip 24fps 解码跑整夜。

契约：
- 锁屏（WTS_SESSION_LOCK=7）/挂起（PBT_APMSUSPEND=4）→ ``set_suspended(True)``：
  强制 T3 + 逐 sprite pause_clip；
- 解锁（WTS_SESSION_UNLOCK=8）/恢复（PBT_APMRESUMEAUTOMATIC=0x12 /
  PBT_APMRESUMESUSPEND=7）→ ``set_suspended(False)``：note_kinetic 同帧回全速 +
  resume_clip；
- **只降档**：不 hide overlay、不动全屏/光标 watcher；
- 锁屏通知需 WTSRegisterSessionNotification 注册才会收到：注册失败降级为不支持
  并记日志（绝不抛）；非 Windows 平台无操作。

纪律：真 ``wintypes.MSG`` 结构 + ``ctypes.addressof``（同
``tests/test_session_watcher_message_read.py``）；不注册真实 WTS 通知（打桩 win32
边界），不起真 QTimer、不 sleep 赌时序。
"""
from __future__ import annotations

import ctypes
import logging
import sys

import pytest
from ctypes import wintypes
from PySide6.QtCore import QObject, Signal

from pet import session_watcher as sw_mod


def _msg_pointer(message: int, wparam: int = 0):
    """构造真实 MSG 结构并返回 (地址, 结构体)；结构体需保活到断言结束。"""
    msg = wintypes.MSG()
    msg.hwnd = 0
    msg.message = message
    msg.wParam = wparam
    msg.lParam = 0
    msg.time = 0
    msg.pt.x = 0
    msg.pt.y = 0
    return ctypes.addressof(msg), msg


class _FakeApp(QObject):
    """带 Qt 会话信号的假 app（只暴露探测器真正使用的接口）。"""

    commitDataRequest = Signal()
    aboutToQuit = Signal()


# ---------------------------------------------------------------- 消息解码
@pytest.mark.parametrize("wparam,expected", [
    (sw_mod.WTS_SESSION_LOCK, "session_lock"),
    (sw_mod.WTS_SESSION_UNLOCK, "session_unlock"),
])
def test_session_change_message_decodes_lock_state(wparam, expected):
    addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE, wparam)
    assert sw_mod.session_power_event(addr) == expected
    assert keepalive is not None


@pytest.mark.parametrize("wparam,expected", [
    (sw_mod.PBT_APMSUSPEND, "power_suspend"),
    (sw_mod.PBT_APMRESUMEAUTOMATIC, "power_resume"),
    (sw_mod.PBT_APMRESUMESUSPEND, "power_resume"),
])
def test_power_broadcast_message_decodes_suspend_state(wparam, expected):
    addr, keepalive = _msg_pointer(sw_mod.WM_POWERBROADCAST, wparam)
    assert sw_mod.session_power_event(addr) == expected
    assert keepalive is not None


def test_unrelated_messages_and_bogus_pointers_return_none():
    addr, keepalive = _msg_pointer(0x000F)          # WM_PAINT
    assert sw_mod.session_power_event(addr) is None
    # 会话结束消息不由本函数负责（走既有 session_end_reason）
    addr2, keepalive2 = _msg_pointer(sw_mod.WM_QUERYENDSESSION)
    assert sw_mod.session_power_event(addr2) is None
    # 同一条消息的非挂起 power 事件（如 PBT_APMQUERYSUSPEND=0）不算
    addr3, keepalive3 = _msg_pointer(sw_mod.WM_POWERBROADCAST, 0x0000)
    assert sw_mod.session_power_event(addr3) is None
    assert sw_mod.session_power_event(None) is None
    assert sw_mod.session_power_event(0) is None
    assert sw_mod.session_power_event("not-a-pointer") is None
    assert keepalive is not None and keepalive2 is not None and keepalive3 is not None


def test_non_session_message_skips_full_struct_copy(monkeypatch):
    """窄读纪律不变：非（会话结束/电源）消息只读 message 字段，不整结构拷贝。"""
    calls: list = []
    real = ctypes.string_at

    def _recording(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(sw_mod.ctypes, "string_at", _recording)
    addr, keepalive = _msg_pointer(0x000F)          # WM_PAINT
    assert sw_mod.session_end_reason(addr) is None
    assert calls == []
    assert keepalive is not None


# ---------------------------------------------------------------- 探测器接线
def test_native_filter_reports_lock_and_unlock():
    app = _FakeApp()
    events: list = []
    watcher = sw_mod.SessionWatcher(app=app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=(
                                        lambda active, reason:
                                        events.append((active, reason))))
    try:
        addr, keep1 = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                   sw_mod.WTS_SESSION_LOCK)
        assert watcher.nativeEventFilter(0, addr) == (False, 0)
        addr2, keep2 = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                    sw_mod.WTS_SESSION_UNLOCK)
        watcher.nativeEventFilter(0, addr2)
        addr3, keep3 = _msg_pointer(sw_mod.WM_POWERBROADCAST,
                                    sw_mod.PBT_APMSUSPEND)
        watcher.nativeEventFilter(0, addr3)
        addr4, keep4 = _msg_pointer(sw_mod.WM_POWERBROADCAST,
                                    sw_mod.PBT_APMRESUMEAUTOMATIC)
        watcher.nativeEventFilter(0, addr4)

        assert events == [(True, "session_lock"), (False, "session_unlock"),
                          (True, "power_suspend"), (False, "power_resume")]
        assert watcher.armed is False, "锁屏不是会话结束：绝不许置位关机闸门"
    finally:
        app.deleteLater()
        assert keep1 is not None and keep2 is not None
        assert keep3 is not None and keep4 is not None


def test_suspend_callback_failure_is_isolated():
    """挂起回调抛异常不得打断原生过滤器（只观测、不拦截）。"""
    app = _FakeApp()

    def _boom(active, reason):
        raise RuntimeError("挂起接线失败")

    watcher = sw_mod.SessionWatcher(app=app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=_boom)
    try:
        addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                       sw_mod.WTS_SESSION_LOCK)
        assert watcher.nativeEventFilter(0, addr) == (False, 0)
    finally:
        app.deleteLater()
        assert keepalive is not None


def test_suspend_repeat_is_forwarded_but_state_is_tracked():
    """重复的同一状态仍转发（幂等由消费方保证），但绝不影响关机 latch。"""
    app = _FakeApp()
    events: list = []
    watcher = sw_mod.SessionWatcher(app=app, on_session_end=lambda: None,
                                    install_native_filter=False,
                                    on_suspend_change=(
                                        lambda active, reason: events.append(active)))
    try:
        addr, keepalive = _msg_pointer(sw_mod.WM_WTSSESSION_CHANGE,
                                      sw_mod.WTS_SESSION_LOCK)
        watcher.nativeEventFilter(0, addr)
        watcher.nativeEventFilter(0, addr)
        assert events == [True, True]
        assert watcher.armed is False
    finally:
        app.deleteLater()
        assert keepalive is not None


# ---------------------------------------------------------------- WTS 注册（win32 边界打桩）
@pytest.mark.skipif(sys.platform != "win32",
                    reason="WTSRegisterSessionNotification 是 Windows 专有 API")
def test_register_session_notifications_is_idempotent_and_unregisters(monkeypatch):
    calls: list = []

    def _register(hwnd, flags):
        calls.append(("register", int(hwnd), int(flags)))
        return 1

    def _unregister(hwnd):
        calls.append(("unregister", int(hwnd)))
        return 1

    monkeypatch.setattr(sw_mod, "_wts_register_session_notification", _register)
    monkeypatch.setattr(sw_mod, "_wts_unregister_session_notification", _unregister)

    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    assert watcher.register_session_notifications(12345) is True
    assert watcher.register_session_notifications(12345) is True   # 幂等
    assert calls == [("register", 12345, sw_mod.NOTIFY_FOR_THIS_SESSION)]

    watcher.unregister_session_notifications()

    assert calls == [("register", 12345, sw_mod.NOTIFY_FOR_THIS_SESSION),
                     ("unregister", 12345)]
    watcher.unregister_session_notifications()                     # 幂等
    assert len(calls) == 2


@pytest.mark.skipif(sys.platform != "win32",
                    reason="WTSRegisterSessionNotification 是 Windows 专有 API")
def test_register_failure_degrades_without_raising(monkeypatch, caplog):
    """注册失败 → 降级为不支持并记日志（绝不让启动路径炸掉）。"""
    def _register(hwnd, flags):
        raise OSError("wtsapi32 不可用")

    monkeypatch.setattr(sw_mod, "_wts_register_session_notification", _register)
    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    with caplog.at_level(logging.INFO, logger="pet.session_watcher"):
        assert watcher.register_session_notifications(12345) is False
    assert any("降级为不支持" in r.getMessage() for r in caplog.records), "失败必须留痕"
    # 未注册成功 ⇒ 反注册不得调用（绝不误摘别人的注册）
    monkeypatch.setattr(sw_mod, "_wts_unregister_session_notification",
                        lambda hwnd: pytest.fail("未注册成功不得反注册"))
    watcher.unregister_session_notifications()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX 降级路径：Windows 上走真实 WTS 注册（上面两条用例覆盖）")
def test_register_session_notifications_noop_on_posix(monkeypatch):
    """非 Windows：注册/反注册为无操作——返回 False 且绝不触碰 WTS 边界。

    产品契约（session_watcher 模块 docstring / install_session_watcher）：非
    Windows 平台降级为不支持锁屏探测，注册调用静默短路。本用例把这条降级
    契约锁死，避免「Windows 专有」用例被门控后 POSIX 路径裸奔。
    """
    monkeypatch.setattr(sw_mod, "_wts_register_session_notification",
                        lambda hwnd, flags: pytest.fail("POSIX 上不得触碰 WTS 注册边界"))
    monkeypatch.setattr(sw_mod, "_wts_unregister_session_notification",
                        lambda hwnd: pytest.fail("POSIX 上不得触碰 WTS 反注册边界"))
    watcher = sw_mod.SessionWatcher(app=None, on_session_end=lambda: None,
                                    install_native_filter=False)
    assert watcher.register_session_notifications(12345) is False
    watcher.unregister_session_notifications()


# ---------------------------------------------------------------- OverlayShell 接线
def _make_shell(tmp_path):
    import tests.test_sprite_menu_facade as fac

    return fac._make_shell(tmp_path)


def _stub_sprite(shell):
    from tests.test_sprite_visibility import PausableStubSprite

    sprite = PausableStubSprite()
    shell.overlay.add_sprite(sprite)
    return sprite


def test_shell_installs_watcher_with_suspend_callback_and_registers_hwnd(
        tmp_path, monkeypatch):
    """壳装配：探测器接上挂起回调，并对 overlay 句柄注册锁屏通知。"""
    from pet import overlay_shell as shell_mod

    installed: list = []
    registered: list = []

    class _Watcher:
        def register_session_notifications(self, hwnd):
            registered.append(int(hwnd))
            return True

        def unregister_session_notifications(self):
            registered.append("unregister")

    def _fake_install(*, app=None, on_session_end=None, on_suspend_change=None):
        installed.append((app, on_session_end, on_suspend_change))
        return _Watcher()

    monkeypatch.setattr(shell_mod, "install_session_watcher", _fake_install)
    shell, _lib = _make_shell(tmp_path)
    try:
        shell._install_session_watcher()

        assert len(installed) == 2, "构造期 + 显式调用各一次（幂等由装配方保证）"
        app, on_end, on_suspend = installed[-1]
        assert app is shell.app
        assert on_end == shell._on_session_end
        assert on_suspend == shell._on_suspend_changed, "挂起回调必须接线到壳"
        assert registered[-1] == int(shell.overlay.winId()), "必须对 overlay 句柄注册"
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_quit_unregisters_session_notifications(tmp_path):
    """退出收口必须反注册锁屏通知（与窗口句柄同生共死）。"""
    shell, _lib = _make_shell(tmp_path)
    calls: list = []

    class _Watcher:
        def unregister_session_notifications(self):
            calls.append("unregister")

    try:
        shell._session_watcher = _Watcher()
        shell._on_about_to_quit()

        assert calls == ["unregister"]
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_suspend_downgrades_without_hiding_and_resumes_clips(tmp_path):
    """锁屏/挂起只降档：clips 停/续 + 档位 T3→T0，overlay 可见性一动不动。"""
    from pet.tick_governor import TIER_ACTIVE, TIER_OCCLUDED

    shell, _lib = _make_shell(tmp_path)
    try:
        shell.overlay.show()
        sprite = _stub_sprite(shell)

        shell._on_suspend_changed(True, "session_lock")

        assert shell.driver.suspended is True
        assert shell.driver.applied_tier == TIER_OCCLUDED
        assert (sprite.pause_calls, sprite.resume_calls) == (1, 0)
        assert shell.overlay.isVisible() is True, "只降档：绝不隐藏 overlay"

        shell._on_suspend_changed(False, "session_unlock")

        assert shell.driver.suspended is False
        assert shell.driver.applied_tier == TIER_ACTIVE
        assert (sprite.pause_calls, sprite.resume_calls) == (1, 1)
        assert shell.overlay.isVisible() is True
    finally:
        shell.overlay.close()
        shell._delete_runtime_marker()


def test_shell_suspend_callback_swallows_errors(tmp_path):
    """接线容错：驱动器异常不得让原生消息链抛（事件过滤器在 Qt 事件循环里）。"""
    shell, _lib = _make_shell(tmp_path)

    class _BoomDriver:
        def set_suspended(self, active):
            raise RuntimeError("驱动故障")

    try:
        shell.driver = _BoomDriver()
        shell._on_suspend_changed(True, "power_suspend")   # 不许抛
    finally:
        shell.driver = None
        shell.overlay.close()
        shell._delete_runtime_marker()
