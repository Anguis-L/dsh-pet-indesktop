# -*- coding: utf-8 -*-
"""sprite 气泡跟随（4.1c）：真实 PetSpeechBubble 独立小窗跟随 sprite。

自 demo（.scratch/single-overlay-window/run_overlay_demo.py Phase 3b）
迁入 pet/ 的产品化版本：跟随源 = overlay.add_position_listener（sprite
不产生 moveEvent）；锚点 = sprite 身体框换算全局（V-2 口径——整画布
rect 会把气泡锚到透明边上沿）；30Hz 跟随节流 + 尾部补发（V-5：tick 级
fanout 游走期每 tick 一次 SetWindowPos ≈1.5ms 的 WM 移动税不能再交）。

构造/跟随/显隐全链路 try/except 静默降级——气泡是外围装饰，失败绝不
崩主链路。聊天/快速对话锚点（island_chat 域）不在本层，属后续外围刀。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QPoint, QRect, QTimer

from .speech_bubble import PetSpeechBubble

logger = logging.getLogger(__name__)

_FOLLOW_INTERVAL = 1.0 / 30.0  # 30Hz 跟随上限


def sprite_anchor_rect_global(sprite, overlay_origin: QPoint) -> QRect:
    """气泡锚点：sprite 身体框（overlay 局部）换算到全局屏幕坐标。

    口径平移自 window_placement.bubble_anchor_rect（旧架构锚点是"host 的
    可见内容矩形"）；sprite 侧的同口径是 body_rect()（body_box×scale；
    未声明 body_box 回退全画布 rect()），直接加 overlay 原点即得全局
    锚点（overlay 铺满屏幕、自身不移动，原点即屏幕全局原点）。
    """
    rect = sprite.body_rect() if hasattr(sprite, "body_rect") else sprite.rect()
    return QRect(overlay_origin + rect.topLeft(), rect.size())


class SpriteBubbleFollower:
    """一只 sprite 的气泡跟随（真实 PetSpeechBubble 独立小窗）。"""

    def __init__(self, overlay, sprite, *, style_id: str = "classic_top") -> None:
        self._overlay = overlay
        self._sprite = sprite
        self._origin = overlay.geometry().topLeft()
        self.bubble: PetSpeechBubble | None = None
        self._last_follow = 0.0
        self._follow_pending = False
        self._follow_timer = QTimer(overlay)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(int(_FOLLOW_INTERVAL * 1000))
        self._follow_timer.timeout.connect(self._flush_follow)
        try:
            self.bubble = PetSpeechBubble(style_id=style_id)
            overlay.add_position_listener(sprite, self._on_sprite_moved)
        except Exception:
            self.bubble = None  # 构造失败 = 无气泡，静默降级

    def anchor(self) -> QRect:
        return sprite_anchor_rect_global(self._sprite, self._origin)

    def _on_sprite_moved(self, sprite) -> None:
        if self.bubble is None:
            return
        now = time.monotonic()
        if now - self._last_follow >= _FOLLOW_INTERVAL:
            self._last_follow = now
            self._follow_pending = False
            try:
                self.bubble.reposition(self.anchor())
            except Exception:
                pass
        else:
            self._follow_pending = True
            if not self._follow_timer.isActive():
                self._follow_timer.start()

    def _flush_follow(self) -> None:
        """节流窗口结束后补发最后一帧位置（防气泡停在半途）。"""
        if not self._follow_pending or self.bubble is None:
            return
        self._follow_pending = False
        self._last_follow = time.monotonic()
        try:
            self.bubble.reposition(self.anchor())
        except Exception:
            pass

    def say(self, text: str, duration_ms: int = 3200, *, subtitle: str = "") -> bool:
        """播一句气泡文本；气泡不可用/文案为空返回 False（静默降级）。"""
        text = str(text or "").strip()
        if self.bubble is None or not text:
            return False
        try:
            self.bubble.show_text(text, self.anchor(), duration_ms,
                                  subtitle=subtitle,
                                  pet_scale=getattr(self._sprite, "scale", None))
            return True
        except Exception:
            logger.debug("overlay: 气泡播放失败", exc_info=True)
            return False

    def set_origin(self, origin: QPoint) -> None:
        """屏迁移后更新全局原点（overlay 重建时由壳层调用）。"""
        self._origin = QPoint(origin)

    def close(self) -> None:
        try:
            self._overlay.remove_position_listener(self._sprite, self._on_sprite_moved)
        except Exception:
            pass
        if self.bubble is not None:
            try:
                self.bubble.close()
            except Exception:
                pass
            self.bubble = None
