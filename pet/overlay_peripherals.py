# -*- coding: utf-8 -*-
"""overlay 外围窗口能力（Phase 4.1b）：全屏/光标监视器。

语义逐位对齐旧架构的每窗路径（pet/window_screen.py 的
on_fullscreen_changed / on_cursor_visibility_changed 与 multi_window_shared
的探测节拍），只是消费者从 PetWindow 换成 OverlayShell：

- 全屏探测 1Hz（platform_win._fg_fullscreen_probe）：前台出现全屏应用 →
  隐藏 overlay；退出 → 恢复。配置门 auto_hide_fullscreen（默认 True）。
- 光标可见性 20Hz（vision.get_cursor_visibility）：HIDDEN 持续 ≥0.2s →
  自动穿透（游戏/全屏应用抢走光标时让出鼠标）；SHOWING → 恢复。
  配置门 cursor_hidden_passthrough（默认 True）。
- 仅 Windows（旧语义同为 Windows 限定）；两扇门全关时停表省电。

探测函数可注入（测试喂假探针，不碰 Win32）。
"""
from __future__ import annotations

import sys
import time

from PySide6.QtCore import QObject, QTimer, Signal

_FULLSCREEN_POLL_MS = 1000
_CURSOR_POLL_MS = 50
_CURSOR_HIDDEN_DEBOUNCE_S = 0.2


class FullscreenCursorWatcher(QObject):
    """overlay 版全屏/光标监视（4.1b parity）。"""

    fullscreen_changed = Signal(bool)
    cursor_visibility_changed = Signal(str)

    def __init__(self, parent: QObject | None = None, *,
                 fullscreen_probe=None, cursor_probe=None, clock=time.monotonic) -> None:
        super().__init__(parent)
        self._clock = clock
        if fullscreen_probe is None or cursor_probe is None:
            if sys.platform == "win32":
                from . import platform_win
                from . import vision
                fullscreen_probe = fullscreen_probe or (
                    lambda: platform_win._fg_fullscreen_probe()[0])
                cursor_probe = cursor_probe or vision.get_cursor_visibility
        self._fullscreen_probe = fullscreen_probe or (lambda: False)
        self._cursor_probe = cursor_probe or (lambda: "UNKNOWN")
        self._fullscreen_enabled = False
        self._cursor_enabled = False
        self._fs_last = False
        self._cursor_hidden_since: float | None = None
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(_FULLSCREEN_POLL_MS)
        self._fs_timer.timeout.connect(self._poll_fullscreen)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(_CURSOR_POLL_MS)
        self._cursor_timer.timeout.connect(self._poll_cursor)

    # ---------------------------------------------------------------- 开关
    def set_fullscreen_enabled(self, on: bool) -> None:
        """auto_hide_fullscreen 配置门：关时停表（省电）并复位状态。"""
        on = bool(on)
        if on == self._fullscreen_enabled:
            return
        self._fullscreen_enabled = on
        if on:
            self._fs_last = False
            self._fs_timer.start()
        else:
            self._fs_timer.stop()
            if self._fs_last:
                self._fs_last = False
                self.fullscreen_changed.emit(False)  # 关门即恢复（不滞留隐藏态）

    def set_cursor_enabled(self, on: bool) -> None:
        """cursor_hidden_passthrough 配置门：关时停表并恢复穿透态。"""
        on = bool(on)
        if on == self._cursor_enabled:
            return
        self._cursor_enabled = on
        if on:
            self._cursor_hidden_since = None
            self._cursor_timer.start()
        else:
            self._cursor_timer.stop()
            self.cursor_visibility_changed.emit("SHOWING")

    # ---------------------------------------------------------------- 探测（同步直调可测）
    def _poll_fullscreen(self) -> None:
        try:
            hit = bool(self._fullscreen_probe())
        except Exception:
            return  # 探测失败不翻转状态（旧路径同样静默）
        if hit != self._fs_last:
            self._fs_last = hit
            self.fullscreen_changed.emit(hit)

    def _poll_cursor(self) -> None:
        try:
            visibility = str(self._cursor_probe())
        except Exception:
            return
        now = self._clock()
        if visibility == "HIDDEN":
            if self._cursor_hidden_since is None:
                self._cursor_hidden_since = now
            elif now - self._cursor_hidden_since >= _CURSOR_HIDDEN_DEBOUNCE_S:
                self.cursor_visibility_changed.emit("HIDDEN")
        elif visibility == "SHOWING":
            self._cursor_hidden_since = None
            self.cursor_visibility_changed.emit("SHOWING")

    def stop(self) -> None:
        self._fs_timer.stop()
        self._cursor_timer.stop()
